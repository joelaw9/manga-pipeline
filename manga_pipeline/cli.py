"""Command-line entry point (SPEC "CLI", "Run lock", "Logging", "Preflight"): python -m manga_pipeline.cli

COMMANDS is the command table: each command's locks, log file, preflight checks and handler. Every command runs in
the same order: config, its locks, logging, its checks, its handler.
"""

import argparse
import logging
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

from manga_pipeline import config, db, export, intake, lock, logs, preflight, process, smoke, state, status, workflow

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
RESET_CHECKS = (*LOCKED_CHECKS, preflight.SHARE_OWNERSHIP)
# state adopt writes the owner file, so it cannot require one; it needs the folder the file goes in.
ADOPT_CHECKS = (*LOCKED_CHECKS, preflight.STAGING_FOLDER)
# smoke takes no lock and touches no folder: it needs only what the clients and scope read.
SMOKE_CHECKS = (preflight.WORKFLOW_SETTINGS, preflight.SERVICE_SETTINGS)


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


COMMANDS = {
    "intake": Command(("intake",), "intake", PART_CHECKS, lambda cfg, args: intake.run(cfg)),
    "process": Command(("process",), "process", PART_CHECKS, lambda cfg, args: process.run(cfg)),
    "export": Command(("export",), "export", PART_CHECKS, lambda cfg, args: export.run(cfg)),
    "status": Command((), None, (), lambda cfg, args: status.run(cfg)),
    "retry": Command((), None, (), retry),
    "smoke": Command((), None, SMOKE_CHECKS, lambda cfg, args: smoke.run(cfg)),
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
    """Exit code: 0 done, 1 a failed smoke check, 2 usage or config error or a refused retry, 3 another run holds a
    lock the command takes, 4 a hard preflight check failed or the database is not one this version can use."""
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
            except preflight.PreflightFailed as e:
                print(f"error: {e}", file=sys.stderr)
                return 4
            try:
                command.handler(cfg, args)
            except workflow.Refused as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            except db.SchemaError as e:
                print(f"error: {e}", file=sys.stderr)
                return 4
            except smoke.Failed as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
    except lock.LockHeld as e:
        # Not logged: a refused run must not open the log file while the holder writes it.
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
