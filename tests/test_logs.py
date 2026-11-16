"""logs.setup: console, plus a daily-rotated <command>.log for a command with its own lock (SPEC "Logging")."""

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from manga_pipeline import config, logs

from tests.timing import budget


@pytest.fixture
def logger():
    yield logging.getLogger("manga_pipeline")
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


def in_dir(folder):
    return {**config.cached(), "logging": {**config.cached()["logging"], "dir": str(folder)}}


@budget(load="fs")
def test_records_reach_the_commands_log_file(logger, tmp_path):
    path = logs.setup(in_dir(tmp_path), "intake")
    logging.getLogger("manga_pipeline.cli").info("hello")
    assert path == tmp_path / "intake.log"
    assert "hello" in path.read_text(encoding="utf-8"), "a package record must be written to the command's log file"


@budget(load="fs")
def test_no_command_logs_to_the_console_only(logger, tmp_path):
    assert logs.setup(in_dir(tmp_path), None) is None
    assert [type(h) for h in logger.handlers] == [logging.StreamHandler]
    assert not list(tmp_path.iterdir()), "a command without its own lock opens no log file"


@budget(load="fs")
def test_file_rotates_at_midnight_keeping_keep_days(logger):
    logs.setup(config.cached(), "process")
    (handler,) = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert (handler.when, handler.backupCount) == ("MIDNIGHT", config.cached()["logging"]["keep_days"])


@budget(load="fs")
def test_setup_twice_keeps_one_pair_of_handlers(logger):
    logs.setup(config.cached(), "export")
    logs.setup(config.cached(), "export")
    assert len(logger.handlers) == 2, "a second setup replaces the first one's handlers"


@budget(load="pure")
@pytest.mark.parametrize(("key", "value"), [("level", "LOUD"), ("keep_days", 0)])
def test_bad_values_are_refused(key, value):
    cfg = {"logging": {**config.cached()["logging"], key: value}}
    with pytest.raises(config.ConfigError, match=f"logging.{key}"):
        logs.setup(cfg, None)
