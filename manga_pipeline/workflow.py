"""Workflow rules (SPEC "Parts", "State", "Chapter stages", "Needs human").

Scope, claims, outcomes, waits, attempts, needs_human and priority.

The only module that writes chapter.stage, status, waiting_on, wait_data, attempts and claimed_by, and series.status.
A part claims a chapter in 1 short transaction, runs its stage with no transaction open, and records the outcome in
another, guarded so a row a person changed meanwhile keeps their change.
"""

import json
import logging
import os
import re
import socket
from collections import Counter
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, NotRequired, TypedDict, final

from manga_pipeline import config, db, lock, notify, strip
from manga_pipeline.clients import discord as discord_client
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.db import sql
from manga_pipeline.errors import SHARE_ERRNOS, SHARE_WINERRORS, Unreachable
from manga_pipeline.metadata import sources as metadata_sources
from manga_pipeline.replies import Json
from manga_pipeline.replies import kavita as kavita_replies

log = logging.getLogger(__name__)
STAGES = ("discover", "validate", "junk", "convert", "comicinfo", "publish", "confirm")
PART_STAGES: dict[str, tuple[str, ...]] = {
    "intake": ("discover", "validate"),
    "process": ("junk", "convert"),
    "export": ("comicinfo", "publish", "confirm"),
}
# The scope filter of every work-picking query, on series s: in_scope's rule in SQL, with the named parameters of
# scope_params. tests/test_workflow.py runs both forms over 1 table of cases.
SCOPE = sql.SQL("""(s.kavita_library_id IS NULL OR s.kavita_library_id = %(scope_library)s::bigint)
    AND (cardinality(%(scope_only)s::text[]) = 0 OR s.name = ANY(%(scope_only)s::text[])
        OR s.alt_name = ANY(%(scope_only)s::text[]) OR s.comicinfo_series = ANY(%(scope_only)s::text[])
        OR s.localized_name = ANY(%(scope_only)s::text[]) OR s.original_name = ANY(%(scope_only)s::text[]))""")
# A series s whose chapters may be worked: not a new series waiting to be admitted (SPEC "Series identification": a
# held one's chapters are recorded and wait for its name and folder).
ADMITTED = sql.SQL("NOT (s.origin = 'new' AND s.admitted_at IS NULL)")
# A junk-stage chapter {c} holds a current junk scan (SPEC "Junk decisions"): same series, same content digest, made
# with logo detection (a scan without "marks" is not current) and under the series' layout (SPEC "State": an override
# file may change it). The one test of a current scan. Fill {c} with sql.Identifier(alias): a plain str becomes a
# quoted literal.
SCANNED = sql.SQL("""EXISTS (SELECT 1 FROM junk_scan js CROSS JOIN LATERAL (SELECT js.scan::jsonb AS doc) j
    JOIN series_conversion sc ON sc.series_id = js.series_id
    WHERE js.chapter_id = {c}.id AND js.series_id = {c}.series_id AND js.content_digest = {c}.content_digest
    AND jsonb_typeof(j.doc -> 'marks') = 'array' AND j.doc ->> 'layout' = sc.layout)""")
# A chapter {c} that replaces an adopted file (SPEC "State"): it names a published file of origin library, as its
# conversion or a better copy that took it. Picked after every other chapter; it holds back only other such chapters in
# the claim gates. Fill {c} with sql.Identifier(alias).
REPLACES_ADOPTED = sql.SQL("""EXISTS (SELECT 1 FROM published_file adopted_file
    WHERE adopted_file.id = {c}.published_file_id AND adopted_file.origin = 'library')""")
# A chapter {c} that failed less than RETRY_PAUSE ago is claimed by no machine (SPEC "Chapter stages"). Fill {c} with
# sql.Identifier(alias).
RETRY_PAUSE = timedelta(minutes=5)
NOT_PAUSED = sql.SQL("({c}.retry_after IS NULL OR {c}.retry_after <= %(now)s)")
# A stored path holding a name Windows cannot use over the share (SPEC "Parts"): a Windows machine skips it.
WINDOWS_UNUSABLE = r"""([<>:"|?*]|[. ](/|$))"""


def unusable_here(stored: str) -> bool:
    """Return whether this machine cannot open a stored path: on Windows, one WINDOWS_UNUSABLE matches.

    The claim query tests the same in SQL (its `unusable` parameter).
    """
    return os.name == "nt" and re.search(WINDOWS_UNUSABLE, stored) is not None


def owner(part: str) -> str:
    """Return who a part's claims and run lock name (SPEC "State", "Run lock").

    process runs once per machine, so its owner carries the host name; intake and export run once in the fleet.
    """
    return f"{part}@{socket.gethostname()}" if part == "process" else part


def claim_name(chapter: db.Row) -> str:
    """Return <chapter id>-<claim count>: the name a claimed chapter's scratch folder and staged file carry.

    So 2 claims of 1 chapter never share a file (SPEC "State").
    """
    return f"{chapter['id']}-{chapter['claim_count']}"


CLAIM_NAME = r"\d+-\d+"  # what claim_name gives, for the start-of-run clean-ups


