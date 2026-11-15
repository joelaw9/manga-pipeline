"""Workflow rules (SPEC "Parts", "State", "Chapter stages", "Needs human"): scope, claims, outcomes, waits,
attempts, needs_human and priority.

The only module that writes chapter.stage, status, waiting_on, wait_data, attempts and claimed_by, and series.status.
A part claims a chapter in 1 short transaction, runs its stage with no transaction open, and records the outcome in
another, guarded so a row a person changed meanwhile keeps their change.
"""

import json
import logging
import os
import socket
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from manga_pipeline import db, lock
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.errors import SHARE_ERRNOS, SHARE_WINERRORS, Unreachable, dropped_share, exists  # noqa: F401

log = logging.getLogger(__name__)
STAGES = ("discover", "validate", "junk", "convert", "comicinfo", "publish", "confirm")
PART_STAGES = {
    "intake": ("discover", "validate"),
    "process": ("junk", "convert"),
    "export": ("comicinfo", "publish", "confirm"),
}
# The scope filter of every work-picking query, on series s: in_scope's rule in SQL, with the named parameters of
# scope_params. tests/test_workflow.py runs both forms over 1 table of cases.
SCOPE = """(s.kavita_library_id IS NULL OR s.kavita_library_id = %(scope_library)s::bigint)
    AND (cardinality(%(scope_only)s::text[]) = 0 OR s.name = ANY(%(scope_only)s::text[]))"""
# A junk-stage chapter {c} holds a current junk scan (SPEC "Junk decisions"): same series, same content digest, and
# made with logo detection (a scan without "marks" is not current). The one test of a current scan.
SCANNED = """EXISTS (SELECT 1 FROM junk_scan js WHERE js.chapter_id = {c}.id AND js.series_id = {c}.series_id
    AND js.content_digest = {c}.content_digest AND jsonb_typeof(js.scan::jsonb -> 'marks') = 'array')"""
# A chapter {c} that failed less than RETRY_PAUSE ago is claimed by no machine (SPEC "Chapter stages").
RETRY_PAUSE = timedelta(minutes=5)
NOT_PAUSED = "({c}.retry_after IS NULL OR {c}.retry_after <= %(now)s)"
# A stored path holding a name Windows cannot use over the share (SPEC "Parts"): a Windows machine skips it.
WINDOWS_UNUSABLE = r"""([<>:"|?*]|[. ](/|$))"""


def owner(part):
    """Who a part's claims and run lock name (SPEC "State", "Run lock"): process runs once per machine, so its owner
    carries the host name; intake and export run once in the fleet."""
    return f"{part}@{socket.gethostname()}" if part == "process" else part


def claim_name(chapter):
    """<chapter id>-<claim count>: the name a claimed chapter's scratch folder and staged file carry, so 2 claims of 1
    chapter never share a file (SPEC "State")."""
    return f"{chapter['id']}-{chapter['claim_count']}"


CLAIM_NAME = r"\d+-\d+"  # what claim_name gives, for the start-of-run clean-ups


class ClaimLost(RuntimeError):
    """The run no longer holds the chapter's claim (it was freed while the run slept or lost its connection): the
    chapter ends with no outcome and no attempt (SPEC "State")."""


@dataclass(frozen=True)
class Context:
    cfg: dict
    conn: Any  # the command's connection (cli.main), holding its run lock
    part: str
    # Who this run's claims name (owner(part) when None).
    owner: str = None
    # Chapters that failed in this run: tried again by a later run, so 1 run never spends all their attempts.
    failed: set = field(default_factory=set)
    # {stage: service} for the stages whose service was unreachable in this run: not claimed again until a later run.
    unreachable: dict = field(default_factory=dict)
    # Outcomes of this run by kind: done, waiting, failed.
    counts: Counter = field(default_factory=Counter)
    # The run's 1 client of each service (SPEC "Services"): what it learns holds for the run. None = built from cfg.
    suwayomi: Any = None
    kavita: Any = None
    # The metadata sources' clients (metadata.check.Sources); intake builds them when None.
    sources: Any = None

    def __post_init__(self):
        if self.owner is None:
            object.__setattr__(self, "owner", owner(self.part))
        if self.suwayomi is None:
            object.__setattr__(self, "suwayomi", suwayomi_client.from_config(self.cfg))
        if self.kavita is None:
            object.__setattr__(self, "kavita", kavita_client.from_config(self.cfg))


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
    (kavita_library_id None: a new series), and series.only is empty or names it. SQL's form is SCOPE."""
    if kavita_library_id is not None and kavita_library_id != cfg["kavita"]["library_id"]:
        return False
    only = cfg["series"]["only"]
    return not only or series_name in only


