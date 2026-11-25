"""Command-line entry point, `python -m manga_pipeline.cli` (SPEC "CLI", "Run lock", "Logging", "Preflight").

COMMANDS is the command table: each command's locks, log file, preflight checks and handler. Every command runs in
the same order: config, its connection and locks (a command with locks), logging, its checks, the schema steps (a
command with locks), its handler. A run the timers or tasks start (--scheduled, an automated command only) first
holds lock.AUTOMATION and reads the automation's pause: paused, it ends there (SPEC "Parts").
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path

from manga_pipeline import (
    archive,
    config,
    db,
    errors,
    export,
    hand_edits,
    handled,
    identify,
    incidents,
    intake,
    lock,
    logs,
    pause,
    preflight,
    process,
    retention,
    rewrite,
    smoke,
    source_review,
    source_sample,
    state,
    status,
    volumes,
    workflow,
)
from manga_pipeline.clients import kavita, suwayomi
from manga_pipeline.db import sql
from manga_pipeline.junk import backup, candidates, groups, redecide, review, review_run, store, where
from manga_pipeline.metadata import audit, human, importer, naming, sources

# Not __name__: under python -m it is "__main__", outside the package logger that logs.setup configures.
log = logging.getLogger("manga_pipeline.cli")
DESCRIPTION = "Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion"
# Every command that takes a lock but state version runs these.
LOCKED_CHECKS = (
    preflight.PYTHON_VERSION,
    preflight.FREE_SPACE,
    preflight.FOLDER_LAYOUT,
    preflight.SHARE_FOLDERS_PRESENT,
)
PART_CHECKS = (
    *LOCKED_CHECKS,
    preflight.SHARE_SPACE,
    preflight.WORKFLOW_SETTINGS,
    preflight.SHARE_OWNERSHIP,
    preflight.FLEET_VERSION,
)
# process runs OCR and the image tools and scorer; it reaches no service.
PROCESS_CHECKS = (
    *PART_CHECKS,
    preflight.OCR_DEVICE,
    preflight.JUNK_WORD_LISTS,
    preflight.IMAGE_TOOLS,
    preflight.SCORER,
    preflight.SCRATCH_SPACE,
    preflight.LONG_PATHS,
    preflight.NAMING_OVERRIDES,
)
# intake and export reach Suwayomi and Kavita; every part reads the naming overrides.
INTAKE_CHECKS = (*PART_CHECKS, preflight.SERVICE_SETTINGS, preflight.NAMING_OVERRIDES)
EXPORT_CHECKS = (*PART_CHECKS, preflight.SERVICE_SETTINGS, preflight.NAMING_OVERRIDES, preflight.DISCORD_SETTINGS)
RESET_CHECKS = (*LOCKED_CHECKS, preflight.SHARE_OWNERSHIP)
# state adopt writes the owner file, so it cannot require one; it needs the folder the file goes in.
ADOPT_CHECKS = (*LOCKED_CHECKS, preflight.STAGING_FOLDER)
# junk review reads the candidates on the share and starts Claude.
REVIEW_CHECKS = (
    *LOCKED_CHECKS,
    preflight.WORKFLOW_SETTINGS,
    preflight.SHARE_OWNERSHIP,
    preflight.FLEET_VERSION,
    preflight.CLAUDE_SETTINGS,
)
# state version digests the program, the shared settings, the packages and the tools of this machine.
VERSION_CHECKS = (preflight.PYTHON_VERSION, preflight.FREE_SPACE, preflight.IMAGE_TOOLS, preflight.SCORER)
# watch takes no lock and touches no folder: it needs only the database and a webhook, so it still runs when every
# part stops at its preflight.
WATCH_CHECKS = (preflight.PYTHON_VERSION, preflight.DISCORD_SETTINGS, preflight.DISCORD_WEBHOOK)
# smoke takes no lock and touches no folder: it needs only what the clients and scope read.
SMOKE_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SERVICE_SETTINGS)
# series link reads both services and writes 1 series' link: what the clients and scope read.
LINK_CHECKS = SMOKE_CHECKS
# metadata import writes series rows and ids under intake's lock, reading both services.
IMPORT_CHECKS = SMOKE_CHECKS
# series edit reads the series' open downloads on the share and its override file to name its chapters.
EDIT_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SHARE_FOLDERS_PRESENT, preflight.NAMING_OVERRIDES)
# source survey, measure and search read the clients and scope; survey and measure read library files.
SOURCE_READ_CHECKS = SMOKE_CHECKS
SOURCE_MEASURE_CHECKS = (*SMOKE_CHECKS, preflight.SHARE_FOLDERS_PRESENT)
# source sample reads library files and runs the junk stage's scan and OCR in the scratch folder.
SAMPLE_CHECKS = (
    *LOCKED_CHECKS,
    preflight.WORKFLOW_SETTINGS,
    preflight.FLEET_VERSION,
    preflight.SERVICE_SETTINGS,
    preflight.OCR_DEVICE,
    preflight.JUNK_WORD_LISTS,
    preflight.SCRATCH_SPACE,
)


@dataclass(frozen=True)
class Command:
    """1 row of the command table: a command's locks, idle mode, log file, preflight checks and handler."""

    locks: tuple[str, ...]  # run locks taken for the run (a part's name stands for workflow.owner of it); empty = none
    idle: str | None  # how the run holds lock.IDLE: lock.SHARED, lock.EXCLUSIVE or None
    log: str | None  # log file name (<log>.log); None = console only
    checks: tuple[preflight.Check, ...]
    # handler(cfg, args, conn): conn is the command's connection holding its locks, None for a command without any
    handler: Callable[[config.Config, argparse.Namespace, db.Connection | None], None]
    builds: bool = False  # creates the database when the server has none, and builds one holding no schema record
    automated: bool = False  # the timers and tasks start it (--scheduled), so the automation's pause stops it

    @property
    def locked(self) -> bool:
        """Return whether the command takes any run lock: its own locks or lock.IDLE."""
        return bool(self.locks or self.idle)