class ClaimLost(RuntimeError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """The run no longer holds the chapter's claim (it was freed while the run slept or lost its connection).

    The chapter ends with no outcome and no attempt (SPEC "State").
    """


@dataclass
class KavitaCache:
    """What a run learnt from Kavita once (SPEC "Publishing"): the library listing, each new series' id search."""

    listing: list[kavita_replies.Series] | None = None  # the library's series (kavita_link.listing), None: not read yet
    attach: dict[int, int | None] = field(default_factory=dict)  # {series id: its Kavita id once searched for}


@dataclass(frozen=True)
class Context:
    """A run's state: context builds it from the config.

    A caller that holds its clients (a test's fakes) gives every field.
    """

    cfg: config.Config
    conn: db.Connection  # the command's connection (cli.main), holding its run lock
    part: str
    owner: str  # who this run's claims name
    # The run's 1 client of each service (SPEC "Services"): what it learns holds for the run.
    suwayomi: suwayomi_client.Suwayomi
    kavita: kavita_client.Kavita
    # The metadata sources' clients: intake's check and validate's volume covers.
    sources: metadata_sources.Sources
    # Discord, None when off: export sends the queued reports through it (SPEC "Needs human").
    discord: discord_client.Discord | None
    # Chapters that failed in this run: tried again by a later run, so 1 run never spends all their attempts.
    failed: set[int] = field(default_factory=set)
    # {stage: service} for the stages whose service was unreachable in this run: not claimed again until a later run.
    unreachable: dict[str, str] = field(default_factory=dict)
    # Outcomes of this run by kind: done, waiting, failed.
    counts: Counter[str] = field(default_factory=Counter)
    # What the run learnt from Kavita once: the library listing, each new series' id search (SPEC "Publishing").
    kavita_cache: KavitaCache = field(default_factory=KavitaCache)


def context(cfg: config.Config, conn: db.Connection, part: str) -> Context:
    """Build the part's run state from cfg: its owner and its 1 client of each service and metadata source."""
    return Context(
        cfg,
        conn,
        part,
        owner=owner(part),
        suwayomi=suwayomi_client.from_config(cfg),
        kavita=kavita_client.from_config(cfg),
        sources=metadata_sources.new_sources(),
        discord=discord_client.from_config(cfg),
    )


@dataclass(frozen=True)
class Done:
    """The stage is done: the chapter is ready at its next stage, attempts 0, or done after its last stage."""

    # Writes that belong with the outcome (e.g. the published_file row of a rename): run in the outcome's transaction.
    record: Callable[[db.Connection], object] | None = None  # its result is ignored


@dataclass(frozen=True)
class Waiting:
    """The chapter waits on `on`, with its wait data; end_waits ends the wait."""

    on: str
    data: dict[str, Json] = field(default_factory=dict)
    record: Callable[[db.Connection], object] | None = None  # its result is ignored


@dataclass(frozen=True)
class Ready:
    """The chapter stays ready at its stage, with no attempt, and may be claimed again in this run.

    The junk stage after a scan (SPEC "Chapter stages").
    """

    record: Callable[[db.Connection], object] | None = None  # its result is ignored


@dataclass(frozen=True)
class Failed:
    """The stage failed: 1 attempt and its error recorded.

    The chapter is ready again after RETRY_PAUSE, or needs_human once it reaches workflow.attempt_limit.
    """

    error: str


@dataclass(frozen=True)
class Dropped:
    """The chapter leaves the pipeline where it stands, never published (status done at its stage)."""

    reason: str
    record: Callable[[db.Connection], object] | None = None  # its result is ignored


@dataclass(frozen=True)
class NeedsHuman:
    """No further attempt can change the result: the chapter is needs_human at once."""

    reason: str
    record: Callable[[db.Connection], object] | None = None  # its result is ignored


OUTCOMES = Done | Waiting | Failed | Dropped | NeedsHuman | Ready
RECORDED = Done | Dropped | Waiting | Ready | NeedsHuman  # outcomes that may carry a record
type Stage = Callable[[Context, db.Row], OUTCOMES]  # a stage's run(ctx, chapter)
type WaitCheck = Callable[[Context, db.Row], bool | NeedsHuman]  # whether a chapter's wait is over (end_waits)


class ScopeParams(TypedDict):
    """The parameters SCOPE names: the Kavita library and series.only."""

    scope_library: int
    scope_only: list[str]


def in_scope(cfg: config.Config, series_names: Iterable[str], kavita_library_id: int | None) -> bool:
    """Return whether a series is worked.

    Kavita holds it in the library kavita.library_id or does not hold it yet (kavita_library_id None: a new series),
    and series.only is empty or names it by any of series_names. SQL's form is SCOPE.
    """
    if kavita_library_id is not None and kavita_library_id != cfg["kavita"]["library_id"]:
        return False
    only = cfg["series"]["only"]
    return not only or any(name in only for name in series_names)


def row_names(row: db.Row) -> list[str]:
    """Return the names series.only may give a series row by (SCOPE's columns).

    Its name, a hand-placed folder's ComicInfo Series, its ComicInfo Series name and its Kavita localized and original
    names.
    """
    names = ("name", "alt_name", "comicinfo_series", "localized_name", "original_name")
    return [row[n] for n in names if row[n]]


def row_in_scope(cfg: config.Config, row: db.Row) -> bool:
    """Return whether a series row is worked: in_scope over its row_names and its Kavita library id."""
    return in_scope(cfg, row_names(row), row["kavita_library_id"])


def scope_params(cfg: config.Config) -> ScopeParams:
    """Return the parameters SCOPE names (series.only holds text: preflight's workflow settings check)."""
    return {"scope_library": cfg["kavita"]["library_id"], "scope_only": list(cfg["series"]["only"])}


def now(conn: db.Connection) -> str:
    """Return the time the program stores: the database server's clock (SPEC "State")."""
    return db.now(conn)


def after(stamp: str, delta: timedelta) -> str:
    """Return a stored time plus delta, in the stored form."""
    return (datetime.fromisoformat(stamp) + delta).isoformat(timespec="seconds")


@dataclass(frozen=True)
class NewChapter:
    """A discovered download, as add_chapter records it. The Suwayomi fields are None for a hand-placed one."""

    series_id: int
    identity: str  # names.identity's JSON
    download_path: str  # stored form
    suwayomi_chapter_id: int | None
    source: str | None  # the Suwayomi source's display name
    source_url: str | None  # the chapter's page at its source
    published_file_id: int | None = None  # the published file it replaces: an adopted file's conversion (SPEC "State")


def add_chapter(conn: db.Connection, new: NewChapter) -> int | None:
    """Record a discovered download as a chapter ready at the first stage; runs inside the caller's transaction.

    Returns its id; None when it names a published file another open chapter already names (nothing inserted).
    """
    if not db.in_transaction(conn):
        msg = "add_chapter runs inside the caller's transaction"
        raise RuntimeError(msg)
    stamp = now(conn)
    row = conn.execute(
        """INSERT INTO chapter (series_id, kind, identity, download_path, suwayomi_chapter_id, source, source_url,
            published_file_id, stage, status, discovered_at, updated_at)
        VALUES (%s, 'download', %s, %s, %s, %s, %s, %s, %s, 'ready', %s, %s)
        ON CONFLICT (published_file_id) WHERE published_file_id IS NOT NULL AND status != 'done' DO NOTHING
        RETURNING id""",
        (
            new.series_id,
            new.identity,
            new.download_path,
            new.suwayomi_chapter_id,
            new.source,
            new.source_url,
            new.published_file_id,
            STAGES[0],
            stamp,
            stamp,
        ),
    ).fetchone()
    return None if row is None else row[0]


def claim(ctx: Context, stages: Iterable[str], series_id: int | None = None) -> db.Row | None:
    """Claim the next ready chapter at one of stages, by priority.

    A chapter that replaces an adopted file (REPLACES_ADOPTED) after every other, then series Kavita knows before new
    series, then the oldest discovery, then id. Chapters of a needs_human series, out of scope, failed in this run, or
    at a stage whose service was unreachable in this run are not picked; nor is a convert-stage chapter while its
    series has a ready junk-stage chapter (a series' junk is decided before any of it converts), nor a scanned
    junk-stage chapter while its series has a ready junk-stage chapter not yet scanned (every one is scanned before any
    is decided; a scan is current while its series and content digest are the chapter's). In both of those gates a
    chapter that does not replace an adopted file ignores those that do: a download is decided and converted without
    waiting for its series' conversions to be scanned, while a conversion waits for every chapter of its series. A
    new series' chapter waits at validate while its series has no naming basis and any of its chapters is at discover
    (the basis is chosen over the whole batch), and at junk while its series has no layout, fewer than
    strip.SAMPLE_CHAPTERS of its chapters past validate (with a download path this machine can use) and any chapter
    still at discover or validate (SPEC "State"). Nor is a chapter of a series in which any chapter is claimed at this
    part's stages, or of a series sharing that series' rules key (machines work different series), nor one that failed
    less than RETRY_PAUSE ago. In the same transaction the claims of every other owner of this part whose run lock is no
    longer held are freed first (SPEC "State"). Returns the claimed row (its claim_count is this claim's), or None when
    nothing is ready.
    """
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
            sample_chapters=strip.SAMPLE_CHAPTERS,
        )
        return ctx.conn.execute(
            sql.SQL("""UPDATE chapter SET status = 'claimed', claimed_by = %(owner)s, claim_count = claim_count + 1,
                updated_at = %(now)s
            WHERE id = (
                SELECT c.id FROM chapter c JOIN series s ON s.id = c.series_id
                WHERE c.status = 'ready' AND c.stage = ANY(%(stages)s::text[])
                    AND s.status = 'ok' AND {scope} AND {admitted}
                    AND (%(series_id)s::bigint IS NULL OR c.series_id = %(series_id)s::bigint)
                    AND c.id <> ALL(%(failed)s::bigint[]) AND {c_not_paused}
                    AND NOT (%(windows)s AND (coalesce(c.download_path, (
                            SELECT f.library_path FROM published_file f WHERE f.id = c.published_file_id), '')
                            ~ %(unusable)s
                        OR coalesce(s.folder, '') ~ %(unusable)s))
                    AND NOT EXISTS (
                        SELECT 1 FROM chapter o JOIN series os ON os.id = o.series_id
                        WHERE o.status = 'claimed' AND o.stage = ANY(%(part_stages)s::text[])
                            AND (o.series_id = c.series_id OR os.rules_key = s.rules_key))
                    AND NOT (c.stage = 'convert' AND EXISTS (
                        SELECT 1 FROM chapter j
                        WHERE j.series_id = c.series_id AND j.stage = 'junk' AND j.status = 'ready'
                            AND j.id <> ALL(%(failed)s::bigint[]) AND {j_not_paused}
                            AND ({c_adopted} OR NOT {j_adopted})))
                    AND NOT (c.stage = 'junk' AND {c_scanned} AND EXISTS (
                        SELECT 1 FROM chapter u
                        WHERE u.series_id = c.series_id AND u.stage = 'junk' AND u.status = 'ready'
                            AND u.id <> ALL(%(failed)s::bigint[]) AND {u_not_paused}
                            AND NOT {u_scanned} AND ({c_adopted} OR NOT {u_adopted})))
                AND NOT (c.stage = 'validate' AND s.origin = 'new' AND s.naming_basis IS NULL AND EXISTS (
                    SELECT 1 FROM chapter d WHERE d.series_id = c.series_id AND d.stage = 'discover'
                        AND d.status IN ('ready', 'claimed')))
                AND NOT (c.stage = 'junk' AND s.origin = 'new'
                    AND NOT EXISTS (SELECT 1 FROM series_conversion sc WHERE sc.series_id = s.id)
                    AND (SELECT count(*) FROM chapter p
                        WHERE p.series_id = c.series_id AND p.kind = 'download' AND p.status <> 'done'
                            AND p.stage NOT IN ('discover', 'validate')
                            AND NOT (%(windows)s AND coalesce(p.download_path, '') ~ %(unusable)s))
                        < %(sample_chapters)s
                    AND EXISTS (
                        SELECT 1 FROM chapter d WHERE d.series_id = c.series_id
                            AND d.stage IN ('discover', 'validate') AND d.status IN ('ready', 'claimed')))
                ORDER BY {c_adopted}, s.origin = 'new' AND s.kavita_series_id IS NULL, c.discovered_at, c.id
                LIMIT 1)
            RETURNING *""").format(
                c_adopted=REPLACES_ADOPTED.format(c=sql.Identifier("c")),
                j_adopted=REPLACES_ADOPTED.format(c=sql.Identifier("j")),
                u_adopted=REPLACES_ADOPTED.format(c=sql.Identifier("u")),
                scope=SCOPE,
                admitted=ADMITTED,
                c_not_paused=NOT_PAUSED.format(c=sql.Identifier("c")),
                j_not_paused=NOT_PAUSED.format(c=sql.Identifier("j")),
                u_not_paused=NOT_PAUSED.format(c=sql.Identifier("u")),
                c_scanned=SCANNED.format(c=sql.Identifier("c")),
                u_scanned=SCANNED.format(c=sql.Identifier("u")),
            ),
            params,
        ).fetchone()


