"""Logging setup (SPEC "Logging"): console, plus <logging.dir>/<command>.log for a command under its own lock.

The log file starts a new file each midnight.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from manga_pipeline import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def setup(cfg: config.Config, command: str | None) -> Path | None:
    """Configure the package logger from cfg["logging"], replacing any earlier setup.

    The console, plus the log file <command>.log unless command is None. Returns the log file path, or None.
    """
    settings = cfg["logging"]
    if settings["level"] not in LEVELS:
        msg = f"logging.level must be one of {', '.join(LEVELS)}, got {settings['level']!r}"
        raise config.ConfigError(msg)
    if settings["keep_days"] < 1:
        msg = f"logging.keep_days must be at least 1, got {settings['keep_days']}"
        raise config.ConfigError(msg)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path = None
    if command is not None:
        folder = config.abs_path(settings["dir"])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{command}.log"
        handlers.append(
            TimedRotatingFileHandler(path, when="midnight", backupCount=settings["keep_days"], encoding="utf-8")
        )
    logger = logging.getLogger("manga_pipeline")
    for old in logger.handlers[:]:
        logger.removeHandler(old)
        old.close()
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    logger.setLevel(settings["level"])
    return path
