"""Command-line entry point (SPEC "CLI", "Run lock", "Logging", "Preflight"): python -m manga_pipeline.cli

COMMANDS is the command table: each command's locks, log file, preflight checks and handler. Every command runs in
the same order: config, its connection and locks (a command with locks), logging, its checks, the schema steps (a
command with locks), its handler.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass

from manga_pipeline import (
    archive,
    config,
    db,
    errors,
    export,
    hand_edits,
    handled,
    identify,
    intake,
    lock,
    logs,
    preflight,
    process,
    retention,
    rewrite,
    smoke,
    state,
    status,
    volumes,
    workflow,
)
from manga_pipeline.clients import kavita, suwayomi
from manga_pipeline.db import sql
from manga_pipeline.junk import candidates, importing, review, review_run
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
PART_CHECKS = (*LOCKED_CHECKS, preflight.WORKFLOW_SETTINGS, preflight.SHARE_OWNERSHIP, preflight.FLEET_VERSION)
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
EXPORT_CHECKS = (*PART_CHECKS, preflight.SERVICE_SETTINGS, preflight.NAMING_OVERRIDES)
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
# smoke takes no lock and touches no folder: it needs only what the clients and scope read.
SMOKE_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SERVICE_SETTINGS)
# series link reads both services and writes 1 series' link: what the clients and scope read.
LINK_CHECKS = SMOKE_CHECKS
# metadata import writes series rows and ids under intake's lock, reading both services.
IMPORT_CHECKS = SMOKE_CHECKS
# series edit reads the series' open downloads on the share and its override file to name its chapters.
EDIT_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SHARE_FOLDERS_PRESENT, preflight.NAMING_OVERRIDES)


@dataclass(frozen=True)
class Command:
    locks: tuple[str, ...]  # run locks taken for the run (a part's name stands for workflow.owner of it); empty = none
    idle: str | None  # how the run holds lock.IDLE: lock.SHARED, lock.EXCLUSIVE or None
    log: str | None  # log file name (<log>.log); None = console only
    checks: tuple[preflight.Check, ...]
    # handler(cfg, args, conn): conn is the command's connection holding its locks, None for a command without any
    handler: Callable[[dict, argparse.Namespace, object], None]
    builds: bool = False  # creates the database when the server has none, and builds one holding no schema record

    @property
    def locked(self):
        return bool(self.locks or self.idle)


def retry(cfg, args):
    with _database(cfg, "retry") as conn:
        if args.series is None:
            workflow.retry_chapter(conn, args.chapter)
            log.info("chapter %s is ready again", args.chapter)
        else:
            workflow.retry_series(conn, args.series)
            log.info("series %s and its needs_human chapters are ready again", args.series)


def _database(cfg, what):
    """The built database, for a command that never builds one (SPEC "State")."""
    if not db.built(cfg):
        msg = f"no database {cfg['database']['name']} built yet: nothing to {what}"
        raise workflow.Refused(msg)
    return closing(db.connect(cfg))


def chapter_use(cfg, args):
    with _database(cfg, "use") as conn:
        workflow.use_chapter(conn, args.chapter, args.suwayomi_chapter)
    log.info("chapter %s will be re-downloaded from Suwayomi chapter %s", args.chapter, args.suwayomi_chapter)


def chapter_drop(cfg, args):
    """The download goes the retention way first, then the chapter is done: a crash between leaves it needs_human,
    and the next drop finds its download already gone.
    """
    with _database(cfg, "drop") as conn:
        row = workflow.droppable(conn, args.chapter)
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
            if digest is None and errors.exists(download):  # held at discover, before its digest was settled
                digest = archive.digest_or_none(download)
            retention.put(cfg, download)
        workflow.drop_chapter(
            conn, args.chapter, lambda c: handled.record(c, row["series_id"], row["identity"], "dropped", digest)
        )
    log.info("chapter %s dropped", args.chapter)


def series_link(cfg, args):
    with _database(cfg, "link") as conn:
        identify.link(
            cfg,
            conn,
            kavita.from_config(cfg),
            args.kavita_series_id,
            identify.link_subject(cfg, suwayomi.from_config(cfg), manga_id=args.manga_id, folder=args.folder),
        )


def series_set_id(cfg, args):
    with _database(cfg, "set an id in") as conn:
        found = human.set_id(conn, args.series_id, args.source, args.id)
    log.info("series %s: %s id %s set by hand", args.series_id, args.source, found)


def series_refetch_covers(cfg, args):
    with _database(cfg, "refetch covers in") as conn:
        human.refetch_covers(conn, args.series_id)
    log.info("series %s: its check is due now: the next intake refreshes its volume covers", args.series_id)


def series_new(cfg, args):
    with _database(cfg, "admit a series in") as conn:
        human.new_series(conn, args.series_id, args.name)
    log.info("series %s: to be admitted as a new series%s", args.series_id, f" as {args.name!r}" if args.name else "")


def series_edit(cfg, args):
    data = hand_edits.load(args.file)
    with _database(cfg, "edit") as conn:
        lines = hand_edits.apply(workflow.context(cfg, conn, "export"), data, args.series_id)
    for line in lines:
        print(line)


def naming_diff(cfg, args):
    """Read-only: what the comparison would rewrite, and the differences 7e never applies (SPEC "Retroactive
    updates").
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


def rewrite_drop(cfg, args):
    with _database(cfg, "drop a rewrite in") as conn:
        row = rewrite.drop(conn, args.file_id, cfg["workflow"]["attempt_limit"])
    if row["pending_staged"]:
        config.to_local(cfg, row["pending_staged"]).unlink(missing_ok=True)
    log.info("published file %s: rewrite dropped", args.file_id)


def rewrite_retry(cfg, args):
    with _database(cfg, "retry a rewrite in") as conn:
        rewrite.retry(conn, args.file_id)
    log.info("published file %s: rewrite tried again by the next export", args.file_id)


def metadata_import(cfg, args, conn):
    state, overrides = importer.read_state(args.state_file), importer.read_overrides(args.config_file)
    importer.run(workflow.context(cfg, conn, "intake"), state, overrides)


def metadata_audit(cfg, args):
    """Opens no database (SPEC "Metadata checks", Audit): it runs before cutover, with or without one."""
    state = importer.read_state(args.state) if args.state else None
    audit.run(cfg, kavita.from_config(cfg), suwayomi.from_config(cfg), sources.new_sources(), state)


def junk_import(cfg, args):
    try:
        source = importing.read_store(args.path) if args.path else importing.read_database(cfg, args.database)
    except importing.SourceError as e:
        raise workflow.Refused(str(e)) from None
    with closing(db.connect(cfg)) as conn:
        counts = importing.run(conn, source)
    skipped = ", ".join(f"{what} {n}" for what, n in counts["skipped"].items()) or "none"
    print(
        f"imported decisions: added {counts['added']}, removed {counts['removed']}, changed {counts['changed']}, "
        f"unchanged {counts['unchanged']}; left to the pipeline's own decisions {counts['pipeline_kept']}; "
        f"imported sightings {counts['seen']}; "
        f"not imported: {skipped}"
    )


def _junk_db(cfg):
    return _database(cfg, "list, show or decide")


def _folder(cfg, row):
    return candidates.folder(cfg, row["folder"])


def junk_list(cfg, args):
    with _junk_db(cfg) as conn:
        try:
            rows = review.listing(conn, cfg, os.environ.get(review.REVIEW_ID_VARIABLE), everything=args.all)
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
        for r in rows:
            folder = _folder(cfg, r)
            fields = [str(r["id"]), r["series_key"], r["kind"], f"chapters {review.chapters(conn, r['id'])}"]
            if args.all:
                fields += [f"reviews {r['reviews']}", f"verdict {r['verdict'] or '-'}"]
            print("	".join([*fields, str(folder / "unit.png"), str(folder / "context.png")]))


def junk_show(cfg, args):
    with _junk_db(cfg) as conn:
        try:
            row = review.get_open(conn, args.id)
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
        folder = _folder(cfg, row)
        info = json.loads((folder / "info.json").read_bytes())
        print(f"candidate {row['id']}: {row['kind']} of series {row['series_key']}")
        print(f"why: {row['reason']}")
        print(f"chapters: {review.chapters(conn, row['id'])}")
        print(f"text: {info['text'] or '-'}")
        print(f"unit: {folder / 'unit.png'}")
        print(f"context: {folder / 'context.png'}")


def junk_decide(cfg, args):
    decider = "claude" if os.environ.get(review.REVIEW_ID_VARIABLE) else "human"
    with _junk_db(cfg) as conn:
        try:
            review.decide(conn, args.id, args.verdict, decider, workflow.now(conn))
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
    log.info("junk candidate %s decided %s by %s", args.id, args.verdict, decider)


def _unlocked(handler):
    """A command without locks: its handler opens what it needs itself."""
    return lambda cfg, args, _conn: handler(cfg, args)


S, X = lock.SHARED, lock.EXCLUSIVE
COMMANDS = {
    "intake": Command(("intake",), S, "intake", INTAKE_CHECKS, lambda cfg, _args, conn: intake.run(cfg, conn)),
    "process": Command(("process",), S, "process", PROCESS_CHECKS, lambda cfg, _args, conn: process.run(cfg, conn)),
    "export": Command(("export",), S, "export", EXPORT_CHECKS, lambda cfg, _args, conn: export.run(cfg, conn)),
    "status": Command((), None, None, (), _unlocked(lambda cfg, _args: status.run(cfg))),
    "retry": Command((), None, None, (), _unlocked(retry)),
    "chapter use": Command((), None, None, (), _unlocked(chapter_use)),
    "chapter drop": Command((), None, None, (), _unlocked(chapter_drop)),
    "smoke": Command((), None, None, SMOKE_CHECKS, _unlocked(lambda cfg, _args: smoke.run(cfg))),
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
    "junk import": Command((), None, None, (), _unlocked(junk_import)),
    "junk list": Command((), None, None, (), _unlocked(junk_list)),
    "junk show": Command((), None, None, (), _unlocked(junk_show)),
    "junk decide": Command((), None, None, (), _unlocked(junk_decide)),
    "junk review": Command(
        ("junk-review",), S, "junk-review", REVIEW_CHECKS, lambda cfg, _args, conn: review_run.run(cfg, conn)
    ),
    "state reset": Command((), X, None, RESET_CHECKS, lambda cfg, _args, conn: state.reset(cfg, conn)),
    "state adopt": Command((), X, None, ADOPT_CHECKS, lambda cfg, _args, conn: state.adopt(cfg, conn), builds=True),
    "state version": Command((), X, None, VERSION_CHECKS, lambda cfg, _args, conn: state.version(cfg, conn)),
}


def parser():
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
    commands = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("intake", help="discover, validate and identify new downloads")
    commands.add_parser("process", help="junk removal and conversion")
    commands.add_parser("export", help="ComicInfo.xml, publish into the library, confirm")
    commands.add_parser("status", help="what is waiting, on what, and what needs a human")
    commands.add_parser("smoke", help="check Suwayomi and Kavita with reads only")
    _retry_command(commands)
    _series_commands(commands)
    _naming_commands(commands)
    _rewrite_commands(commands)
    _metadata_commands(commands)
    _junk_commands(commands)
    _chapter_commands(commands)
    _state_commands(commands)
    return p


def _retry_command(commands):
    """Add the retry command to the subparsers."""
    retry = commands.add_parser("retry", help="make a needs_human chapter or series ready again")
    target = retry.add_mutually_exclusive_group(required=True)
    target.add_argument("chapter", nargs="?", type=int, help="chapter id")
    target.add_argument("--series", type=int, metavar="ID", help="a series and its chapters")


def _series_commands(commands):
    """Add the series commands to the subparsers."""
    series = commands.add_parser("series", help="series identification").add_subparsers(
        dest="series_command", required=True, metavar="ACTION"
    )
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


def _naming_commands(commands):
    """Add the naming commands to the subparsers."""
    naming = commands.add_parser("naming", help="chapter naming").add_subparsers(
        dest="naming_command", required=True, metavar="ACTION"
    )
    diff = naming.add_parser("diff", help="list what retroactive updates would rewrite, and what they never apply")
    diff.add_argument("series_id", nargs="?", type=int, help="series id (default: every series in scope)")
    diff.set_defaults(command="naming diff")


def _rewrite_commands(commands):
    """Add the rewrite commands to the subparsers."""
    rewrites = commands.add_parser("rewrite", help="retroactive rewrites of published files").add_subparsers(
        dest="rewrite_command", required=True, metavar="ACTION"
    )
    drop_rewrite = rewrites.add_parser("drop", help="give up a stuck rewrite before its rename (as status names it)")
    drop_rewrite.add_argument("file_id", type=int, help="published file id")
    drop_rewrite.set_defaults(command="rewrite drop")
    retry_rewrite = rewrites.add_parser("retry", help="try a failed rewrite again from its next step")
    retry_rewrite.add_argument("file_id", type=int, help="published file id")
    retry_rewrite.set_defaults(command="rewrite retry")


def _metadata_commands(commands):
    """Add the metadata commands to the subparsers."""
    metadata = commands.add_parser("metadata", help="series metadata").add_subparsers(
        dest="metadata_command", required=True, metavar="ACTION"
    )
    import_metadata = metadata.add_parser("import", help="import the old metadata tool's ids, covers and overrides")
    import_metadata.add_argument("state_file", help="its .sync_state.json (only read)")
    import_metadata.add_argument("config_file", help="its config.yaml (only read)")
    import_metadata.set_defaults(command="metadata import")
    audit_ = metadata.add_parser("audit", help="list every library series whose source ids disagree (read-only)")
    audit_.add_argument("--state", metavar="PATH", help="the old metadata tool's .sync_state.json (only read)")
    audit_.set_defaults(command="metadata audit")


def _junk_commands(commands):
    """Add the junk commands to the subparsers."""
    junk = commands.add_parser("junk", help="junk decisions").add_subparsers(
        dest="junk_command", required=True, metavar="ACTION"
    )
    import_ = junk.add_parser("import", help="import decisions from a bulk-converter junk store or a pipeline database")
    source = import_.add_mutually_exclusive_group(required=True)
    source.add_argument("path", nargs="?", help="the bulk converter's junk.sqlite3 (only read)")
    source.add_argument("--database", help="another pipeline database on the same server (only read)")
    import_.set_defaults(command="junk import")
    list_ = junk.add_parser("list", help="the open junk candidates to decide")
    list_.add_argument("--all", action="store_true", help="every open candidate, stuck and unsure ones included")
    list_.set_defaults(command="junk list")
    show = junk.add_parser("show", help="a candidate's images, OCR text and reason")
    show.add_argument("id", type=int, help="candidate id")
    show.set_defaults(command="junk show")
    decide = junk.add_parser("decide", help="record a verdict on a candidate")
    decide.add_argument("id", type=int, help="candidate id")
    decide.add_argument("verdict", choices=review.VERDICTS, help="junk, keep or unsure")
    decide.set_defaults(command="junk decide")
    junk.add_parser("review", help="let Claude decide the open candidates (claude -p)").set_defaults(
        command="junk review"
    )


def _chapter_commands(commands):
    """Add the chapter commands to the subparsers."""
    chapter = commands.add_parser("chapter", help="settle a needs_human chapter").add_subparsers(
        dest="chapter_command", required=True, metavar="ACTION"
    )
    use = chapter.add_parser("use", help="re-download a needs_human chapter at validate from this Suwayomi chapter")
    use.add_argument("chapter", type=int, help="chapter id")
    use.add_argument("suwayomi_chapter", type=int, help="Suwayomi chapter id of the replacement")
    use.set_defaults(command="chapter use")
    drop = chapter.add_parser("drop", help="give a needs_human chapter up: its download goes, it is never published")
    drop.add_argument("chapter", type=int, help="chapter id")
    drop.set_defaults(command="chapter drop")


def _state_commands(commands):
    """Add the state commands to the subparsers."""
    state = commands.add_parser("state", help="workflow state and share ownership").add_subparsers(
        dest="state_command", required=True, metavar="ACTION"
    )
    reset = state.add_parser("reset", help="clear the workflow state and the staging and scratch folders")
    adopt_ = state.add_parser("adopt", help="make this database the owner of the share")
    version = state.add_parser("version", help="make this machine's program, settings, packages and tools the fleet's")
    for sub, name in ((reset, "state reset"), (adopt_, "state adopt"), (version, "state version")):
        sub.add_argument("--yes", action="store_true", required=True, help="confirm")
        sub.set_defaults(command=name)


def main(argv=None):
    """Exit code: 0 done, 1 an unexpected error (logged with its traceback), a failed smoke check or a failed junk
    review, 2 usage or config error or a refused command, 3 another run holds a lock the command takes, 4 a hard
    preflight check failed, the database server cannot be used, a command with locks finds no database, or the
    database is not one this version can use.
    """
    p = parser()
    args = p.parse_args(argv)
    command = COMMANDS[args.command]
    try:
        cfg = config.init(args.config, args.set)
    except config.ConfigError as e:
        p.error(str(e))
    conn = None
    try:
        if command.locked:
            try:
                conn = _open(cfg, command)
            except (db.CannotConnectError, db.NoDatabaseError) as e:
                # Not logged: the log file is opened only under the run's locks.
                print(f"error: {e}", file=sys.stderr)
                return 4
            except config.ConfigError as e:
                p.error(str(e))
        return _under_locks(p, cfg, command, args, conn)
    finally:
        if conn is not None:
            conn.close()


def _under_locks(p, cfg, command, args, conn):
    """Run the command under its locks (SPEC "Run lock"), its log set up there; its exit code (3 when another run holds
    a lock it takes).
    """
    names = tuple(workflow.owner(name) for name in command.locks)
    try:
        with lock.held(conn, cfg, names, command.idle) if command.locked else nullcontext():
            try:
                logs.setup(cfg, command.log)
            except config.ConfigError as e:
                p.error(str(e))
            return _run(p, cfg, command, args, conn)
    except lock.LockHeld as e:
        # Not logged: a refused run must not open the log file while the holder writes it.
        print(f"error: {e}", file=sys.stderr)
        return 3


def _run(p, cfg, command, args, conn):
    """The command's preflight checks, schema steps and handler; its exit code (main's docstring)."""
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


def _open(cfg, command):
    """The command's 1 connection, opened before its locks (SPEC "Run lock"): a command that builds creates a missing
    database; for any other, a database missing or holding no schema record is NoDatabaseError.
    """
    return db.open_database(cfg, builds=command.builds)


if __name__ == "__main__":
    sys.exit(main())
