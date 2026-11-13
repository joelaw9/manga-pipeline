"""Workflow rules (SPEC "Parts", "State", "Chapter stages", "Needs human"): scope, claims, outcomes, waits,
attempts, needs_human and priority.

The only module that writes chapter.stage, status, waiting_on, wait_data, attempts and claimed_by, and series.status.
A part claims a chapter in 1 short transaction, runs its stage with no transaction open, and records the outcome in
another, guarded so a row a person changed meanwhile keeps their change.
"""

import errno
import json
import logging
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from manga_pipeline import db

log = logging.getLogger(__name__)
STAGES = ("discover", "validate", "junk", "convert", "comicinfo", "publish", "confirm")
PART_STAGES = {
    "intake": ("discover", "validate"),
    "process": ("junk", "convert"),
    "export": ("comicinfo", "publish", "confirm"),
}
# A share path failing with these is the share being unreachable, not the chapter failing.
SHARE_ERRNOS = {errno.EIO, errno.ESTALE, errno.ETIMEDOUT}
# The scope filter of every work-picking query: in_scope, registered on the connection by Context.
SCOPE = "in_scope(s.name)"


@dataclass(frozen=True)
class Context:
    cfg: dict
    conn: Any  # sqlite3.Connection from db.connect
    part: str
    # Chapters that failed in this run: tried again by a later run, so 1 run never spends all their attempts.
    failed: set = field(default_factory=set)
    # {stage: service} for the stages whose service was unreachable in this run: not claimed again until a later run.
    unreachable: dict = field(default_factory=dict)
    # Outcomes of this run by kind: done, waiting, failed.
    counts: Counter = field(default_factory=Counter)

    def __post_init__(self):
        # SQL's scope filter is in_scope itself, so the rule has 1 definition.
        self.conn.create_function("in_scope", 1, lambda name: in_scope(self.cfg, name), deterministic=True)


@dataclass(frozen=True)
class Done:
    # Writes that belong with the outcome (e.g. the published_file row of a rename): run in the outcome's transaction.
    record: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class Waiting:
    on: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Failed:
    error: str


class Unreachable(Exception):
    """A service the stage needs cannot be reached: the chapter stays ready at no attempt, and the part claims no
    chapter at that stage for the rest of the run (raised outside a stage, it ends the run)."""

    def __init__(self, service):
        super().__init__(service)
        self.service = service


def in_scope(cfg, series_name):
    """Whether a series is worked: series.only is empty or names it. The 1 definition of scope; SQL calls it as
    in_scope(name) (SCOPE)."""
    only = cfg["series"]["only"]
    return not only or series_name in only


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def add_chapter(conn, series_id, identity, download_path):
    """Record a discovered download as a chapter ready at the first stage; runs inside the caller's transaction."""
    if not conn.in_transaction:
        raise RuntimeError("add_chapter runs inside the caller's transaction")
    stamp = now()
    conn.execute(
        """INSERT INTO chapter (series_id, kind, identity, download_path, stage, status, discovered_at, updated_at)
        VALUES (?, 'download', ?, ?, ?, 'ready', ?, ?)""",
        (series_id, identity, download_path, STAGES[0], stamp, stamp),
    )


def claim(ctx, stages, series_id=None):
    """Claim the next ready chapter at one of stages, by priority: series Kavita knows before new series, then the
    oldest discovery, then id. Chapters of a needs_human series, out of scope, failed in this run, or at a stage whose
    service was unreachable in this run are not picked; nor is a convert-stage chapter while its series has a ready
    junk-stage chapter (a series' junk is scanned before any of it converts). Returns the claimed row, or None when
    nothing is ready."""
    stages = [s for s in stages if s not in ctx.unreachable]
    with db.transaction(ctx.conn):
        return ctx.conn.execute(
            f"""UPDATE chapter SET status = 'claimed', claimed_by = :part, updated_at = :now
            WHERE id = (
                SELECT c.id FROM chapter c JOIN series s ON s.id = c.series_id
                WHERE c.status = 'ready' AND c.stage IN (SELECT value FROM json_each(:stages))
                    AND s.status = 'ok' AND {SCOPE}
                    AND (:series_id IS NULL OR c.series_id = :series_id)
                    AND c.id NOT IN (SELECT value FROM json_each(:failed))
                    AND NOT (c.stage = 'convert' AND EXISTS (
                        SELECT 1 FROM chapter j
                        WHERE j.series_id = c.series_id AND j.stage = 'junk' AND j.status = 'ready'
                            AND j.id NOT IN (SELECT value FROM json_each(:failed))))
                ORDER BY s.origin = 'new', c.discovered_at, c.id
                LIMIT 1)
            RETURNING *""",
            _params(ctx, stages=json.dumps(stages), series_id=series_id, failed=json.dumps(sorted(ctx.failed))),
        ).fetchone()


