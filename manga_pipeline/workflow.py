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
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.errors import Unreachable

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
SCOPE = "in_scope(s.name, s.kavita_library_id)"
# A junk-stage chapter {c} holds a current junk scan (SPEC "Junk decisions"): same series, same content digest, and
# made with logo detection (a scan without "marks" is not current). The one test of a current scan.
SCANNED = """EXISTS (SELECT 1 FROM junk_scan js WHERE js.chapter_id = {c}.id AND js.series_id = {c}.series_id
    AND js.content_digest = {c}.content_digest AND json_type(js.scan, '$.marks') = 'array')"""


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
    # The run's 1 client of each service (SPEC "Services"): what it learns holds for the run. None = built from cfg.
    suwayomi: Any = None
    kavita: Any = None

    def __post_init__(self):
        if self.suwayomi is None:
            object.__setattr__(self, "suwayomi", suwayomi_client.from_config(self.cfg))
        if self.kavita is None:
            object.__setattr__(self, "kavita", kavita_client.from_config(self.cfg))
        # SQL's scope filter is in_scope itself, so the rule has 1 definition.
        self.conn.create_function(
            "in_scope", 2, lambda name, library: in_scope(self.cfg, name, library), deterministic=True
        )


@dataclass(frozen=True)
class Done:
    # Writes that belong with the outcome (e.g. the published_file row of a rename): run in the outcome's transaction.
    record: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class Waiting:
    on: str
    data: dict = field(default_factory=dict)
    record: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class Ready:
    """The chapter stays ready at its stage, with no attempt, and may be claimed again in this run (the junk stage
    after a scan, SPEC "Chapter stages")."""

    record: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class Dropped:
    """The chapter leaves the pipeline where it stands, never published (status done at its stage)."""

    reason: str
    record: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class NeedsHuman:
    """No further attempt can change the result: the chapter is needs_human at once."""

    reason: str
    record: Callable[[Any], None] | None = None


OUTCOMES = Done | Waiting | Failed | Dropped | NeedsHuman | Ready
RECORDED = Done | Dropped | Waiting | Ready | NeedsHuman  # outcomes that may carry a record


def in_scope(cfg, series_name, kavita_library_id):
    """Whether a series is worked: Kavita holds it in the library kavita.library_id or does not hold it yet
    (kavita_library_id None: a new series), and series.only is empty or names it. The 1 definition of scope; SQL
    calls it as in_scope(name, kavita_library_id) (SCOPE)."""
    if kavita_library_id is not None and kavita_library_id != cfg["kavita"]["library_id"]:
        return False
    only = cfg["series"]["only"]
    return not only or series_name in only


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def add_chapter(conn, series_id, identity, download_path, suwayomi_chapter_id, source):
    """Record a discovered download as a chapter ready at the first stage; runs inside the caller's transaction."""
    if not conn.in_transaction:
        raise RuntimeError("add_chapter runs inside the caller's transaction")
    stamp = now()
    conn.execute(
        """INSERT INTO chapter (series_id, kind, identity, download_path, suwayomi_chapter_id, source, stage, status,
            discovered_at, updated_at)
        VALUES (?, 'download', ?, ?, ?, ?, ?, 'ready', ?, ?)""",
        (series_id, identity, download_path, suwayomi_chapter_id, source, STAGES[0], stamp, stamp),
    )


def claim(ctx, stages, series_id=None):
    """Claim the next ready chapter at one of stages, by priority: series Kavita knows before new series, then the
    oldest discovery, then id. Chapters of a needs_human series, out of scope, failed in this run, or at a stage whose
    service was unreachable in this run are not picked; nor is a convert-stage chapter while its series has a ready
    junk-stage chapter (a series' junk is decided before any of it converts), nor a scanned junk-stage chapter while
    its series has a ready junk-stage chapter not yet scanned (every one is scanned before any is decided; a scan is
    current while its series and content digest are the chapter's). Returns the claimed row, or None when nothing is
    ready."""
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
                    AND NOT (c.stage = 'junk' AND {SCANNED.format(c="c")} AND EXISTS (
                        SELECT 1 FROM chapter u
                        WHERE u.series_id = c.series_id AND u.stage = 'junk' AND u.status = 'ready'
                            AND u.id NOT IN (SELECT value FROM json_each(:failed))
                            AND NOT {SCANNED.format(c="u")}))
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
            "%s: %s: unreachable (%s); stage %s skipped for this run",
            ctx.part,
            _name(chapter),
            e,
            chapter["stage"],
        )
        return e
    except Exception as e:
        outcome = Failed(f"{type(e).__name__}: {e}")
    if not isinstance(outcome, OUTCOMES):
        raise TypeError(f"stage {stage.__module__}.{stage.__name__} returned {outcome!r}, not an outcome")
    try:
        recorded = _finish(ctx, chapter, outcome)
    except Exception as e:
        if not (isinstance(outcome, RECORDED) and outcome.record):
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
    elif isinstance(outcome, Dropped):
        ctx.counts["dropped"] += 1
        log.info("%s: %s dropped: %s", ctx.part, _name(chapter), outcome.reason)
    elif isinstance(outcome, NeedsHuman):
        ctx.counts["needs_human"] += 1  # needs_human logged the reason
    elif isinstance(outcome, Ready):
        ctx.counts["ready"] += 1
        log.info("%s: %s scanned, ready again", ctx.part, _name(chapter))
    else:
        ctx.counts["done"] += 1
        log.info("%s: %s done", ctx.part, _name(chapter))
    return outcome


