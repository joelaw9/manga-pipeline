"""Command-line entry point (SPEC "CLI"): python -m manga_pipeline.cli"""

import argparse
import logging
import sys

from manga_pipeline import config, lock, logs, preflight

# Not __name__: under python -m it is "__main__", outside the package logger that logs.setup configures.
log = logging.getLogger("manga_pipeline.cli")
DESCRIPTION = "Suwayomi to Kavita manga pipeline: junk removal, metadata, bad-download repair, AVIF conversion"


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
    return p


def main(argv=None):
    """Exit code: 0 done, 2 usage or config error, 3 another run holds the run lock, 4 a hard preflight check
    failed."""
    p = parser()
    args = p.parse_args(argv)
    try:
        cfg = config.init(args.config, args.set)
    except config.ConfigError as e:
        p.error(str(e))
    try:
        with lock.held(cfg):
            try:
                logs.setup(cfg)
            except config.ConfigError as e:
                p.error(str(e))
            try:
                preflight.run(cfg, preflight.CHECKS)
            except preflight.PreflightFailed as e:
                print(f"error: {e}", file=sys.stderr)
                return 4
            log.info("started")
    except lock.LockHeld as e:
        # Not logged: a refused run must not open application.log while the holder writes it.
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