def retry(cfg: config.Config, args: argparse.Namespace) -> None:
    """Make a needs_human chapter, or a series and its needs_human chapters, ready again (`retry`)."""
    with _database(cfg, "retry") as conn:
        if args.series is None:
            workflow.retry_chapter(conn, args.chapter)
            log.info("chapter %s is ready again", args.chapter)
        else:
            workflow.retry_series(conn, args.series)
            log.info("series %s and its needs_human chapters are ready again", args.series)


def _database(cfg: config.Config, what: str) -> closing[db.Connection]:
    """Return a connection to the built database, for a command that never builds one (SPEC "State")."""
    if not db.built(cfg):
        msg = f"no database {cfg['database']['name']} built yet: nothing to {what}"
        raise workflow.Refused(msg)
    return closing(db.connect(cfg))


PAUSE_POLL = 10  # seconds between state pause's looks at the scheduled runs still running
PAUSE_REPORT = 6  # looks between 2 of its progress lines (1 a minute)


def _unbuilt_refused(cfg: config.Config, what: str) -> db.Connection:
    """Open the built database and apply no schema step (any program version may); Refused when none is built."""
    try:
        return db.open_database(cfg, builds=False)
    except db.NoDatabaseError:
        msg = f"no database {cfg['database']['name']} built yet: nothing to {what}"
        raise workflow.Refused(msg) from None


def state_pause(cfg: config.Config, _args: argparse.Namespace) -> None:
    """Pause the automation, then return once no scheduled run is left (`state pause`, SPEC "Run lock")."""
    if not cfg["lock"]["enabled"]:
        msg = "lock.enabled is false: with no locks, state pause cannot tell when the scheduled runs have ended"
        raise workflow.Refused(msg)
    with closing(_unbuilt_refused(cfg, "pause")) as conn:
        print(f"automation paused since {pause.set_pause(conn)}")
        looks = 0
        while running := pause.waiting(conn):
            if looks % PAUSE_REPORT == 0:
                print(f"waiting for {running} scheduled run(s) to end")
            looks += 1
            time.sleep(PAUSE_POLL)
    print("no scheduled run is running")


def state_resume(cfg: config.Config, _args: argparse.Namespace) -> None:
    """End the automation's pause (`state resume`); Refused when it is not paused."""
    with closing(_unbuilt_refused(cfg, "resume")) as conn:
        if not pause.clear(conn):
            msg = "the automation is not paused"
            raise workflow.Refused(msg)
    print("automation resumed")


def watch(cfg: config.Config, _args: argparse.Namespace) -> None:
    """Queue the incidents found and send what is queued (`watch`, SPEC "Needs human")."""
    with _database(cfg, "watch") as conn:
        incidents.watch(cfg, conn)


def chapter_use(cfg: config.Config, args: argparse.Namespace) -> None:
    """Have a needs_human chapter re-downloaded at validate from the given Suwayomi chapter (`chapter use`)."""
    with _database(cfg, "use") as conn:
        workflow.use_chapter(conn, args.chapter, args.suwayomi_chapter)
    log.info("chapter %s will be re-downloaded from Suwayomi chapter %s", args.chapter, args.suwayomi_chapter)


def chapter_drop(cfg: config.Config, args: argparse.Namespace) -> None:
    """Give a needs_human chapter up: its download goes the retention way first, then the chapter is done.

    A crash between leaves it needs_human, and the next drop finds its download already gone.
    """
    with _database(cfg, "drop") as conn:
        row = workflow.droppable(conn, args.chapter)
        renamed = handled.renamed(cfg, conn, row)
        if renamed:  # a drop records its digest, and junk would then put the renamed copy aside
            msg = f"chapter {args.chapter}: {renamed}: retry {args.chapter} closes it and keeps that copy"
            raise workflow.Refused(msg)
        if row["kind"] == "rewrite":  # no download, nothing handled: its staged file goes; the file stays as it is
            if row["staged_path"]:
                config.to_local(cfg, row["staged_path"]).unlink(missing_ok=True)
            workflow.drop_chapter(conn, args.chapter, lambda _c: None)
            log.info("chapter %s dropped", args.chapter)
            return
        digest = row["content_digest"]
        if row["staged_path"]:  # a better copy held past convert: its staged file goes too
            config.to_local(cfg, row["staged_path"]).unlink(missing_ok=True)
        if row["download_path"]:
            download = config.to_local(cfg, row["download_path"])
            if not download.is_dir():  # a directory: a conversion's link error, which intake removes once done
                if digest is None and errors.exists(download):  # held at discover, before its digest was settled
                    digest = archive.digest_or_none(download)
                retention.put(cfg, download)
        workflow.drop_chapter(
            conn, args.chapter, lambda c: handled.record(c, row["series_id"], row["identity"], "dropped", digest)
        )
    log.info("chapter %s dropped", args.chapter)


def series_link(cfg: config.Config, args: argparse.Namespace) -> None:
    """Link a Suwayomi manga or a hand-placed download folder to a Kavita series (`series link`)."""
    with _database(cfg, "link") as conn:
        kavita_client = kavita.from_config(cfg)
        suwayomi_client = suwayomi.from_config(cfg)
        if args.folder is not None:
            subject = identify.folder_link_subject(cfg, suwayomi_client, args.folder)
        else:
            subject = identify.manga_link_subject(suwayomi_client, args.manga_id)
        identify.link(cfg, conn, kavita_client, args.kavita_series_id, subject)


def series_set_id(cfg: config.Config, args: argparse.Namespace) -> None:
    """Set a series' id for a metadata source by hand (`series set-id`); it wins over the rest."""
    with _database(cfg, "set an id in") as conn:
        found = human.set_id(conn, args.series_id, args.source, args.id)
    log.info("series %s: %s id %s set by hand", args.series_id, args.source, found)


def series_refetch_covers(cfg: config.Config, args: argparse.Namespace) -> None:
    """Make a series' check due now, so the next intake refreshes its volume covers (`series refetch-covers`)."""
    with _database(cfg, "refetch covers in") as conn:
        human.refetch_covers(conn, args.series_id)
    log.info("series %s: its check is due now: the next intake refreshes its volume covers", args.series_id)