def _free_dead_claims(ctx: Context) -> None:
    """Free the claims of every other owner of this part whose run lock is not held, inside the claim's transaction.

    They go back to ready, attempts unchanged. None with lock.enabled false: no run holds a lock then (SPEC "State").
    """
    if not ctx.cfg["lock"]["enabled"]:
        return
    freed = ctx.conn.execute(
        sql.SQL("""UPDATE chapter c SET status = 'ready', claimed_by = NULL, updated_at = %(now)s
        WHERE c.status = 'claimed' AND c.claimed_by <> %(owner)s
            AND (c.claimed_by = %(part)s OR c.claimed_by LIKE %(part)s || '@%%')
            AND NOT {held}
        RETURNING c.id, c.claimed_by""").format(held=lock.held_sql(sql.Identifier("c", "claimed_by"))),
        _params(ctx),
    ).fetchall()
    for row in freed:
        log.info("%s: freed chapter %s: its owner %s holds no run lock", ctx.owner, row["id"], row["claimed_by"])


def series_with_ready(ctx: Context, stages: Iterable[str]) -> list[int]:
    """Ids of the in-scope, not needs_human series with a ready chapter at one of stages, in priority order.

    A series whose ready chapters all replace an adopted file after every other, then claim's order.
    """
    rows = ctx.conn.execute(
        sql.SQL("""SELECT c.series_id FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'ready' AND c.stage = ANY(%(stages)s::text[]) AND s.status = 'ok' AND {scope}
            AND {admitted} AND {not_paused}
        GROUP BY c.series_id, s.origin, s.kavita_series_id
        ORDER BY bool_and({adopted}), s.origin = 'new' AND s.kavita_series_id IS NULL, MIN(c.discovered_at),
            MIN(c.id)""").format(
            scope=SCOPE,
            admitted=ADMITTED,
            not_paused=NOT_PAUSED.format(c=sql.Identifier("c")),
            adopted=REPLACES_ADOPTED.format(c=sql.Identifier("c")),
        ),
        _params(ctx, stages=list(stages)),
    )
    return [row[0] for row in rows]