def scope_params(cfg):
    """The parameters SCOPE names (series.only holds text: preflight's workflow settings check)."""
    return {"scope_library": cfg["kavita"]["library_id"], "scope_only": list(cfg["series"]["only"])}


def now(conn):
    """The time the program stores: the database server's clock (SPEC "State")."""
    return db.now(conn)


def after(stamp, delta):
    """A stored time plus delta, in the stored form."""
    return (datetime.fromisoformat(stamp) + delta).isoformat(timespec="seconds")


def add_chapter(conn, series_id, identity, download_path, suwayomi_chapter_id, source, source_url):
    """Record a discovered download as a chapter ready at the first stage; runs inside the caller's transaction."""
    if not db.in_transaction(conn):
        raise RuntimeError("add_chapter runs inside the caller's transaction")
    stamp = now(conn)
    conn.execute(
        """INSERT INTO chapter (series_id, kind, identity, download_path, suwayomi_chapter_id, source, source_url,
            stage, status, discovered_at, updated_at)
        VALUES (%s, 'download', %s, %s, %s, %s, %s, %s, 'ready', %s, %s)""",
        (series_id, identity, download_path, suwayomi_chapter_id, source, source_url, STAGES[0], stamp, stamp),
    )


def claim(ctx, stages, series_id=None):
    """Claim the next ready chapter at one of stages, by priority: series Kavita knows before new series, then the
    oldest discovery, then id. Chapters of a needs_human series, out of scope, failed in this run, or at a stage whose
    service was unreachable in this run are not picked; nor is a convert-stage chapter while its series has a ready
    junk-stage chapter (a series' junk is decided before any of it converts), nor a scanned junk-stage chapter while
    its series has a ready junk-stage chapter not yet scanned (every one is scanned before any is decided; a scan is
    current while its series and content digest are the chapter's). Nor is a chapter of a series in which any chapter
    is claimed at this part's stages, or of a series sharing that series' rules key (machines work different series),
    nor one that failed less than RETRY_PAUSE ago. In the same transaction the claims of every other owner of this
    part whose run lock is no longer held are freed first (SPEC "State"). Returns the claimed row (its claim_count
    is this claim's), or None when nothing is ready."""
    stages = [s for s in stages if s not in ctx.unreachable]
    with db.transaction(ctx.conn):
        _free_dead_claims(ctx)
        params = _params(
            ctx,
            stages=stages,
            series_id=series_id,
            failed=sorted(ctx.failed),
            part_stages=list(PART_STAGES[ctx.part]),
            windows=os.name == "nt",
            unusable=WINDOWS_UNUSABLE,
        )
        return ctx.conn.execute(
            f"""UPDATE chapter SET status = 'claimed', claimed_by = %(owner)s, claim_count = claim_count + 1,
                updated_at = %(now)s
            WHERE id = (
                SELECT c.id FROM chapter c JOIN series s ON s.id = c.series_id
                WHERE c.status = 'ready' AND c.stage = ANY(%(stages)s::text[])
                    AND s.status = 'ok' AND {SCOPE}
                    AND (%(series_id)s::bigint IS NULL OR c.series_id = %(series_id)s::bigint)
                    AND c.id <> ALL(%(failed)s::bigint[]) AND {NOT_PAUSED.format(c="c")}
                    AND NOT (%(windows)s AND (coalesce(c.download_path, '') ~ %(unusable)s
                        OR coalesce(s.folder, '') ~ %(unusable)s))
                    AND NOT EXISTS (
                        SELECT 1 FROM chapter o JOIN series os ON os.id = o.series_id
                        WHERE o.status = 'claimed' AND o.stage = ANY(%(part_stages)s::text[])
                            AND (o.series_id = c.series_id OR os.rules_key = s.rules_key))
                    AND NOT (c.stage = 'convert' AND EXISTS (
                        SELECT 1 FROM chapter j
                        WHERE j.series_id = c.series_id AND j.stage = 'junk' AND j.status = 'ready'
                            AND j.id <> ALL(%(failed)s::bigint[]) AND {NOT_PAUSED.format(c="j")}))
                    AND NOT (c.stage = 'junk' AND {SCANNED.format(c="c")} AND EXISTS (
                        SELECT 1 FROM chapter u
                        WHERE u.series_id = c.series_id AND u.stage = 'junk' AND u.status = 'ready'
                            AND u.id <> ALL(%(failed)s::bigint[]) AND {NOT_PAUSED.format(c="u")}
                            AND NOT {SCANNED.format(c="u")}))
                ORDER BY s.origin = 'new', c.discovered_at, c.id
                LIMIT 1)
            RETURNING *""",
            params,
        ).fetchone()