def series_with_ready(ctx, stages):
    """Ids of the in-scope, not needs_human series with a ready chapter at one of stages, in priority order."""
    rows = ctx.conn.execute(
        f"""SELECT c.series_id FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'ready' AND c.stage IN (SELECT value FROM json_each(:stages)) AND s.status = 'ok' AND {SCOPE}
        GROUP BY c.series_id
        ORDER BY s.origin = 'new', MIN(c.discovered_at), MIN(c.id)""",
        _params(ctx, stages=json.dumps(stages)),
    )
    return [row[0] for row in rows]


def work(ctx, chapter, stage):
    """Run stage(ctx, chapter) on a claimed chapter, record its outcome and log it. Any exception from the stage is
    Failed, except Unreachable (and an OSError with a SHARE_ERRNOS errno, which is Unreachable("share")): that leaves
    the chapter ready with its attempts unchanged, and the chapter's stage is not claimed again in this run. Returns
    the outcome (the Unreachable for that case). An outcome dropped because a person changed the row meanwhile is
    neither logged as an outcome nor counted."""
    if ctx.conn.in_transaction:
        raise RuntimeError("a stage must run with no transaction open")
    try:
        with share_errors():
            outcome = stage(ctx, chapter)
    except Unreachable as e:
        _finish(ctx, chapter, e)
        ctx.unreachable[chapter["stage"]] = e.service
        log.warning(
            "%s: %s: %s unreachable; stage %s skipped for this run",
            ctx.part,
            _name(chapter),
            e.service,
            chapter["stage"],
        )
        return e
    except Exception as e:
        outcome = Failed(f"{type(e).__name__}: {e}")
    if not isinstance(outcome, Done | Waiting | Failed):
        raise TypeError(f"stage {stage.__module__}.{stage.__name__} returned {outcome!r}, not an outcome")
    try:
        recorded = _finish(ctx, chapter, outcome)
    except Exception as e:
        if not (isinstance(outcome, Done) and outcome.record):
            raise
        outcome = Failed(f"recording the outcome: {type(e).__name__}: {e}")
        recorded = _finish(ctx, chapter, outcome)
    if not recorded:
        return outcome  # _finish logged that it was dropped
    if isinstance(outcome, Failed):
        ctx.failed.add(chapter["id"])
        ctx.counts["failed"] += 1
        log.warning("%s: %s failed: %s", ctx.part, _name(chapter), outcome.error)
    elif isinstance(outcome, Waiting):
        ctx.counts["waiting"] += 1
        log.info("%s: %s waits on %s", ctx.part, _name(chapter), outcome.on)
    else:
        ctx.counts["done"] += 1
        log.info("%s: %s done", ctx.part, _name(chapter))
    return outcome


def _name(chapter):
    """How a log line names a chapter: id, series, identity and stage."""
    return f"chapter {chapter['id']} (series {chapter['series_id']}, {chapter['identity']}, at {chapter['stage']})"


def drain(ctx, stages, series_id=None):
    """Work chapters until none is ready at the stages of `stages` ({stage: stage function}), claiming again by
    priority after each one."""
    while chapter := claim(ctx, tuple(stages), series_id):
        work(ctx, chapter, stages[chapter["stage"]])