def work(ctx: Context, chapter: db.Row, stage: Stage) -> OUTCOMES | ClaimLost | Unreachable:
    """Run stage(ctx, chapter) on a claimed chapter, record its outcome and log it.

    Any exception from the stage is Failed, except Unreachable (and a share error, a full disk included, which is
    Unreachable("share"): share_errors): that leaves the chapter ready with its attempts unchanged, and the chapter's
    stage is not claimed again in this run. Returns the outcome (the Unreachable for that case). An outcome dropped
    because a person changed the row meanwhile is neither logged as an outcome nor counted.
    """
    if db.in_transaction(ctx.conn):
        msg = "a stage must run with no transaction open"
        raise RuntimeError(msg)
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
    except Exception as e:  # noqa: BLE001  # the stage boundary: whatever a stage raises is its chapter's failure
        outcome = Failed(f"{type(e).__name__}: {e}")
    if not isinstance(outcome, OUTCOMES):
        msg = f"stage {stage.__module__}.{stage.__name__} returned {outcome!r}, not an outcome"
        raise TypeError(msg)
    outcome, recorded = _record_outcome(ctx, chapter, outcome)
    if recorded:  # else _finish logged that it was dropped
        _count(ctx, chapter, outcome)
    return outcome


def _record_outcome(ctx: Context, chapter: db.Row, outcome: OUTCOMES) -> tuple[OUTCOMES, bool]:
    """Record the outcome; return (the outcome recorded, whether it was: false when dropped).

    An outcome whose own writes fail is recorded as Failed in its place.
    """
    try:
        return outcome, _finish(ctx, chapter, outcome)
    except Exception as e:
        if not (isinstance(outcome, RECORDED) and outcome.record):
            raise
        failed = Failed(f"recording the outcome: {type(e).__name__}: {e}")
        return failed, _finish(ctx, chapter, failed)


def _count(ctx: Context, chapter: db.Row, outcome: OUTCOMES) -> None:
    """Count a recorded outcome in the run and log its 1 line (needs_human logged its own reason)."""
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