def series_new(cfg: config.Config, args: argparse.Namespace) -> None:
    """Have a held series admitted as a new series, under --name when given (`series new`)."""
    with _database(cfg, "admit a series in") as conn:
        human.new_series(conn, args.series_id, args.name)
    log.info("series %s: to be admitted as a new series%s", args.series_id, f" as {args.name!r}" if args.name else "")


def series_edit(cfg: config.Config, args: argparse.Namespace) -> None:
    """Apply a hand-edit file of chapter titles and volumes and print the lines it reports (`series edit`)."""
    data = hand_edits.load(args.file)
    with _database(cfg, "edit") as conn:
        lines = hand_edits.apply(workflow.context(cfg, conn, "export"), data, args.series_id)
    for line in lines:
        print(line)


def naming_diff(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print what the comparison would rewrite, and the differences 7e never applies (SPEC "Retroactive updates").

    Read-only.
    """
    with db.read_only(cfg) as conn:
        ctx = workflow.context(cfg, conn, "export")
        where = sql.SQL("s.id = %(series)s") if args.series_id else sql.SQL("s.kavita_series_id IS NOT NULL")
        series_rows = conn.execute(
            sql.SQL("SELECT s.* FROM series s WHERE {where} AND {scope} ORDER BY s.id").format(
                where=where, scope=workflow.SCOPE
            ),
            {**workflow.scope_params(cfg), "series": args.series_id},
        ).fetchall()
        for series in series_rows:
            try:
                queued, wants, unknown, apart, edits = rewrite.diff(ctx, series)
            except volumes.VolumeError as e:
                print(f"series {series['id']} ({series['name']}): {e}")
                continue
            if unknown:
                print(f"series {series['id']}: covers unknown (some files' covers not read yet: export reads them)")
            for path, old, new, target, cover, title in queued:
                renamed = f" -> {target}" if target != path else ""
                change = (f"volume {old or '-'} -> {new or '-'}" if old != new else f"volume {old or '-'}") + renamed
                titled = f", title {title!r}" if title is not None else ""
                print(f"series {series['id']}: {path}: {change}{titled}{f', {cover}' if cover else ''}")
            for volume in wants:
                print(f"series {series['id']}: volume {volume} cover wanted (intake fetches it)")
            for path, stored, now in apart:
                print(f"series {series['id']}: {path}: volume {stored}, the sources say {now or 'none'} (not applied)")
            for token, title, volume in edits:
                print(f"series {series['id']}: edit {token}: {hand_edits.describe(naming.Edit(title, volume))}")


def rewrite_drop(cfg: config.Config, args: argparse.Namespace) -> None:
    """Give up a stuck rewrite before its rename and delete its staged file (`rewrite drop`)."""
    with _database(cfg, "drop a rewrite in") as conn:
        row = rewrite.drop(conn, args.file_id, cfg["workflow"]["attempt_limit"])
    if row["pending_staged"]:
        config.to_local(cfg, row["pending_staged"]).unlink(missing_ok=True)
    log.info("published file %s: rewrite dropped", args.file_id)


def rewrite_retry(cfg: config.Config, args: argparse.Namespace) -> None:
    """Have the next export try a failed rewrite again from its next step (`rewrite retry`)."""
    with _database(cfg, "retry a rewrite in") as conn:
        rewrite.retry(conn, args.file_id)
    log.info("published file %s: rewrite tried again by the next export", args.file_id)


def metadata_import(cfg: config.Config, args: argparse.Namespace, conn: db.Connection | None) -> None:
    """Import the old metadata tool's ids, covers and overrides from its state and config files (`metadata import`)."""
    state, overrides = importer.read_state(args.state_file), importer.read_overrides(args.config_file)
    importer.run(workflow.context(cfg, locked_connection(conn), "intake"), state, overrides)


def metadata_audit(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print the library's source-id audit (`metadata audit`).

    It lists the series whose source ids disagree, the sources no origin gives an id for and the Suwayomi manga no
    series matches. Opens no database (SPEC "Metadata checks", Audit): it runs before cutover, with or without one.
    """
    state = importer.read_state(args.state) if args.state else None
    _lines(audit.run(cfg, kavita.from_config(cfg), suwayomi.from_config(cfg), sources.new_sources(), state))


def _lines(lines: Iterable[str]) -> None:
    for line in lines:
        print(line)


def source_survey(cfg: config.Config, _args: argparse.Namespace) -> None:
    """Print every series in scope with its combined measures and the triggers it crosses (`source survey`)."""
    with db.read_only(cfg) as conn:
        _lines(source_review.survey(workflow.context(cfg, conn, "source")))


def source_measure(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print a series' published files' measures and its linked manga (`source measure`)."""
    with db.read_only(cfg) as conn:
        _lines(source_review.measure(workflow.context(cfg, conn, "source"), args.series_id))


def source_search(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print what each source's search finds for a series (`source search`)."""
    with db.read_only(cfg) as conn:
        _lines(source_review.search(workflow.context(cfg, conn, "source"), args.series_id, args.source))


def source_sample_command(cfg: config.Config, args: argparse.Namespace, conn: db.Connection | None) -> None:
    """Print the better-copy rule's outcome on a manga's copies of a series' files (`source sample`)."""
    ctx = workflow.context(cfg, locked_connection(conn), "source")
    _lines(source_sample.sample(ctx, args.series_id, args.manga_id, args.chapters))


def source_switch(cfg: config.Config, args: argparse.Namespace, conn: db.Connection | None) -> None:
    """Link a manga to a series and add it to Suwayomi's library (`source switch`)."""
    ctx = workflow.context(cfg, locked_connection(conn), "source")
    _lines(source_review.switch(ctx, args.series_id, args.manga_id))


def source_replace(cfg: config.Config, args: argparse.Namespace, conn: db.Connection | None) -> None:
    """Queue a linked manga's copies of a series' published files (`source replace`)."""
    ctx = workflow.context(cfg, locked_connection(conn), "source")
    _lines(source_review.replace(ctx, args.series_id, args.manga_id, None if args.all else args.file))


def source_retire(cfg: config.Config, args: argparse.Namespace, conn: db.Connection | None) -> None:
    """Take a manga out of Suwayomi's library and unlink it from a series (`source retire`)."""
    ctx = workflow.context(cfg, locked_connection(conn), "source")
    _lines(source_review.retire(ctx, args.series_id, args.manga_id))


def _junk_db(cfg: config.Config) -> closing[db.Connection]:
    return _database(cfg, "list, show, decide or re-decide")


def _folder(cfg: config.Config, row: db.Row) -> Path:
    return candidates.folder(cfg, row["folder"])


def junk_list(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print the open junk candidates to decide, 1 line each with its image paths (`junk list`; --all: every one).

    Inside a review, then 1 line per group of near candidates: its ids and its sheets.
    """
    review_id = os.environ.get(review.REVIEW_ID_VARIABLE)
    with _junk_db(cfg) as conn:
        try:
            rows = review.listing(conn, cfg, review_id, everything=args.all)
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
        near = groups.near_groups(rows) if review_id else []
        group_of = {r["id"]: n for n, members in enumerate(near, 1) for r in members}
        for r in rows:
            folder = _folder(cfg, r)
            fields: list[str] = [str(r["id"]), r["series_key"], r["kind"], f"chapters {review.chapters(conn, r['id'])}"]
            if args.all:
                fields += [f"reviews {r['reviews']}", f"verdict {r['verdict'] or '-'}"]
            if r["id"] in group_of:
                fields.append(f"group {group_of[r['id']]}")
            print("	".join([*fields, str(folder / "unit.png"), str(folder / "context.png")]))
    if review_id:
        for n, (members, sheets) in enumerate(groups.write_sheets(cfg, review_id, near), 1):
            ids = ",".join(str(r["id"]) for r in members)
            print("	".join([f"group {n}", f"ids {ids}", *(str(s) for s in sheets)]))


def junk_show(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print open junk candidates' reason, chapters, OCR text and image paths (`junk show`), all or none."""
    review_id = os.environ.get(review.REVIEW_ID_VARIABLE)
    with _junk_db(cfg) as conn:
        try:
            rows = [review.get_open(conn, i, review_id) for i in args.ids]
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
        for row in rows:
            folder = _folder(cfg, row)
            info = candidates.read_info(folder)
            print(f"candidate {row['id']}: {row['kind']} of series {row['series_key']}")
            print(f"why: {row['reason']}")
            print(f"chapters: {review.chapters(conn, row['id'])}")
            print(f"text: {info['text'] or '-'}")
            print(f"unit: {folder / 'unit.png'}")
            print(f"context: {folder / 'context.png'}")


def junk_decide(cfg: config.Config, args: argparse.Namespace) -> None:
    """Record a verdict on junk candidates, by claude under a review run, else by a human (`junk decide`), all or none.

    --keep-rest (junk, inside a review): the rest of that series the review listed is kept.
    """
    review_id = os.environ.get(review.REVIEW_ID_VARIABLE)
    decider = "claude" if review_id else "human"
    if args.keep_rest and args.verdict != "junk":
        msg = f"--keep-rest goes with junk, not {args.verdict}"
        raise workflow.Refused(msg)
    with _junk_db(cfg) as conn:
        try:
            if args.keep_rest:
                kept = review.decide_junk_keeping_rest(conn, args.ids, review_id, workflow.now(conn))
            else:
                review.decide(conn, args.ids, args.verdict, review_id, workflow.now(conn))
                kept = []
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
    log.info("junk candidate(s) %s decided %s by %s", args.ids, args.verdict, decider)
    if kept:
        log.info("junk candidate(s) %s kept as the rest by %s", [r["id"] for r in kept], decider)


def _decision_line(row: db.Row, acts: dict[int, list[str]]) -> str:
    """Return a decision as `junk rows` and `junk row` print it, its state and the acts that changed it."""
    if row["superseded_by"] is not None:
        state = f"superseded by {row['superseded_by']}"
    elif row["retired_by"] is not None:
        state = f"retired by act {row['retired_by']}"
    else:
        state = "active"
    fields = [
        str(row["id"]),
        row["series_key"] or "global",
        row["kind"],
        row["verdict"],
        row["decider"],
        row["decided_at"],
        row["source"] or "-",
        state,
        "acts: " + (", ".join(acts.get(row["id"], [])) or "-"),
        "images" if row["images"] else "no images",
    ]
    return "\t".join(fields)


def junk_rows(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print the decisions of a series and the global ones, or every decision, active and not (`junk rows`)."""
    with _junk_db(cfg) as conn:
        if args.series is not None and store.unknown_series(conn, [args.series]):
            msg = f"no series has the key {args.series!r}"
            raise workflow.Refused(msg)
        rows, acts = store.history(conn, args.series)
        for row in rows:
            print(_decision_line(row, acts))


def junk_row(cfg: config.Config, args: argparse.Namespace) -> None:
    """Print decisions with their kept images, or (--where) the sighted units each decides now (`junk row`)."""
    if args.series and not args.where:
        msg = "--series goes with --where"
        raise workflow.Refused(msg)
    with _junk_db(cfg) as conn:
        rows: list[db.Row] = []
        for i in args.ids:
            row = store.get(conn, i)
            if row is None:
                msg = f"no junk decision {i}"
                raise workflow.Refused(msg)
            if args.series and row["series_key"] != store.GLOBAL:
                msg = f"junk decision {i} is series {row['series_key']!r}'s: --series is for a global one"
                raise workflow.Refused(msg)
            rows.append(row)
        missing = store.unknown_series(conn, args.series)
        if missing:
            msg = f"no series has the key(s) {missing}"
            raise workflow.Refused(msg)
        acts = store.acts_of(conn, args.ids)
        for row in rows:
            print(_decision_line(row, acts))
            if args.where:
                units = where.decided_by(conn, row, args.series)
                print("\n".join(f"  {u.series_key}\t{u.identity}\t{u.kind}" for u in units) or "  no sighted unit")
            else:
                print(f"  {candidates.kept_images(cfg, row['images'])}")


def junk_redecide(cfg: config.Config, args: argparse.Namespace) -> None:
    """Re-decide decisions in 1 act (`junk redecide`): default, --series S… or --retire."""
    if args.series and args.retire:
        msg = "--series and --retire go apart: name series to opt out, or retire the global decision"
        raise workflow.Refused(msg)
    with _junk_db(cfg) as conn:
        at = workflow.now(conn)
        try:
            if args.series:
                act = redecide.redecide_in(conn, args.ids, args.verdict, args.series, at)
            elif args.retire:
                act = redecide.retire_global(conn, args.ids, args.verdict, at)
            else:
                act = redecide.redecide(conn, args.ids, args.verdict, at)
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
    print(f"junk act {act}: undo it with junk undo {act}")
    log.info("junk act %s: decision(s) %s re-decided %s", act, args.ids, args.verdict)


def junk_undo(cfg: config.Config, args: argparse.Namespace) -> None:
    """Reverse a junk act exactly, in a new act (`junk undo`)."""
    with _junk_db(cfg) as conn:
        try:
            act = redecide.undo(conn, args.act, workflow.now(conn))
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
    print(f"junk act {act} undid act {args.act}")
    log.info("junk act %s undid act %s", act, args.act)


def junk_backup(cfg: config.Config, args: argparse.Namespace) -> None:
    """Write the junk knowledge into a new folder in DIR (`junk backup`)."""
    try:
        folder = backup.backup(cfg, Path(args.dir))
    except review.Refused as e:
        raise workflow.Refused(str(e)) from None
    print(f"junk knowledge backed up to {folder}")
    log.info("junk knowledge backed up to %s", folder)


def junk_restore(args: argparse.Namespace, conn: db.Connection) -> None:
    """Write a junk backup back into an empty junk store, ids kept (`junk restore`)."""
    try:
        counts = backup.restore(conn, Path(args.dir))
    except review.Refused as e:
        raise workflow.Refused(str(e)) from None
    print(", ".join(f"{table} {n}" for table, n in counts.items()))
    log.info("junk knowledge restored from %s: %s", args.dir, counts)


def locked_connection(conn: db.Connection | None) -> db.Connection:
    """Return a locked command's connection; RuntimeError when it has none (its row in COMMANDS takes no lock)."""
    if conn is None:
        msg = "a command that takes locks runs without its connection"
        raise RuntimeError(msg)
    return conn


def _unlocked(
    handler: Callable[[config.Config, argparse.Namespace], None],
) -> Callable[[config.Config, argparse.Namespace, db.Connection | None], None]:
    """Return the command-table handler of a command without locks: its handler opens what it needs itself."""
    return lambda cfg, args, _conn: handler(cfg, args)


S, X = lock.SHARED, lock.EXCLUSIVE
COMMANDS = {
    "intake": Command(
        ("intake",),
        S,
        "intake",
        INTAKE_CHECKS,
        lambda cfg, _args, conn: intake.run(cfg, locked_connection(conn)),
        automated=True,
    ),
    "process": Command(
        ("process",),
        S,
        "process",
        PROCESS_CHECKS,
        lambda cfg, _args, conn: process.run(cfg, locked_connection(conn)),
        automated=True,
    ),
    "export": Command(
        ("export",),
        S,
        "export",
        EXPORT_CHECKS,
        lambda cfg, _args, conn: export.run(cfg, locked_connection(conn)),
        automated=True,
    ),
    "status": Command((), None, None, (), _unlocked(lambda cfg, _args: _lines(status.run(cfg)))),
    "retry": Command((), None, None, (), _unlocked(retry)),
    "chapter use": Command((), None, None, (), _unlocked(chapter_use)),
    "chapter drop": Command((), None, None, (), _unlocked(chapter_drop)),
    "smoke": Command((), None, None, SMOKE_CHECKS, _unlocked(lambda cfg, _args: _lines(smoke.run(cfg)))),
    "watch": Command((), None, None, WATCH_CHECKS, _unlocked(watch), automated=True),
    "series link": Command((), None, None, LINK_CHECKS, _unlocked(series_link)),
    "series set-id": Command((), None, None, (), _unlocked(series_set_id)),
    "series new": Command((), None, None, (), _unlocked(series_new)),
    "series refetch-covers": Command((), None, None, (), _unlocked(series_refetch_covers)),
    "series edit": Command((), None, None, EDIT_CHECKS, _unlocked(series_edit)),
    "naming diff": Command((), None, None, (), _unlocked(naming_diff)),
    "rewrite drop": Command((), None, None, (), _unlocked(rewrite_drop)),
    "rewrite retry": Command((), None, None, (), _unlocked(rewrite_retry)),
    "metadata import": Command(("intake",), S, None, IMPORT_CHECKS, metadata_import),
    "metadata audit": Command((), None, None, LINK_CHECKS, _unlocked(metadata_audit)),
    "junk list": Command((), None, None, (), _unlocked(junk_list)),
    "junk show": Command((), None, None, (), _unlocked(junk_show)),
    "junk decide": Command((), None, None, (), _unlocked(junk_decide)),
    "junk rows": Command((), None, None, (), _unlocked(junk_rows)),
    "junk row": Command((), None, None, (), _unlocked(junk_row)),
    "junk redecide": Command((), None, None, (), _unlocked(junk_redecide)),
    "junk undo": Command((), None, None, (), _unlocked(junk_undo)),
    "junk backup": Command((), None, None, (), _unlocked(junk_backup)),
    "junk restore": Command(
        (), X, None, (), lambda _cfg, args, conn: junk_restore(args, locked_connection(conn)), builds=True
    ),
    "junk review": Command(
        ("junk-review",),
        S,
        "junk-review",
        REVIEW_CHECKS,
        lambda cfg, _args, conn: review_run.run(cfg, locked_connection(conn)),
        automated=True,
    ),
    "source survey": Command((), None, None, SOURCE_MEASURE_CHECKS, _unlocked(source_survey)),
    "source measure": Command((), None, None, SOURCE_MEASURE_CHECKS, _unlocked(source_measure)),
    "source search": Command((), None, None, SOURCE_READ_CHECKS, _unlocked(source_search)),
    "source sample": Command(("source",), S, "source", SAMPLE_CHECKS, source_sample_command),
    "source switch": Command(("intake", "source"), S, "source", INTAKE_CHECKS, source_switch),
    "source replace": Command(("intake", "source"), S, "source", INTAKE_CHECKS, source_replace),
    "source retire": Command(("intake", "source"), S, "source", INTAKE_CHECKS, source_retire),
    "state reset": Command(
        (), X, None, RESET_CHECKS, lambda cfg, _args, conn: state.reset(cfg, locked_connection(conn))
    ),
    "state adopt": Command(
        (), X, None, ADOPT_CHECKS, lambda cfg, _args, conn: state.adopt(cfg, locked_connection(conn)), builds=True
    ),
    "state version": Command(
        (), X, None, VERSION_CHECKS, lambda cfg, _args, conn: state.version(cfg, locked_connection(conn))
    ),
    "state pause": Command((), None, None, (), _unlocked(state_pause)),
    "state resume": Command((), None, None, (), _unlocked(state_resume)),
}


def parser() -> argparse.ArgumentParser:
    """Build the argument parser: the global --config and --set, and every command."""
    p = argparse.ArgumentParser(prog="manga_pipeline", description=DESCRIPTION)
    p.add_argument("--config", help="config file (default: config.yaml in the project root)")
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set a config key for this run only, e.g. logging.level=DEBUG; beats the environment variable"
        f" {config.ENV_PREFIX}_LOGGING_LEVEL, which beats the config file",
    )
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="a run the timers or scheduled tasks start: does nothing while the automation is paused (state pause)",
    )
    commands = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("intake", help="discover, validate and identify new downloads")
    commands.add_parser("process", help="junk removal and conversion")
    commands.add_parser("export", help="ComicInfo.xml, publish into the library, confirm")
    commands.add_parser("status", help="what is waiting, on what, and what needs a human")
    commands.add_parser("smoke", help="check Suwayomi and Kavita with reads only")
    commands.add_parser("watch", help="report stalled parts and downloads not taken, and send the queued reports")
    _retry_command(commands.add_parser("retry", help="make a needs_human chapter or series ready again"))
    _series_commands(commands.add_parser("series", help="series identification"))
    _naming_commands(commands.add_parser("naming", help="chapter naming"))
    _rewrite_commands(commands.add_parser("rewrite", help="retroactive rewrites of published files"))
    _metadata_commands(commands.add_parser("metadata", help="series metadata"))
    _junk_commands(commands.add_parser("junk", help="junk decisions"))
    _chapter_commands(commands.add_parser("chapter", help="settle a needs_human chapter"))
    _source_commands(commands.add_parser("source", help="review a series' source, step by step"))
    _state_commands(commands.add_parser("state", help="workflow state and share ownership"))
    return p


def _retry_command(retry: argparse.ArgumentParser) -> None:
    """Fill the retry command's parser."""
    target = retry.add_mutually_exclusive_group(required=True)
    target.add_argument("chapter", nargs="?", type=int, help="chapter id")
    target.add_argument("--series", type=int, metavar="ID", help="a series and its chapters")


def _series_commands(command: argparse.ArgumentParser) -> None:
    """Add the series command's actions to its parser."""
    series = command.add_subparsers(dest="series_command", required=True, metavar="ACTION")
    set_id = series.add_parser("set-id", help="set a series' id for a metadata source by hand (wins over the rest)")
    set_id.add_argument("series_id", type=int, help="series id (as status shows it)")
    set_id.add_argument("source", help="anilist, mangaupdates, mangadex, mangabaka or myanimelist")
    set_id.add_argument("id", help="the source's id (a MangaUpdates id may be its base-36 text)")
    set_id.set_defaults(command="series set-id")
    edit = series.add_parser("edit", help="correct chapter titles and volumes by hand (a JSON edit file)")
    edit.add_argument("file", help='JSON: {"series": NAME, "chapters": {"10": {"title": ..., "volume": ...}}}')
    edit.add_argument("--series-id", type=int, help="the series id (as status shows it) when 2 series share NAME")
    edit.set_defaults(command="series edit")
    refetch = series.add_parser("refetch-covers", help="refresh a series' MangaDex volume covers at its next check")
    refetch.add_argument("series_id", type=int, help="series id (as status shows it)")
    refetch.set_defaults(command="series refetch-covers")
    new = series.add_parser("new", help="admit a held series as a new series (a near hit or a name clash)")
    new.add_argument("series_id", type=int, help="series id (as status shows it)")
    new.add_argument("--name", help="the name to admit it under (default: its own, SPEC 'Series identification')")
    new.set_defaults(command="series new")
    link = series.add_parser(
        "link",
        help="link a Suwayomi manga or a hand-placed download folder to a Kavita series (or refresh that link's "
        "names and folder)",
    )
    linked = link.add_mutually_exclusive_group(required=True)
    linked.add_argument("manga_id", nargs="?", type=int, help="Suwayomi manga id")
    linked.add_argument(
        "--folder", metavar="SOURCE_FOLDER/TITLE_FOLDER", help="a hand-placed folder under the downloads' mangas folder"
    )
    link.add_argument("kavita_series_id", type=int, help="Kavita series id")
    link.set_defaults(command="series link")


def _naming_commands(command: argparse.ArgumentParser) -> None:
    """Add the naming command's actions to its parser."""
    naming = command.add_subparsers(dest="naming_command", required=True, metavar="ACTION")
    diff = naming.add_parser("diff", help="list what retroactive updates would rewrite, and what they never apply")
    diff.add_argument("series_id", nargs="?", type=int, help="series id (default: every series in scope)")
    diff.set_defaults(command="naming diff")


def _rewrite_commands(command: argparse.ArgumentParser) -> None:
    """Add the rewrite command's actions to its parser."""
    rewrites = command.add_subparsers(dest="rewrite_command", required=True, metavar="ACTION")
    drop_rewrite = rewrites.add_parser("drop", help="give up a stuck rewrite before its rename (as status names it)")
    drop_rewrite.add_argument("file_id", type=int, help="published file id")
    drop_rewrite.set_defaults(command="rewrite drop")
    retry_rewrite = rewrites.add_parser("retry", help="try a failed rewrite again from its next step")
    retry_rewrite.add_argument("file_id", type=int, help="published file id")
    retry_rewrite.set_defaults(command="rewrite retry")


def _metadata_commands(command: argparse.ArgumentParser) -> None:
    """Add the metadata command's actions to its parser."""
    metadata = command.add_subparsers(dest="metadata_command", required=True, metavar="ACTION")
    import_metadata = metadata.add_parser("import", help="import the old metadata tool's ids, covers and overrides")
    import_metadata.add_argument("state_file", help="its .sync_state.json (only read)")
    import_metadata.add_argument("config_file", help="its config.yaml (only read)")
    import_metadata.set_defaults(command="metadata import")
    audit_ = metadata.add_parser("audit", help="list every library series whose source ids disagree (read-only)")
    audit_.add_argument("--state", metavar="PATH", help="the old metadata tool's .sync_state.json (only read)")
    audit_.set_defaults(command="metadata audit")


def _junk_commands(command: argparse.ArgumentParser) -> None:
    """Add the junk command's actions to its parser."""
    junk = command.add_subparsers(dest="junk_command", required=True, metavar="ACTION")
    list_ = junk.add_parser("list", help="the open junk candidates to decide")
    list_.add_argument("--all", action="store_true", help="every open candidate, stuck and unsure ones included")
    list_.set_defaults(command="junk list")
    show = junk.add_parser("show", help="candidates' images, OCR text and reason")
    show.add_argument("ids", type=int, nargs="+", metavar="ID", help="candidate ids")
    show.set_defaults(command="junk show")
    decide = junk.add_parser("decide", help="record a verdict on candidates")
    decide.add_argument("ids", type=int, nargs="+", metavar="ID", help="candidate ids, decided together or not at all")
    decide.add_argument("verdict", choices=review.VERDICTS, help="junk, keep or unsure")
    decide.add_argument(
        "--keep-rest", action="store_true", help="with junk, inside a review: keep the rest of the series it listed"
    )
    decide.set_defaults(command="junk decide")
    rows = junk.add_parser(
        "rows", help="the decisions of a series and the global ones, or every decision, with history"
    )
    rows.add_argument("series", nargs="?", help="a series key (its rules key); none: every decision")
    rows.set_defaults(command="junk rows")
    row = junk.add_parser("row", help="decisions with their kept images, or (--where) what each decides now")
    row.add_argument("ids", type=int, nargs="+", metavar="ROW", help="decision ids")
    row.add_argument("--where", action="store_true", help="the sighted chapter units each decides now")
    row.add_argument("--series", nargs="+", default=[], help="with --where, for a global decision: only these series")
    row.set_defaults(command="junk row")
    redecide_ = junk.add_parser("redecide", help="re-decide decisions in 1 act, which junk undo reverses")
    redecide_.add_argument("ids", type=int, nargs="+", metavar="ROW", help="decision ids, re-decided together or not")
    redecide_.add_argument("verdict", choices=redecide.VERDICTS, help="the new verdict")
    redecide_.add_argument(
        "--series", nargs="+", default=[], help="a global decision: decide it so in these series only"
    )
    redecide_.add_argument("--retire", action="store_true", help="a global decision: retire it, write nothing")
    redecide_.set_defaults(command="junk redecide")
    undo = junk.add_parser("undo", help="reverse a junk act exactly")
    undo.add_argument("act", type=int, help="the act's id, as junk redecide printed it")
    undo.set_defaults(command="junk undo")
    backup_ = junk.add_parser("backup", help="write the junk knowledge (decisions, acts, sightings, watermarks) to DIR")
    backup_.add_argument("dir", help="the folder to write a new junk-<time> folder in")
    backup_.set_defaults(command="junk backup")
    restore = junk.add_parser("restore", help="restore a junk backup into an empty junk store, ids kept")
    restore.add_argument("dir", help="the junk-<time> folder junk backup wrote")
    restore.set_defaults(command="junk restore")
    junk.add_parser("review", help="let Claude decide the open candidates (claude -p)").set_defaults(
        command="junk review"
    )


def _chapter_commands(command: argparse.ArgumentParser) -> None:
    """Add the chapter command's actions to its parser."""
    chapter = command.add_subparsers(dest="chapter_command", required=True, metavar="ACTION")
    use = chapter.add_parser("use", help="re-download a needs_human chapter at validate from this Suwayomi chapter")
    use.add_argument("chapter", type=int, help="chapter id")
    use.add_argument("suwayomi_chapter", type=int, help="Suwayomi chapter id of the replacement")
    use.set_defaults(command="chapter use")
    drop = chapter.add_parser("drop", help="give a needs_human chapter up: its download goes, it is never published")
    drop.add_argument("chapter", type=int, help="chapter id")
    drop.set_defaults(command="chapter drop")


def _source_commands(command: argparse.ArgumentParser) -> None:
    """Add the source command's actions to its parser."""
    source = command.add_subparsers(dest="source_command", required=True, metavar="ACTION")
    source.add_parser("survey", help="every series in scope by the triggers it crosses (reaches no service)")
    measure = source.add_parser("measure", help="a series' published files' measures and its linked manga")
    search = source.add_parser("search", help="search the ranked sources (or the named ones) for a series")
    search.add_argument("--source", action="append", default=[], metavar="NAME", help='a source, e.g. "Src (EN)"')
    sample = source.add_parser("sample", help="judge a manga's copies of a series' latest files (stores nothing)")
    sample.add_argument("--chapters", type=int, default=source_review.SAMPLE_CHAPTERS, help="files to sample")
    switch = source.add_parser("switch", help="link a manga to a series and add it to Suwayomi's library")
    replace = source.add_parser("replace", help="queue a linked manga's copies of published files")
    files = replace.add_mutually_exclusive_group(required=True)
    files.add_argument("--file", type=int, action="append", metavar="ID", help="a published file (source measure)")
    files.add_argument("--all", action="store_true", help="every confirmed published file")
    retire = source.add_parser("retire", help="take a manga out of Suwayomi's library and unlink it")
    for sub in (measure, search, sample, switch, replace, retire):
        sub.add_argument("series_id", type=int, help="series id (as status shows it)")
    for sub in (sample, switch, replace, retire):
        sub.add_argument("manga_id", type=int, help="Suwayomi manga id")
    for sub in (switch, replace, retire):
        sub.add_argument("--yes", action="store_true", required=True, help="confirm")
    for name, sub in source.choices.items():
        sub.set_defaults(command=f"source {name}")


def _state_commands(command: argparse.ArgumentParser) -> None:
    """Add the state command's actions to its parser."""
    state = command.add_subparsers(dest="state_command", required=True, metavar="ACTION")
    reset = state.add_parser("reset", help="clear the workflow state and the staging and scratch folders")
    adopt_ = state.add_parser("adopt", help="make this database the owner of the share")
    version = state.add_parser("version", help="make this machine's program, settings, packages and tools the fleet's")
    for sub, name in ((reset, "state reset"), (adopt_, "state adopt"), (version, "state version")):
        sub.add_argument("--yes", action="store_true", required=True, help="confirm")
        sub.set_defaults(command=name)
    state.add_parser("pause", help="pause the timers' and tasks' runs; returns once none is running").set_defaults(
        command="state pause"
    )
    state.add_parser("resume", help="end the pause").set_defaults(command="state resume")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line's command; return its exit code.

    Exit code: 0 done, 1 an unexpected error (logged with its traceback), a failed smoke check or a failed junk review,
    2 usage or config error or a refused command, 3 another run holds a lock the command takes, 4 a hard preflight
    check failed, the database server cannot be used, a command with locks finds no database, or the database is not
    one this version can use.
    """
    p = parser()
    args = p.parse_args(argv)
    command = COMMANDS[args.command]
    if args.scheduled and not command.automated:
        p.error(f"--scheduled is only for the commands the timers and tasks start, not {args.command}")
    try:
        cfg = config.init(args.config, args.set)
    except config.ConfigError as e:
        p.error(str(e))
    conns: list[db.Connection] = []
    try:
        try:
            conn = _open(cfg, command) if command.locked else None
            conns.extend([conn] if conn is not None else [])
            pause_conn = None
            if args.scheduled:  # the connection holding lock.AUTOMATION: watch, unlocked, gets one of its own
                pause_conn = conn if conn is not None else db.open_database(cfg, builds=False)
                conns.append(pause_conn)
        except (db.CannotConnectError, db.NoDatabaseError) as e:
            # Not logged: the log file is opened only under the run's locks.
            print(f"error: {e}", file=sys.stderr)
            return 4
        except config.ConfigError as e:
            p.error(str(e))
        if pause_conn is None:
            return _under_locks(p, cfg, command, args, conn)
        return _scheduled(p, cfg, command, args, (conn, pause_conn))
    finally:
        for opened in dict.fromkeys(conns):
            opened.close()


def _scheduled(
    p: argparse.ArgumentParser,
    cfg: config.Config,
    command: Command,
    args: argparse.Namespace,
    conns: tuple[db.Connection | None, db.Connection],
) -> int:
    """Run a scheduled command: hold lock.AUTOMATION, then read the pause; paused, end at once (exit 0).

    Holding it before the read is what lets state pause wait for every run that read no pause (SPEC "Run lock"). conns
    is (the command's connection, None for one without locks; the connection holding the lock).
    """
    conn, pause_conn = conns
    with lock.held(pause_conn, cfg, (), automation=True):
        since = pause.paused(pause_conn)
        if since is not None:
            print(f"automation paused since {since}: nothing run")
            return 0
        return _under_locks(p, cfg, command, args, conn)


def _under_locks(
    p: argparse.ArgumentParser,
    cfg: config.Config,
    command: Command,
    args: argparse.Namespace,
    conn: db.Connection | None,
) -> int:
    """Run the command under its locks (SPEC "Run lock"), its log set up there; return its exit code.

    3 when another run holds a lock it takes.
    """
    names = tuple(workflow.owner(name) for name in command.locks)
    try:
        with lock.held(locked_connection(conn), cfg, names, command.idle) if command.locked else nullcontext():
            try:
                logs.setup(cfg, command.log)
            except config.ConfigError as e:
                p.error(str(e))
            return _run(p, cfg, command, args, conn)
    except lock.LockHeld as e:
        # Not logged: a refused run must not open the log file while the holder writes it.
        print(f"error: {e}", file=sys.stderr)
        return 3


def _run(
    p: argparse.ArgumentParser,
    cfg: config.Config,
    command: Command,
    args: argparse.Namespace,
    conn: db.Connection | None,
) -> int:
    """Run the command's preflight checks, schema steps and handler; return its exit code (main's docstring)."""
    try:
        preflight.run(cfg, command.checks)
        if conn is not None:
            db.apply_steps(conn)
        command.handler(cfg, args, conn)
    except preflight.PreflightError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except workflow.Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (db.SchemaError, db.CannotConnectError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except config.ConfigError as e:  # a database setting, checked where the database is opened
        p.error(str(e))
    except (smoke.SmokeError, review_run.ReviewError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception:
        # The only place an unexpected error is logged; inside the lock, which guards the log file.
        log.exception("%s: failed", args.command)
        return 1
    return 0


def _open(cfg: config.Config, command: Command) -> db.Connection:
    """Open the command's 1 connection, before its locks (SPEC "Run lock").

    A command that builds creates a missing database; for any other, a database missing or holding no schema record is
    NoDatabaseError.
    """
    return db.open_database(cfg, builds=command.builds)


if __name__ == "__main__":
    sys.exit(main())