def _free_dead_claims(ctx):
    """Inside the claim's transaction: the claims of every other owner of this part whose run lock is not held go
    back to ready, attempts unchanged. None with lock.enabled false: no run holds a lock then (SPEC "State")."""
    if not ctx.cfg["lock"]["enabled"]:
        return
    freed = ctx.conn.execute(
        f"""UPDATE chapter c SET status = 'ready', claimed_by = NULL, updated_at = %(now)s
        WHERE c.status = 'claimed' AND c.claimed_by <> %(owner)s
            AND (c.claimed_by = %(part)s OR c.claimed_by LIKE %(part)s || '@%%')
            AND NOT {lock.HELD_SQL.format(name="c.claimed_by")}
        RETURNING c.id, c.claimed_by""",
        _params(ctx),
    ).fetchall()
    for row in freed:
        log.info("%s: freed chapter %s: its owner %s holds no run lock", ctx.owner, row["id"], row["claimed_by"])


def series_with_ready(ctx, stages):
    """Ids of the in-scope, not needs_human series with a ready chapter at one of stages, in priority order."""
    rows = ctx.conn.execute(
        f"""SELECT c.series_id FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'ready' AND c.stage = ANY(%(stages)s::text[]) AND s.status = 'ok' AND {SCOPE}
            AND {NOT_PAUSED.format(c="c")}
        GROUP BY c.series_id, s.origin
        ORDER BY s.origin = 'new', MIN(c.discovered_at), MIN(c.id)""",
        _params(ctx, stages=list(stages)),
    )
    return [row[0] for row in rows]