def _name(chapter: db.Row) -> str:
    """How a log line names a chapter: id, series, identity and stage."""
    return f"chapter {chapter['id']} (series {chapter['series_id']}, {chapter['identity']}, at {chapter['stage']})"


def drain(ctx: Context, stages: Mapping[str, Stage], series_id: int | None = None) -> int:
    """Work chapters until none is ready at the stages of `stages` ({stage: stage function}).

    Claims again by priority after each one. Returns how many ended Done.
    """
    done = 0
    while chapter := claim(ctx, tuple(stages), series_id):
        done += isinstance(work(ctx, chapter, stages[chapter["stage"]]), Done)
    return done


def end_waits(ctx: Context, on: str, holds: WaitCheck) -> None:
    """Make every in-scope chapter waiting on `on` ready again at its stage when holds(ctx, chapter) is true.

    Or needs_human when it returns a NeedsHuman (no further wait can help). The chapters of a needs_human series keep
    waiting.
    """
    rows = ctx.conn.execute(
        sql.SQL("""SELECT c.* FROM chapter c JOIN series s ON s.id = c.series_id
        WHERE c.status = 'waiting' AND c.waiting_on = %(on)s AND s.status = 'ok' AND {scope} ORDER BY c.id""").format(
            scope=SCOPE
        ),
        _params(ctx, on=on),
    ).fetchall()
    for chapter in rows:
        try:
            with share_errors():
                ended = holds(ctx, chapter)
        except Unreachable:
            raise
        except Exception:
            log.exception("%s: checking the %s wait of %s failed", ctx.part, on, _name(chapter))
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


def charge_waits(ctx: Context, on: str, reason: str, waited_since: str, at: str) -> int:
    """Charge 1 attempt to each in-scope chapter of an ok series waiting on `on` whose wait began by waited_since.

    The wait was recorded at or before waited_since (a wait renewed at a cost, SPEC "Publishing"); the charge stamps
    updated_at with `at`, so a charged chapter's wait counts from then. A chapter reaching the attempt limit becomes
    needs_human. Runs inside the caller's transaction; returns how many were charged.
    """
    if not db.in_transaction(ctx.conn):
        msg = "charge_waits runs inside the caller's transaction"
        raise RuntimeError(msg)
    limit = ctx.cfg["workflow"]["attempt_limit"]
    rows = ctx.conn.execute(
        sql.SQL("""UPDATE chapter SET attempts = attempts + 1, last_error = %(reason)s, updated_at = %(at)s
        WHERE status = 'waiting' AND waiting_on = %(on)s AND updated_at <= %(since)s
            AND series_id IN (SELECT s.id FROM series s WHERE s.status = 'ok' AND {scope})
        RETURNING id, attempts""").format(scope=SCOPE),
        _params(ctx, reason=reason, on=on, since=waited_since, at=at),
    ).fetchall()
    for row in rows:
        if row["attempts"] >= limit:
            needs_human(ctx.conn, "chapter", row["id"], f"{reason} (after {limit} attempts)")
    return len(rows)


def release_own_claims(ctx: Context) -> None:
    """Free the claims a killed run of this owner left, in scope or not.

    They go back to ready at their stage, attempts unchanged (this run holds the owner's lock, so they are dead). Not
    when lock.enabled is false: another live run of the owner may hold them.
    """
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


def holds_claim(ctx: Context, chapter: db.Row) -> None:
    """Raise ClaimLost unless this run still holds the chapter's claim; inside the caller's transaction or alone.

    Called immediately before an act that cannot be undone outside the database (SPEC "State").
    """
    held = ctx.conn.execute(
        "SELECT 1 FROM chapter WHERE id = %s AND status = 'claimed' AND claimed_by = %s AND claim_count = %s",
        (chapter["id"], ctx.owner, chapter["claim_count"]),
    ).fetchone()
    if held is None:
        msg = f"the claim on chapter {chapter['id']} was freed or taken by another run"
        raise ClaimLost(msg)


def needs_human(conn: db.Connection, kind: Literal["chapter", "series"], item_id: int, reason: str) -> None:
    """Make a chapter or a series needs_human (kind "chapter" or "series"): the single place it happens.

    Runs inside the caller's transaction, and queues its Discord report there (SPEC "Needs human").
    """
    if not db.in_transaction(conn):
        msg = "needs_human runs inside the caller's transaction"
        raise RuntimeError(msg)
    row: db.Row
    if kind == "chapter":
        conn.execute(
            """UPDATE chapter SET status = 'needs_human', claimed_by = NULL, waiting_on = NULL, wait_data = NULL,
                last_error = %s, updated_at = %s
            WHERE id = %s""",
            (reason, now(conn), item_id),
        )
        row = db.one_row(
            conn,
            """SELECT c.id, c.identity, c.stage, s.name FROM chapter c JOIN series s ON s.id = c.series_id
            WHERE c.id = %s""",
            (item_id,),
        )
    else:
        conn.execute("UPDATE series SET status = 'needs_human', status_reason = %s WHERE id = %s", (reason, item_id))
        row = db.one_row(conn, "SELECT id, name, origin, admitted_at FROM series WHERE id = %s", (item_id,))
    notify.queue(conn, kind, item_id, *human_line(kind, row, reason))
    log.warning("needs human: %s %s: %s", kind, item_id, reason)


