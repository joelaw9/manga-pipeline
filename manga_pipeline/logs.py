"""Logging setup (SPEC "Logging"): console plus <logging.dir>/application.log, a new file each midnight."""

import logging
from logging.handlers import TimedRotatingFileHandler

from manga_pipeline import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def setup(cfg):
    """Configure the package logger from cfg["logging"], replacing any earlier setup. Returns the log file path."""
    settings = cfg["logging"]
    if settings["level"] not in LEVELS:
        raise config.ConfigError(f"logging.level must be one of {', '.join(LEVELS)}, got {settings['level']!r}")
    if settings["keep_days"] < 1:
        raise config.ConfigError(f"logging.keep_days must be at least 1, got {settings['keep_days']}")
    folder = config.abs_path(settings["dir"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "application.log"
    logger = logging.getLogger("manga_pipeline")
    for old in logger.handlers[:]:
        logger.removeHandler(old)
        old.close()
    for handler in (
        logging.StreamHandler(),
        TimedRotatingFileHandler(path, when="midnight", backupCount=settings["keep_days"], encoding="utf-8"),
    ):
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    logger.setLevel(settings["level"])
    return path