def work(ctx, chapter, stage):
    """Run stage(ctx, chapter) on a claimed chapter, record its outcome and log it. Any exception from the stage is
    Failed, except Unreachable (and an OSError with a SHARE_ERRNOS errno, which is Unreachable("share")): that leaves
    the chapter ready with its attempts unchanged, and the chapter's stage is not claimed again in this run. Returns
    the outcome (the Unreachable for that case). An outcome dropped because a person changed the row meanwhile is
    neither logged as an outcome nor counted."""
    if db.in_transaction(ctx.conn):
        raise RuntimeError("a stage must run with no transaction open")
    try:
        with share_errors():
            outcome = stage(ctx, chapter)
    except ClaimLost as e:
        log.warning("%s: %s: %s; nothing recorded", ctx.owner, _name(chapter), e)
        return e
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
        WHERE c.status = 'waiting' AND c.waiting_on = %(on)s AND s.status = 'ok' AND {SCOPE} ORDER BY c.id""",
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
                    "SELECT 1 FROM chapter WHERE id = %s AND status = 'waiting' AND waiting_on = %s",
                    (chapter["id"], on),
                ).fetchone()
                if still:
                    needs_human(ctx.conn, "chapter", chapter["id"], ended.reason)
        elif ended:
            with db.transaction(ctx.conn):
                ctx.conn.execute(
                    """UPDATE chapter SET status = 'ready', waiting_on = NULL, wait_data = NULL, updated_at = %s
                    WHERE id = %s AND status = 'waiting' AND waiting_on = %s""",
                    (now(ctx.conn), chapter["id"], on),
                )


def charge_waits(ctx, on, reason, waited_since, at):
    """1 attempt for each in-scope chapter of an ok series waiting on `on` whose wait was recorded at or before
    waited_since (a wait renewed at a cost, SPEC "Publishing"); the charge stamps updated_at with `at`, so a charged
    chapter's wait counts from then. A chapter reaching the attempt limit becomes needs_human. Runs inside the caller's
    transaction; returns how many were charged."""
    if not db.in_transaction(ctx.conn):
        raise RuntimeError("charge_waits runs inside the caller's transaction")
    limit = ctx.cfg["workflow"]["attempt_limit"]
    rows = ctx.conn.execute(
        f"""UPDATE chapter SET attempts = attempts + 1, last_error = %(reason)s, updated_at = %(at)s
        WHERE status = 'waiting' AND waiting_on = %(on)s AND updated_at <= %(since)s
            AND series_id IN (SELECT s.id FROM series s WHERE s.status = 'ok' AND {SCOPE})
        RETURNING id, attempts""",
        _params(ctx, reason=reason, on=on, since=waited_since, at=at),
    ).fetchall()
    for row in rows:
        if row["attempts"] >= limit:
            needs_human(ctx.conn, "chapter", row["id"], f"{reason} (after {limit} attempts)")
    return len(rows)


def release_own_claims(ctx):
    """Free the claims a killed run of this owner left, in scope or not: back to ready at their stage, attempts
    unchanged (this run holds the owner's lock, so they are dead). Not when lock.enabled is false: another live run
    of the owner may hold them."""
    if not ctx.cfg["lock"]["enabled"]:
        return
    with db.transaction(ctx.conn):
        released = ctx.conn.execute(
            """UPDATE chapter SET status = 'ready', claimed_by = NULL, updated_at = %(now)s
            WHERE status = 'claimed' AND claimed_by = %(owner)s""",
            _params(ctx),
        ).rowcount
    if released:
        log.info("%s: released %d claim(s) left by an earlier run", ctx.owner, released)


def holds_claim(ctx, chapter):
    """Raise ClaimLost unless this run still holds the chapter's claim; inside the caller's transaction or alone.
    Called immediately before an act that cannot be undone outside the database (SPEC "State")."""
    held = ctx.conn.execute(
        "SELECT 1 FROM chapter WHERE id = %s AND status = 'claimed' AND claimed_by = %s AND claim_count = %s",
        (chapter["id"], ctx.owner, chapter["claim_count"]),
    ).fetchone()
    if held is None:
        raise ClaimLost(f"the claim on chapter {chapter['id']} was freed or taken by another run")


def needs_human(conn, kind, item_id, reason):
    """The single place a chapter or a series becomes needs_human (kind "chapter" or "series"); runs inside the
    caller's transaction."""
    if not db.in_transaction(conn):
        raise RuntimeError("needs_human runs inside the caller's transaction")
    if kind == "chapter":
        conn.execute(
            """UPDATE chapter SET status = 'needs_human', claimed_by = NULL, waiting_on = NULL, wait_data = NULL,
                last_error = %s, updated_at = %s
            WHERE id = %s""",
            (reason, now(conn), item_id),
        )
    elif kind == "series":
        conn.execute("UPDATE series SET status = 'needs_human', status_reason = %s WHERE id = %s", (reason, item_id))
    else:
        raise ValueError(f"needs_human: kind must be chapter or series, got {kind!r}")
    log.warning("needs human: %s %s: %s", kind, item_id, reason)  # Phase 8 adds the Discord message


class Refused(ValueError):
    """A person's command that does not apply to the item it names; nothing was changed."""


def retry_chapter(conn, chapter_id):
    """A needs_human chapter becomes ready again at the stage where it stopped, with its attempts at 0."""
    with db.transaction(conn):
        row = conn.execute("SELECT status FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
        if row is None:
            raise Refused(f"no chapter {chapter_id}")
        if row["status"] != "needs_human":
            raise Refused(f"chapter {chapter_id} is {row['status']}, not needs_human")
        _ready_again(conn, "id = %s", chapter_id)


def retry_series(conn, series_id):
    """A series becomes ok (reason cleared), and its needs_human chapters ready again at their stages, attempts 0.
    Refused when neither the series nor any of its chapters is needs_human."""
    with db.transaction(conn):
        row = conn.execute("SELECT status FROM series WHERE id = %s", (series_id,)).fetchone()
        if row is None:
            raise Refused(f"no series {series_id}")
        chapters = _ready_again(conn, "series_id = %s AND status = 'needs_human'", series_id)
        if row["status"] != "needs_human" and not chapters:
            raise Refused(f"series {series_id} is {row['status']} and has no needs_human chapter")
        conn.execute("UPDATE series SET status = 'ok', status_reason = NULL WHERE id = %s", (series_id,))


def move_chapters(conn, from_series, to_series):
    """Move a series' chapters to another (series link merging a held row; inside the caller's transaction). An open
    chapter past junk goes back to junk, ready, attempts 0, so the new series' baseline and handled-digest check
    apply; a part holding its claim then drops its outcome."""
    junk = STAGES.index("junk")
    later = STAGES[junk + 1 :]
    conn.execute(
        f"""UPDATE chapter SET stage = 'junk', status = 'ready', waiting_on = NULL, wait_data = NULL,
            claimed_by = NULL, attempts = 0, retry_after = NULL, updated_at = %s
        WHERE series_id = %s AND status NOT IN ('done', 'needs_human')
            AND stage IN ({",".join(["%s"] * len(later))})""",
        (now(conn), from_series, *later),
    )
    conn.execute("UPDATE chapter SET series_id = %s WHERE series_id = %s", (to_series, from_series))


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
        f"""UPDATE chapter SET status = 'ready', attempts = 0, retry_after = NULL, updated_at = %s,
            redownload = CASE WHEN redownload::jsonb ->> 'original' IS NULL THEN NULL
                ELSE jsonb_set(jsonb_set(jsonb_set(redownload::jsonb, '{{tried}}', '[]'), '{{same_source_done}}',
                    'false'), '{{chosen}}', 'null')::text END
        WHERE {where}""",
        (now(conn), value),
    ).rowcount


# The stages at which a person may drop a needs_human chapter: nothing of it is staged or published yet.
DROPPABLE = ("discover", "validate", "junk")


def use_chapter(conn, chapter_id, suwayomi_chapter_id):
    """A person's chosen replacement (chapter use): a needs_human chapter at validate becomes ready with the Suwayomi
    chapter to download as its replacement (SPEC "Bad downloads")."""
    with db.transaction(conn):
        _refuse_unless_human(conn, chapter_id, ("validate",))
        conn.execute(
            """UPDATE chapter SET status = 'ready', attempts = 0, retry_after = NULL, updated_at = %s,
                redownload = jsonb_set(coalesce(redownload, '{}')::jsonb, '{chosen}', to_jsonb(%s::bigint))::text
            WHERE id = %s""",
            (now(conn), suwayomi_chapter_id, chapter_id),
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
                notes = NULL, updated_at = %s WHERE id = %s""",
            (now(conn), chapter_id),
        )
        record(conn)


def _refuse_unless_human(conn, chapter_id, stages):
    row = conn.execute("SELECT * FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
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
    _set_meta(ctx, "last_start", now(ctx.conn))
    result = "error"
    services = set()
    try:
        release_own_claims(ctx)
        body(ctx)
    except Unreachable as e:
        services.add(e.service)
        log.warning("%s: unreachable (%s), run ended", ctx.owner, e)
        result = "unreachable"
    else:
        result = "ok"
    finally:
        if result != "error":
            services |= set(ctx.unreachable.values())
            if services:
                result = f"unreachable: {', '.join(sorted(services))}"
        end = now(ctx.conn)
        _set_meta(ctx, "last_end", end)
        _set_meta(ctx, "last_result", result)
        if result == "ok":
            _set_meta(ctx, "last_success", end)
        counts = ", ".join(f"{ctx.counts[kind]} {kind}" for kind in ("done", "waiting", "failed"))
        log.info("%s: run ended: %s (%s)", ctx.owner, result, counts)


def _finish(ctx, chapter, outcome):
    """Record outcome for a chapter this run claimed (its owner and claim count), unless a person changed the row
    meanwhile or the claim was freed. Returns whether it was recorded."""
    limit = ctx.cfg["workflow"]["attempt_limit"]
    with db.transaction(ctx.conn):
        stamp = now(ctx.conn)
        set_, values, at_limit = _outcome_columns(chapter, outcome, limit, stamp)
        changed = ctx.conn.execute(
            f"UPDATE chapter SET {set_}, claimed_by = NULL, updated_at = %s "
            "WHERE id = %s AND claimed_by = %s AND claim_count = %s AND status = 'claimed'",
            (*values, stamp, chapter["id"], ctx.owner, chapter["claim_count"]),
        ).rowcount
        if not changed:
            log.warning(
                "chapter %s changed while %s worked on it, or its claim was freed; its outcome is dropped",
                chapter["id"],
                ctx.owner,
            )
            return False
        if isinstance(outcome, NeedsHuman):
            needs_human(ctx.conn, "chapter", chapter["id"], outcome.reason)
        elif at_limit:
            needs_human(ctx.conn, "chapter", chapter["id"], f"{outcome.error} (after {limit} attempts)")
        if isinstance(outcome, RECORDED) and outcome.record:
            outcome.record(ctx.conn)
    return True


def _outcome_columns(chapter, outcome, limit, stamp):
    """(SET clause, its values, whether the attempt limit is reached) for an outcome recorded at stamp. Only a
    failure sets retry_after; every other outcome clears it."""
    if isinstance(outcome, Failed):
        attempts = chapter["attempts"] + 1
        return (
            "status = 'ready', attempts = %s, last_error = %s, retry_after = %s",
            (attempts, outcome.error, after(stamp, RETRY_PAUSE)),
            attempts >= limit,
        )
    if isinstance(outcome, Done):
        last = chapter["stage"] == STAGES[-1]
        stage = chapter["stage"] if last else STAGES[STAGES.index(chapter["stage"]) + 1]
        set_, values = "stage = %s, status = %s, attempts = 0", (stage, "done" if last else "ready")
    elif isinstance(outcome, Waiting):
        set_, values = "status = 'waiting', waiting_on = %s, wait_data = %s", (outcome.on, json.dumps(outcome.data))
    elif isinstance(outcome, Dropped):
        set_, values = "status = 'done', last_error = %s, notes = NULL", (f"dropped: {outcome.reason}",)  # not listed
    else:
        set_, values = "status = 'ready'", ()  # Unreachable: no attempt; NeedsHuman: needs_human sets the status
    return f"{set_}, retry_after = NULL", values, False


@contextmanager
def share_errors():
    """An OSError with a SHARE_ERRNOS errno, or on Windows a SHARE_WINERRORS winerror, becomes Unreachable("share");
    other errors pass unchanged."""
    try:
        yield
    except OSError as e:
        if e.errno not in SHARE_ERRNOS and getattr(e, "winerror", None) not in SHARE_WINERRORS:
            raise
        raise Unreachable("share") from e


def _params(ctx, **extra):
    return {"part": ctx.part, "owner": ctx.owner, "now": now(ctx.conn), **scope_params(ctx.cfg), **extra}


def _set_meta(ctx, name, value):
    with db.transaction(ctx.conn):
        set_meta(ctx.conn, f"{ctx.owner}.{name}", value)  # 1 set of run records per part and machine


def set_meta(conn, key, value):
    """Store a meta value; runs inside the caller's transaction."""
    if not db.in_transaction(conn):
        raise RuntimeError("set_meta runs inside the caller's transaction")
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn, key):
    """A meta value, or None when it was never stored."""
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return None if row is None else row[0]