def _name(chapter):
    """How a log line names a chapter: id, series, identity and stage."""
    return f"chapter {chapter['id']} (series {chapter['series_id']}, {chapter['identity']}, at {chapter['stage']})"


def drain(ctx, stages, series_id=None):
    """Work chapters until none is ready at the stages of `stages` ({stage: stage function}), claiming again by
    priority after each one. Returns how many ended Done."""
    done = 0
    while chapter := claim(ctx, tuple(stages), series_id):
        done += isinstance(work(ctx, chapter, stages[chapter["stage"]]), Done)
    return done


def end_waits(ctx, on, holds):
    """Make every in-scope chapter waiting on `on` ready again at its stage when holds(ctx, chapter) is true, or
    needs_human when it returns a NeedsHuman (no further wait can help). The chapters of a needs_human series keep
    waiting."""
    rows = ctx.conn.execute(
        f"""SELECT c.* FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = :on AND s.status = 'ok' AND {SCOPE} ORDER BY c.id""",
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
        if isinstance(ended, NeedsHuman):
            with db.transaction(ctx.conn):
                still = ctx.conn.execute(
                    "SELECT 1 FROM chapter WHERE id = ? AND status = 'waiting' AND waiting_on = ?", (chapter["id"], on)
                ).fetchone()
                if still:
                    needs_human(ctx.conn, "chapter", chapter["id"], ended.reason)
        elif ended:
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    """UPDATE chapter SET status = 'ready', waiting_on = NULL, wait_data = NULL, updated_at = ?
                    WHERE id = ? AND status = 'waiting' AND waiting_on = ?""",
                    (now(), chapter["id"], on),
                )


def charge_waits(ctx, on, reason, waited_since, at):
    """1 attempt for each in-scope chapter of an ok series waiting on `on` whose wait was recorded at or before
    waited_since (a wait renewed at a cost, SPEC "Publishing"); the charge stamps updated_at with `at`, so a charged
    chapter's wait counts from then. A chapter reaching the attempt limit becomes needs_human. Runs inside the caller's
    transaction; returns how many were charged."""
    if not ctx.conn.in_transaction:
        raise RuntimeError("charge_waits runs inside the caller's transaction")
    limit = ctx.cfg["workflow"]["attempt_limit"]
    rows = ctx.conn.execute(
        f"""UPDATE chapter SET attempts = attempts + 1, last_error = :reason, updated_at = :at
        WHERE status = 'waiting' AND waiting_on = :on AND updated_at <= :since
            AND series_id IN (SELECT s.id FROM series s WHERE s.status = 'ok' AND {SCOPE})
        RETURNING id, attempts""",
        _params(ctx, reason=reason, on=on, since=waited_since, at=at),
    ).fetchall()
    for row in rows:
        if row["attempts"] >= limit:
            needs_human(ctx.conn, "chapter", row["id"], f"{reason} (after {limit} attempts)")
    return len(rows)


def release_own_claims(ctx):
    """Free the claims a killed run of this part left, in scope or not: back to ready at their stage, attempts
    unchanged. Not when lock.enabled is false: another live run of the part may hold them."""
    if not ctx.cfg["lock"]["enabled"]:
        return
    with db.transaction(ctx.conn):
        released = ctx.conn.execute(
            """UPDATE chapter SET status = 'ready', claimed_by = NULL, updated_at = :now
            WHERE status = 'claimed' AND claimed_by = :part""",
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


def move_chapters(conn, from_series, to_series):
    """Move a series' chapters to another (series link merging a held row; inside the caller's transaction). An open
    chapter past junk goes back to junk, ready, attempts 0, so the new series' baseline and handled-digest check
    apply; a part holding its claim then drops its outcome."""
    junk = STAGES.index("junk")
    later = STAGES[junk + 1 :]
    conn.execute(
        f"""UPDATE chapter SET stage = 'junk', status = 'ready', waiting_on = NULL, wait_data = NULL,
            claimed_by = NULL, attempts = 0, updated_at = ?
        WHERE series_id = ? AND status NOT IN ('done', 'needs_human') AND stage IN ({",".join("?" * len(later))})""",
        (now(), from_series, *later),
    )
    conn.execute("UPDATE chapter SET series_id = ? WHERE series_id = ?", (to_series, from_series))


def reset(conn):
    """Empty the workflow state (SPEC "State"): every WORKFLOW_TABLES table, and every series back to ok but one held
    for want of a Kavita series or a folder, which only `series link` settles (identification never runs again)."""
    with db.transaction(conn):
        for table in db.WORKFLOW_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            """UPDATE series SET status = 'ok', status_reason = NULL
            WHERE kavita_series_id IS NOT NULL AND folder IS NOT NULL"""
        )