def human_line(kind: Literal["chapter", "series"], row: db.Row, reason: str) -> tuple[str, str]:
    """Return how a needs_human chapter or series is reported (SPEC "Needs human"): (what and why, the fix).

    The fix is the command that clears it, and the others that settle a chapter at its stage (SPEC "Bad downloads");
    empty for a new series whose reason already names it. row: a chapter's id, identity, stage and series name, or a
    series' id, name, origin and admitted_at.
    """
    if kind == "chapter":
        fix = f"retry {row['id']}"
        if row["stage"] == "validate":
            fix += f", chapter use {row['id']} SUWAYOMI_CHAPTER_ID"
        if row["stage"] in DROPPABLE:
            fix += f", chapter drop {row['id']}"
        return f"chapter {row['id']} ({row['name']} / {row['identity']}, at {row['stage']}): {reason}", fix
    fix = f"retry --series {row['id']}"
    if row["origin"] == "new" and row["admitted_at"] is None:
        fix = "" if "series new" in reason else f"series link or series new {row['id']}"
    return f"series {row['id']} ({row['name']}): {reason}", fix


class Refused(ValueError):  # noqa: N818  # a signal, not an error (PEP 8: the Error suffix is for errors)
    """A person's command that does not apply to the item it names; nothing was changed."""


