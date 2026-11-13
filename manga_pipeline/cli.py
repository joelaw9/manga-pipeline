"""Command-line entry point (SPEC "CLI", "Run lock", "Logging", "Preflight"): python -m manga_pipeline.cli

COMMANDS is the command table: each command's locks, log file, preflight checks and handler. Every command runs in
the same order: config, its locks, logging, its checks, its handler.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from manga_pipeline import (
    archive,
    config,
    db,
    export,
    handled,
    identify,
    intake,
    lock,
    logs,
    preflight,
    process,
    retention,
    smoke,
    state,
    status,
    workflow,
)
from manga_pipeline.clients import kavita, suwayomi
from manga_pipeline.junk import candidates, importing, review, review_run

# Not __name__: under python -m it is "__main__", outside the package logger that logs.setup configures.
log = logging.getLogger("manga_pipeline.cli")
DESCRIPTION = "Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion"
PART_LOCKS = ("intake", "process", "export", "junk-review")
# Every command that takes a lock runs these.
LOCKED_CHECKS = (
    preflight.PYTHON_VERSION,
    preflight.FREE_SPACE,
    preflight.DATABASE_LOCATION,
    preflight.FOLDER_LAYOUT,
    preflight.SHARE_FOLDERS_PRESENT,
)
PART_CHECKS = (*LOCKED_CHECKS, preflight.WORKFLOW_SETTINGS, preflight.SHARE_OWNERSHIP)
# process runs OCR and the image tools and scorer; it reaches no service.
PROCESS_CHECKS = (*PART_CHECKS, preflight.OCR_DEVICE, preflight.IMAGE_TOOLS, preflight.SCORER, preflight.SCRATCH_SPACE)
# intake and export reach Suwayomi and Kavita.
SERVICE_PART_CHECKS = (*PART_CHECKS, preflight.SERVICE_SETTINGS)
RESET_CHECKS = (*LOCKED_CHECKS, preflight.SHARE_OWNERSHIP)
# state adopt writes the owner file, so it cannot require one; it needs the folder the file goes in.
ADOPT_CHECKS = (*LOCKED_CHECKS, preflight.STAGING_FOLDER)
# junk review touches local disk only and starts Claude.
REVIEW_CHECKS = (
    preflight.PYTHON_VERSION,
    preflight.FREE_SPACE,
    preflight.DATABASE_LOCATION,
    preflight.FOLDER_LAYOUT,
    preflight.WORKFLOW_SETTINGS,
    preflight.CLAUDE_SETTINGS,
)
# smoke takes no lock and touches no folder: it needs only what the clients and scope read.
SMOKE_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SERVICE_SETTINGS)
# series link reads both services and writes 1 series' link: what the clients and scope read.
LINK_CHECKS = SMOKE_CHECKS


@dataclass(frozen=True)
class Command:
    locks: tuple[str, ...]  # lock names taken for the run; empty = none
    log: str | None  # log file name (<log>.log); None = console only
    checks: tuple[preflight.Check, ...]
    handler: Callable[[dict, argparse.Namespace], None]


def retry(cfg, args):
    path = config.abs_path(cfg["database"]["path"])
    if not path.is_file():
        raise workflow.Refused(f"no database at {path}: nothing to retry")
    with closing(db.connect(cfg)) as conn:
        if args.series is None:
            workflow.retry_chapter(conn, args.chapter)
            log.info("chapter %s is ready again", args.chapter)
        else:
            workflow.retry_series(conn, args.series)
            log.info("series %s and its needs_human chapters are ready again", args.series)


def _database(cfg, what):
    path = config.abs_path(cfg["database"]["path"])
    if not path.is_file():
        raise workflow.Refused(f"no database at {path}: nothing to {what}")
    return closing(db.connect(cfg))


def chapter_use(cfg, args):
    with _database(cfg, "use") as conn:
        workflow.use_chapter(conn, args.chapter, args.suwayomi_chapter)
    log.info("chapter %s will be re-downloaded from Suwayomi chapter %s", args.chapter, args.suwayomi_chapter)


def chapter_drop(cfg, args):
    """The download goes the retention way first, then the chapter is done: a crash between leaves it needs_human,
    and the next drop finds its download already gone."""
    with _database(cfg, "drop") as conn:
        row = workflow.droppable(conn, args.chapter)
        digest = row["content_digest"]
        if row["download_path"]:
            download = Path(row["download_path"])
            if digest is None and download.exists():  # held at discover, before its digest was settled
                digest = archive.digest_or_none(download)
            retention.put(cfg, download)
        workflow.drop_chapter(
            conn, args.chapter, lambda c: handled.record(c, row["series_id"], row["identity"], "dropped", digest)
        )
    log.info("chapter %s dropped", args.chapter)


def series_link(cfg, args):
    path = config.abs_path(cfg["database"]["path"])
    if not path.is_file():
        raise workflow.Refused(f"no database at {path}: nothing to link")
    with closing(db.connect(cfg)) as conn:
        identify.link(
            cfg, conn, suwayomi.from_config(cfg), kavita.from_config(cfg), args.manga_id, args.kavita_series_id
        )


def junk_import(cfg, args):
    with closing(db.connect(cfg)) as conn:
        try:
            counts = importing.run(conn, args.path, config.abs_path(cfg["database"]["path"]))
        except importing.SourceError as e:
            raise workflow.Refused(str(e)) from None
    skipped = ", ".join(f"{what} {n}" for what, n in counts["skipped"].items()) or "none"
    print(
        f"imported decisions: added {counts['added']}, removed {counts['removed']}, changed {counts['changed']}, "
        f"unchanged {counts['unchanged']}; left to the pipeline's own decisions {counts['pipeline_kept']}; "
        f"imported sightings {counts['seen']}; "
        f"not imported: {skipped}"
    )


def _junk_db(cfg):
    path = config.abs_path(cfg["database"]["path"])
    if not path.is_file():
        raise workflow.Refused(f"no database at {path}")
    return closing(db.connect(cfg))


def _folder(cfg, row):
    return candidates.folder(cfg, row["folder"])


def junk_list(cfg, args):
    with _junk_db(cfg) as conn:
        try:
            rows = review.listing(conn, cfg, os.environ.get(review.REVIEW_ID_VARIABLE), args.all)
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
            review.decide(conn, args.id, args.verdict, decider, workflow.now())
        except review.Refused as e:
            raise workflow.Refused(str(e)) from None
    log.info("junk candidate %s decided %s by %s", args.id, args.verdict, decider)


COMMANDS = {
    "intake": Command(("intake",), "intake", SERVICE_PART_CHECKS, lambda cfg, args: intake.run(cfg)),
    "process": Command(("process",), "process", PROCESS_CHECKS, lambda cfg, args: process.run(cfg)),
    "export": Command(("export",), "export", SERVICE_PART_CHECKS, lambda cfg, args: export.run(cfg)),
    "status": Command((), None, (), lambda cfg, args: status.run(cfg)),
    "retry": Command((), None, (), retry),
    "chapter use": Command((), None, (), chapter_use),
    "chapter drop": Command((), None, (), chapter_drop),
    "smoke": Command((), None, SMOKE_CHECKS, lambda cfg, args: smoke.run(cfg)),
    "series link": Command((), None, LINK_CHECKS, series_link),
    "junk import": Command((), None, (), junk_import),
    "junk list": Command((), None, (), junk_list),
    "junk show": Command((), None, (), junk_show),
    "junk decide": Command((), None, (), junk_decide),
    "junk review": Command(("junk-review",), "junk-review", REVIEW_CHECKS, lambda cfg, args: review_run.run(cfg)),
    "state reset": Command(PART_LOCKS, None, RESET_CHECKS, lambda cfg, args: state.reset(cfg)),
    "state adopt": Command(PART_LOCKS, None, ADOPT_CHECKS, lambda cfg, args: state.adopt(cfg)),
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
    retry = commands.add_parser("retry", help="make a needs_human chapter or series ready again")
    target = retry.add_mutually_exclusive_group(required=True)
    target.add_argument("chapter", nargs="?", type=int, help="chapter id")
    target.add_argument("--series", type=int, metavar="ID", help="a series and its chapters")
    link = (
        commands.add_parser("series", help="series identification")
        .add_subparsers(dest="series_command", required=True, metavar="ACTION")
        .add_parser("link", help="link a Suwayomi manga to a Kavita series (or refresh that link's names and folder)")
    )
    link.add_argument("manga_id", type=int, help="Suwayomi manga id")
    link.add_argument("kavita_series_id", type=int, help="Kavita series id")
    link.set_defaults(command="series link")
    junk = commands.add_parser("junk", help="junk decisions").add_subparsers(
        dest="junk_command", required=True, metavar="ACTION"
    )
    import_ = junk.add_parser("import", help="import decisions from a bulk-converter junk store or a pipeline database")
    import_.add_argument("path", help="the database file (only read)")
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
    state = commands.add_parser("state", help="workflow state and share ownership").add_subparsers(
        dest="state_command", required=True, metavar="ACTION"
    )
    reset = state.add_parser("reset", help="clear the workflow state and the staging and scratch folders")
    adopt_ = state.add_parser("adopt", help="make this database the owner of the share")
    for sub, name in ((reset, "state reset"), (adopt_, "state adopt")):
        sub.add_argument("--yes", action="store_true", required=True, help="confirm")
        sub.set_defaults(command=name)
    return p


def main(argv=None):
    """Exit code: 0 done, 1 an unexpected error (logged with its traceback), a failed smoke check or a failed junk
    review, 2 usage or config error or a refused command, 3 another run holds a lock the command takes, 4 a hard
    preflight check failed or the database is not one this version can use."""
    p = parser()
    args = p.parse_args(argv)
    command = COMMANDS[args.command]
    try:
        cfg = config.init(args.config, args.set)
    except config.ConfigError as e:
        p.error(str(e))
    try:
        with lock.held(cfg, *command.locks):
            try:
                logs.setup(cfg, command.log)
            except config.ConfigError as e:
                p.error(str(e))
            try:
                preflight.run(cfg, command.checks)
                command.handler(cfg, args)
            except preflight.PreflightFailed as e:
                print(f"error: {e}", file=sys.stderr)
                return 4
            except workflow.Refused as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            except db.SchemaError as e:
                print(f"error: {e}", file=sys.stderr)
                return 4
            except (smoke.Failed, review_run.ReviewFailed) as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            except Exception:
                # The only place an unexpected error is logged; inside the lock, which guards the log file.
                log.exception("%s: failed", args.command)
                return 1
    except lock.LockHeld as e:
        # Not logged: a refused run must not open the log file while the holder writes it.
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