def end_waits(ctx, on, holds):
    """Make every in-scope chapter waiting on `on` ready again at its stage when holds(ctx, chapter) is true."""
    rows = ctx.conn.execute(
        f"""SELECT c.* FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = :on AND {SCOPE} ORDER BY c.id""",
        _params(ctx, on=on),
    ).fetchall()
    for chapter in rows:
        try:
            with share_errors():
                ended = holds(ctx, chapter)
        except Unreachable:
            raise
        except Exception as e:
            log.error(
                "%s: checking the %s wait of %s failed: %s: %s", ctx.part, on, _name(chapter), type(e).__name__, e
            )
            continue
        if ended:
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    """UPDATE chapter SET status = 'ready', waiting_on = NULL, wait_data = NULL, updated_at = ?
                    WHERE id = ? AND status = 'waiting' AND waiting_on = ?""",
                    (now(), chapter["id"], on),
                )


def release_own_claims(ctx):
    """Free the in-scope claims a killed run of this part left: back to ready at their stage, attempts unchanged. Not
    when lock.enabled is false: another live run of the part may hold them."""
    if not ctx.cfg["lock"]["enabled"]:
        return
    with db.transaction(ctx.conn):
        released = ctx.conn.execute(
            f"""UPDATE chapter SET status = 'ready', claimed_by = NULL, updated_at = :now
            WHERE status = 'claimed' AND claimed_by = :part
                AND series_id IN (SELECT s.id FROM series s WHERE {SCOPE})""",
            _params(ctx),
        ).rowcount
    if released:
        log.info("%s: released %d claim(s) left by an earlier run", ctx.part, released)


def needs_human(conn, kind, item_id, reason):
    """The single place a chapter or a series becomes needs_human (kind "chapter" or "series"); runs inside the
    caller's transaction."""
    if not conn.in_transaction:
        raise RuntimeError("needs_human runs inside the caller's transaction")
    if kind == "chapter":
        conn.execute(
            """UPDATE chapter SET status = 'needs_human', claimed_by = NULL, waiting_on = NULL, wait_data = NULL,
                last_error = ?, updated_at = ?
            WHERE id = ?""",
            (reason, now(), item_id),
        )
    elif kind == "series":
        conn.execute("UPDATE series SET status = 'needs_human', status_reason = ? WHERE id = ?", (reason, item_id))
    else:
        raise ValueError(f"needs_human: kind must be chapter or series, got {kind!r}")
    log.warning("needs human: %s %s: %s", kind, item_id, reason)  # Phase 8 adds the Discord message


class Refused(ValueError):
    """A person's command that does not apply to the item it names; nothing was changed."""


def retry_chapter(conn, chapter_id):
    """A needs_human chapter becomes ready again at the stage where it stopped, with its attempts at 0."""
    with db.transaction(conn):
        row = conn.execute("SELECT status FROM chapter WHERE id = ?", (chapter_id,)).fetchone()
        if row is None:
            raise Refused(f"no chapter {chapter_id}")
        if row["status"] != "needs_human":
            raise Refused(f"chapter {chapter_id} is {row['status']}, not needs_human")
        _ready_again(conn, "id = ?", chapter_id)


def retry_series(conn, series_id):
    """A series becomes ok (reason cleared), and its needs_human chapters ready again at their stages, attempts 0.
    Refused when neither the series nor any of its chapters is needs_human."""
    with db.transaction(conn):
        row = conn.execute("SELECT status FROM series WHERE id = ?", (series_id,)).fetchone()
        if row is None:
            raise Refused(f"no series {series_id}")
        chapters = _ready_again(conn, "series_id = ? AND status = 'needs_human'", series_id)
        if row["status"] != "needs_human" and not chapters:
            raise Refused(f"series {series_id} is {row['status']} and has no needs_human chapter")
        conn.execute("UPDATE series SET status = 'ok', status_reason = NULL WHERE id = ?", (series_id,))