def retry_chapter(conn: db.Connection, chapter_id: int) -> None:
    """Make a needs_human chapter ready again at the stage where it stopped, with its attempts at 0."""
    with db.transaction(conn):
        row = conn.execute("SELECT status FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
        if row is None:
            msg = f"no chapter {chapter_id}"
            raise Refused(msg)
        if row["status"] != "needs_human":
            msg = f"chapter {chapter_id} is {row['status']}, not needs_human"
            raise Refused(msg)
        _ready_again(conn, sql.SQL("id = %s"), chapter_id)


def retry_series(conn: db.Connection, series_id: int) -> None:
    """Make a series ok (reason cleared), and its needs_human chapters ready again at their stages, attempts 0.

    Refused when neither the series nor any of its chapters is needs_human, and for a new series not yet admitted
    (SPEC "Series identification": `series link` or `series new` settles it).
    """
    with db.transaction(conn):
        row = conn.execute("SELECT status, origin, admitted_at FROM series WHERE id = %s", (series_id,)).fetchone()
        if row is None:
            msg = f"no series {series_id}"
            raise Refused(msg)
        if row["origin"] == "new" and row["admitted_at"] is None:
            msg = (
                f"series {series_id} is a new series not yet admitted: series link {series_id}'s manga or folder to a "
                f"Kavita series, or series new {series_id} [--name NAME]"
            )
            raise Refused(msg)
        chapters = _ready_again(conn, sql.SQL("series_id = %s AND status = 'needs_human'"), series_id)
        if row["status"] != "needs_human" and not chapters:
            msg = f"series {series_id} is {row['status']} and has no needs_human chapter"
            raise Refused(msg)
        conn.execute("UPDATE series SET status = 'ok', status_reason = NULL WHERE id = %s", (series_id,))


def move_chapters(conn: db.Connection, from_series: int, to_series: int) -> None:
    """Move a series' chapters to another (series link merging a held row; inside the caller's transaction).

    An open chapter past junk goes back to junk, ready, attempts 0, so the new series' baseline and handled-digest
    check apply; a part holding its claim then drops its outcome.
    """
    junk = STAGES.index("junk")
    later = STAGES[junk + 1 :]
    _back_to_junk(
        conn,
        sql.SQL("series_id = %s AND status NOT IN ('done', 'needs_human') AND stage IN ({stages})").format(
            stages=sql.SQL(",").join([sql.Placeholder()] * len(later))
        ),
        (from_series, *later),
    )
    conn.execute("UPDATE chapter SET series_id = %s WHERE series_id = %s", (to_series, from_series))


def layout_changed(conn: db.Connection, series_id: int) -> None:
    """Rescan a series' open downloads under its changed layout (SPEC "State"; inside the caller's transaction).

    Those at convert go back to junk, ready, attempts 0; those waiting at junk become ready. Their scans are stale
    (SCANNED compares the layout) and are kept, so the candidates a needs_human junk chapter waits on stay open. A
    chapter past convert is published as converted.
    """
    _back_to_junk(
        conn, sql.SQL("series_id = %s AND kind = 'download' AND stage = 'convert' AND status <> 'done'"), (series_id,)
    )
    conn.execute(
        """UPDATE chapter SET status = 'ready', waiting_on = NULL, wait_data = NULL, updated_at = %s
        WHERE series_id = %s AND kind = 'download' AND stage = 'junk' AND status = 'waiting'""",
        (now(conn), series_id),
    )


def _back_to_junk(conn: db.Connection, where: sql.Composable, params: tuple[object, ...]) -> None:
    """Send the chapters matching `where` back to junk: ready, attempts 0, wait and claim cleared."""
    conn.execute(
        sql.SQL("""UPDATE chapter SET stage = 'junk', status = 'ready', waiting_on = NULL, wait_data = NULL,
            claimed_by = NULL, attempts = 0, retry_after = NULL, updated_at = %s
        WHERE {where}""").format(where=where),
        (now(conn), *params),
    )


def reset(conn: db.Connection) -> None:
    """Empty the workflow state (SPEC "State"): every WORKFLOW_TABLES table, and the series' statuses.

    Every series goes back to ok but one held for want of a Kavita series or a folder, which only `series link` (or
    `series new` for a new series not yet admitted) settles (identification never runs again).
    """
    with db.transaction(conn):
        for table in db.WORKFLOW_TABLES:
            conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        conn.execute(
            """UPDATE series SET status = 'ok', status_reason = NULL
            WHERE folder IS NOT NULL AND (kavita_series_id IS NOT NULL OR admitted_at IS NOT NULL)"""
        )


def _ready_again(conn: db.Connection, where: sql.SQL, value: int) -> int:
    """Ready again at its stage, attempts 0.

    A re-download's source search starts again from the top, and a person's choice (chapter use) is forgotten (SPEC
    "Bad downloads").
    """
    return conn.execute(
        sql.SQL("""UPDATE chapter SET status = 'ready', attempts = 0, retry_after = NULL, updated_at = %s,
            redownload = CASE WHEN redownload::jsonb ->> 'original' IS NULL THEN NULL
                ELSE jsonb_set(jsonb_set(jsonb_set(redownload::jsonb, '{{tried}}', '[]'), '{{same_source_done}}',
                    'false'), '{{chosen}}', 'null')::text END
        WHERE {where}""").format(where=where),
        (now(conn), value),
    ).rowcount


class Original(TypedDict):
    """A re-download's original copy: its Suwayomi source's display name, manga and chapter (None: hand-placed)."""

    source: str | None
    manga_id: int | None
    chapter_id: int | None


class Rejection(TypedDict):
    """A rejected copy: its source, Suwayomi chapter (None: hand-placed), content digest (None: unreadable), why."""

    source: str | None
    chapter_id: int | None
    digest: str | None
    why: str


class Removal(TypedDict):
    """A copy to remove: its stored path, Suwayomi chapter (None: hand-placed), whether it comes off the queue."""

    path: str
    chapter_id: int | None
    dequeue: bool


class Target(TypedDict):
    """A Suwayomi chapter to download (redownload.target): source display name, manga, chapter, stored path, page."""

    source: str
    manga_id: int
    chapter_id: int
    path: str
    url: str | None  # the chapter's page at its source


class SameSource(Target):
    """The original chapter, picked to download once more (redownload.pick); enqueue records it as done."""

    same_source: Literal[True]


class Chosen(Target):
    """The Suwayomi chapter a person chose with chapter use, picked to download; enqueue clears the choice."""

    chosen: Literal[True]


class Expected(TypedDict):
    """The download a re-download waits on: its target and when it was queued (url: none before schema step 0053)."""

    source: str
    manga_id: int
    chapter_id: int
    path: str
    url: NotRequired[str | None]
    enqueued_at: str


@final
class PersonChoice(TypedDict):
    """The re-download state of a chapter never rejected that a person chose a replacement for (use_chapter)."""

    chosen: int


@final
class Redownload(TypedDict):
    """A chapter's re-download state once it was rejected (SPEC "Bad downloads"; redownload.py's docstring)."""

    defect: str | None
    original: Original
    rejected: list[Rejection]
    tried: list[str]
    same_source_done: bool
    expected: Expected | None
    chosen: int | None
    remove: list[Removal]


type RedownloadState = PersonChoice | Redownload  # chapter.redownload, told apart by "original"


def read_redownload(text: str) -> RedownloadState:
    """Return a chapter's re-download state (chapter.redownload, JSON text). Unchecked."""
    return json.loads(text)


def read_notes(text: str) -> list[str]:
    """Return a chapter's notes (chapter.notes, JSON text: a list of notes). Unchecked."""
    return json.loads(text)


# The stages at which a person may drop a needs_human chapter: nothing of it is staged or published yet.
DROPPABLE = ("discover", "validate", "junk")


def use_chapter(conn: db.Connection, chapter_id: int, suwayomi_chapter_id: int) -> None:
    """Record a person's chosen replacement (chapter use).

    A needs_human chapter at validate becomes ready with the Suwayomi chapter to download as its replacement (SPEC "Bad
    downloads").
    """
    with db.transaction(conn):
        _refuse_unless_human(conn, chapter_id, ("validate",))
        conn.execute(
            """UPDATE chapter SET status = 'ready', attempts = 0, retry_after = NULL, updated_at = %s,
                redownload = jsonb_set(coalesce(redownload, '{}')::jsonb, '{chosen}', to_jsonb(%s::bigint))::text
            WHERE id = %s""",
            (now(conn), suwayomi_chapter_id, chapter_id),
        )


def droppable(conn: db.Connection, chapter_id: int) -> db.Row:
    """Return the row of a chapter a person may drop; Refused otherwise.

    It may be dropped when needs_human at a DROPPABLE stage, with no removal or download of its re-download
    outstanding.
    """
    with db.transaction(conn):
        row = _refuse_unless_human(conn, chapter_id, _droppable_stages(conn, chapter_id))
        state = None if row["redownload"] is None else read_redownload(row["redownload"])
        pending = (
            []
            if state is None or "original" not in state
            else [r["path"] for r in state["remove"]] + ([state["expected"]["path"]] if state["expected"] else [])
        )
        if pending:
            msg = (
                f"chapter {chapter_id} still has re-download work outstanding ({', '.join(pending)}): "
                f"retry {chapter_id} lets intake finish it"
            )
            raise Refused(msg)
        return row


def drop_chapter(conn: db.Connection, chapter_id: int, record: Callable[[db.Connection], object]) -> None:
    """Drop a needs_human chapter for a person (chapter drop): done where it stands.

    record(conn) (its handled row) runs in the same transaction. The caller removes its download first.
    """
    with db.transaction(conn):
        _refuse_unless_human(conn, chapter_id, _droppable_stages(conn, chapter_id))
        conn.execute(
            """UPDATE chapter SET status = 'done', last_error = 'dropped by a person', redownload = NULL,
                notes = NULL, updated_at = %s WHERE id = %s""",
            (now(conn), chapter_id),
        )
        record(conn)


def _droppable_stages(conn: db.Connection, chapter_id: int) -> tuple[str, ...]:
    """Return DROPPABLE, or every stage for a chapter naming its file.

    A chapter naming its file is a rewrite, or a better copy (SPEC "Retroactive updates"): it holds the file's 1
    open-chapter place.
    """
    row = conn.execute("SELECT published_file_id FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
    return STAGES if row is not None and row["published_file_id"] is not None else DROPPABLE


def _refuse_unless_human(conn: db.Connection, chapter_id: int, stages: Collection[str]) -> db.Row:
    row = conn.execute("SELECT * FROM chapter WHERE id = %s", (chapter_id,)).fetchone()
    if row is None:
        msg = f"no chapter {chapter_id}"
        raise Refused(msg)
    if row["status"] != "needs_human" or row["stage"] not in stages:
        msg = f"chapter {chapter_id} is {row['status']} at {row['stage']}, not needs_human at {' or '.join(stages)}"
        raise Refused(msg)
    return row


def run_part(ctx: Context, body: Callable[[Context], object]) -> None:
    """Run a part (SPEC "Parts"): record its start, release its own stale claims, run body(ctx), record its end.

    The end records its result ("ok", "unreachable: <service>, …" or "error"), and its last success when the result is
    ok. Unreachable raised outside a stage (discovery, a wait check) ends the run; either way the exit code stays 0.
    Any other exception is recorded as "error" and left to the caller, unlogged.
    """
    _set_meta(ctx, "last_start", now(ctx.conn))
    result = "error"
    services: set[str] = set()
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


def _finish(ctx: Context, chapter: db.Row, outcome: OUTCOMES | Unreachable) -> bool:
    """Record outcome for a chapter this run claimed (its owner and claim count).

    Not when a person changed the row meanwhile or the claim was freed. Returns whether it was recorded.
    """
    limit = ctx.cfg["workflow"]["attempt_limit"]
    with db.transaction(ctx.conn):
        stamp = now(ctx.conn)
        set_, values, limit_reason = _outcome_columns(chapter, outcome, limit, stamp)
        changed = ctx.conn.execute(
            sql.SQL(
                "UPDATE chapter SET {set_}, claimed_by = NULL, updated_at = %s "
                "WHERE id = %s AND claimed_by = %s AND claim_count = %s AND status = 'claimed'"
            ).format(set_=set_),
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
        elif limit_reason is not None:
            needs_human(ctx.conn, "chapter", chapter["id"], limit_reason)
        if isinstance(outcome, RECORDED) and outcome.record:
            outcome.record(ctx.conn)
    return True


def _outcome_columns(
    chapter: db.Row, outcome: OUTCOMES | Unreachable, limit: int, stamp: str
) -> tuple[sql.Composable, tuple[object, ...], str | None]:
    """Return (SET clause, its values, the needs_human reason once the attempt limit is reached) for an outcome.

    Recorded at stamp. Only a failure sets retry_after; every other outcome clears it.
    """
    if isinstance(outcome, Failed):
        attempts = chapter["attempts"] + 1
        return (
            sql.SQL("status = 'ready', attempts = %s, last_error = %s, retry_after = %s"),
            (attempts, outcome.error, after(stamp, RETRY_PAUSE)),
            f"{outcome.error} (after {limit} attempts)" if attempts >= limit else None,
        )
    set_: sql.SQL
    values: tuple[object, ...]
    if isinstance(outcome, Done):
        # a rewrite or a better copy ends at publish: its file's row carries the rest (SPEC "Retroactive updates")
        last = chapter["stage"] == STAGES[-1] or (
            chapter["published_file_id"] is not None and chapter["stage"] == "publish"
        )
        stage = chapter["stage"] if last else STAGES[STAGES.index(chapter["stage"]) + 1]
        set_, values = sql.SQL("stage = %s, status = %s, attempts = 0"), (stage, "done" if last else "ready")
    elif isinstance(outcome, Waiting):
        set_, values = (
            sql.SQL("status = 'waiting', waiting_on = %s, wait_data = %s"),
            (outcome.on, json.dumps(outcome.data)),
        )
    elif isinstance(outcome, Dropped):  # not listed
        set_, values = sql.SQL("status = 'done', last_error = %s, notes = NULL"), (f"dropped: {outcome.reason}",)
    else:  # Unreachable: no attempt; NeedsHuman: needs_human sets the status
        set_, values = sql.SQL("status = 'ready'"), ()
    return set_ + sql.SQL(", retry_after = NULL"), values, None


SHARE = "share"  # the service an Unreachable of the share names


@contextmanager
def share_errors() -> Generator[None]:
    """Turn a share error into Unreachable("share", the error's text); other errors pass unchanged.

    A share error is an OSError with a SHARE_ERRNOS errno, or on Windows a SHARE_WINERRORS winerror (a full disk is
    one).
    """
    try:
        yield
    except OSError as e:
        if e.errno not in SHARE_ERRNOS and getattr(e, "winerror", None) not in SHARE_WINERRORS:
            raise
        raise Unreachable(SHARE, str(e)) from e


def _params(ctx: Context, **extra: object) -> dict[str, object]:
    return {"part": ctx.part, "owner": ctx.owner, "now": now(ctx.conn), **scope_params(ctx.cfg), **extra}


def _set_meta(ctx: Context, name: str, value: str) -> None:
    with db.transaction(ctx.conn):
        set_meta(ctx.conn, f"{ctx.owner}.{name}", value)  # 1 set of run records per part and machine


def set_meta(conn: db.Connection, key: str, value: str) -> None:
    """Store a meta value; runs inside the caller's transaction."""
    if not db.in_transaction(conn):
        msg = "set_meta runs inside the caller's transaction"
        raise RuntimeError(msg)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: db.Connection, key: str) -> str | None:
    """Return a meta value, or None when it was never stored."""
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return None if row is None else row[0]