def _ready_again(conn, where, value):
    """Ready again at its stage, attempts 0; a re-download's source search starts again from the top, and a person's
    choice (chapter use) is forgotten (SPEC "Bad downloads")."""
    return conn.execute(
        f"""UPDATE chapter SET status = 'ready', attempts = 0, updated_at = ?,
            redownload = CASE WHEN json_extract(redownload, '$.original') IS NULL THEN NULL
                ELSE json_set(redownload, '$.tried', json('[]'), '$.same_source_done', json('false'),
                    '$.chosen', json('null')) END
        WHERE {where}""",
        (now(), value),
    ).rowcount


# The stages at which a person may drop a needs_human chapter: nothing of it is staged or published yet.
DROPPABLE = ("discover", "validate", "junk")


def use_chapter(conn, chapter_id, suwayomi_chapter_id):
    """A person's chosen replacement (chapter use): a needs_human chapter at validate becomes ready with the Suwayomi
    chapter to download as its replacement (SPEC "Bad downloads")."""
    with db.transaction(conn):
        _refuse_unless_human(conn, chapter_id, ("validate",))
        conn.execute(
            """UPDATE chapter SET status = 'ready', attempts = 0, updated_at = ?,
                redownload = json_set(coalesce(redownload, '{}'), '$.chosen', ?)
            WHERE id = ?""",
            (now(), suwayomi_chapter_id, chapter_id),
        )


def droppable(conn, chapter_id):
    """The row of a chapter a person may drop (needs_human at a DROPPABLE stage, with no removal or download of its
    re-download outstanding); Refused otherwise."""
    with db.transaction(conn):
        row = _refuse_unless_human(conn, chapter_id, DROPPABLE)
        state = json.loads(row["redownload"] or "{}")
        pending = [r["path"] for r in state.get("remove", [])] + (
            [state["expected"]["path"]] if state.get("expected") else []
        )
        if pending:
            raise Refused(
                f"chapter {chapter_id} still has re-download work outstanding ({', '.join(pending)}): "
                f"retry {chapter_id} lets intake finish it"
            )
        return row


def drop_chapter(conn, chapter_id, record):
    """A person gives a needs_human chapter up (chapter drop): done where it stands, with record(conn) (its handled
    row) in the same transaction. The caller removes its download first."""
    with db.transaction(conn):
        _refuse_unless_human(conn, chapter_id, DROPPABLE)
        conn.execute(
            """UPDATE chapter SET status = 'done', last_error = 'dropped by a person', redownload = NULL,
                updated_at = ? WHERE id = ?""",
            (now(), chapter_id),
        )
        record(conn)


def _refuse_unless_human(conn, chapter_id, stages):
    row = conn.execute("SELECT * FROM chapter WHERE id = ?", (chapter_id,)).fetchone()
    if row is None:
        raise Refused(f"no chapter {chapter_id}")
    if row["status"] != "needs_human" or row["stage"] not in stages:
        raise Refused(
            f"chapter {chapter_id} is {row['status']} at {row['stage']}, not needs_human at {' or '.join(stages)}"
        )
    return row


def run_part(ctx, body):
    """A part's run (SPEC "Parts"): record its start, release its own stale claims, run body(ctx), record its end and
    result ("ok", "unreachable: <service>, …" or "error"), and its last success when the result is ok. Unreachable
    raised outside a stage (discovery, a wait check) ends the run; either way the exit code stays 0. Any other
    exception is recorded as "error" and left to the caller, unlogged."""
    _set_meta(ctx, "last_start", now())
    result = "error"
    services = set()
    try:
        release_own_claims(ctx)
        body(ctx)
    except Unreachable as e:
        services.add(e.service)
        log.warning("%s: unreachable (%s), run ended", ctx.part, e)
        result = "unreachable"
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
        if isinstance(outcome, NeedsHuman):
            needs_human(ctx.conn, "chapter", chapter["id"], outcome.reason)
        elif at_limit:
            needs_human(ctx.conn, "chapter", chapter["id"], f"{outcome.error} (after {limit} attempts)")
        if isinstance(outcome, RECORDED) and outcome.record:
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
    if isinstance(outcome, Dropped):
        return "status = 'done', last_error = ?", (f"dropped: {outcome.reason}",), False
    return "status = 'ready'", (), False  # Unreachable: no attempt; NeedsHuman: needs_human sets the status


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
        set_meta(ctx.conn, f"{ctx.part}.{name}", value)


def set_meta(conn, key, value):
    """Store a meta value; runs inside the caller's transaction."""
    if not conn.in_transaction:
        raise RuntimeError("set_meta runs inside the caller's transaction")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn, key):
    """A meta value, or None when it was never stored."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]