def reset(conn):
    """Empty the workflow state (SPEC "State"): every WORKFLOW_TABLES table, and every series back to ok."""
    with db.transaction(conn):
        for table in db.WORKFLOW_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE series SET status = 'ok', status_reason = NULL")


def _ready_again(conn, where, value):
    return conn.execute(
        f"UPDATE chapter SET status = 'ready', attempts = 0, updated_at = ? WHERE {where}", (now(), value)
    ).rowcount


def run_part(ctx, body):
    """A part's run (SPEC "Parts"): record its start, release its own stale claims, run body(ctx), record its end and
    result ("ok", "unreachable: <service>, …" or "error"), and its last success when the result is ok. Unreachable
    raised outside a stage (discovery, a wait check) ends the run; either way the exit code stays 0. Any other
    exception is logged with its traceback, recorded as "error" and raised again."""
    _set_meta(ctx, "last_start", now())
    result = "error"
    services = set()
    try:
        release_own_claims(ctx)
        body(ctx)
    except Unreachable as e:
        services.add(e.service)
        log.warning("%s: %s unreachable, run ended", ctx.part, e.service)
        result = "unreachable"
    except Exception:
        log.exception("%s: run failed", ctx.part)
        raise
    else:
        result = "ok"
    finally:
        if result != "error":
            services |= set(ctx.unreachable.values())
            if services:
                result = f"unreachable: {', '.join(sorted(services))}"
        end = now()
        _set_meta(ctx, "last_end", end)
        _set_meta(ctx, "last_result", result)
        if result == "ok":
            _set_meta(ctx, "last_success", end)
        counts = ", ".join(f"{ctx.counts[kind]} {kind}" for kind in ("done", "waiting", "failed"))
        log.info("%s: run ended: %s (%s)", ctx.part, result, counts)


def _finish(ctx, chapter, outcome):
    """Record outcome for a chapter this part claimed, unless a person changed the row meanwhile. Returns whether it
    was recorded."""
    limit = ctx.cfg["workflow"]["attempt_limit"]
    set_, values, at_limit = _outcome_columns(chapter, outcome, limit)
    with db.transaction(ctx.conn):
        changed = ctx.conn.execute(
            f"UPDATE chapter SET {set_}, claimed_by = NULL, updated_at = ? "
            "WHERE id = ? AND claimed_by = ? AND status = 'claimed'",
            (*values, now(), chapter["id"], ctx.part),
        ).rowcount
        if not changed:
            log.warning("chapter %s changed while %s worked on it; its outcome is dropped", chapter["id"], ctx.part)
            return False
        if at_limit:
            needs_human(ctx.conn, "chapter", chapter["id"], f"{outcome.error} (after {limit} attempts)")
        if isinstance(outcome, Done) and outcome.record:
            outcome.record(ctx.conn)
    return True


def _outcome_columns(chapter, outcome, limit):
    """(SET clause, its values, whether the attempt limit is reached) for an outcome."""
    if isinstance(outcome, Done):
        last = chapter["stage"] == STAGES[-1]
        stage = chapter["stage"] if last else STAGES[STAGES.index(chapter["stage"]) + 1]
        return "stage = ?, status = ?, attempts = 0", (stage, "done" if last else "ready"), False
    if isinstance(outcome, Waiting):
        return "status = 'waiting', waiting_on = ?, wait_data = ?", (outcome.on, json.dumps(outcome.data)), False
    if isinstance(outcome, Failed):
        attempts = chapter["attempts"] + 1
        return "status = 'ready', attempts = ?, last_error = ?", (attempts, outcome.error), attempts >= limit
    return "status = 'ready'", (), False  # Unreachable: no attempt


@contextmanager
def share_errors():
    """An OSError with a SHARE_ERRNOS errno becomes Unreachable("share"); other errors pass unchanged."""
    try:
        yield
    except OSError as e:
        if e.errno not in SHARE_ERRNOS:
            raise
        raise Unreachable("share") from e


def _params(ctx, **extra):
    return {"part": ctx.part, "now": now(), **extra}


def _set_meta(ctx, name, value):
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f"{ctx.part}.{name}", value),
        )
