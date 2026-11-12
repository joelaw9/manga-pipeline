"""The entry point loads config (with env and --set), sets up logging, refuses a bad config with a usage error and
stops while another run holds the run lock (SPEC "CLI")."""

import logging
import subprocess
import sys

import pytest
import yaml

from manga_pipeline import cli, config, lock, preflight

from tests.timing import budget


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


@budget(load="fs")
def test_main_logs_start():
    assert cli.main([]) == 0
    log_file = config.abs_path(config.cached()["logging"]["dir"]) / "application.log"
    assert "started" in log_file.read_text(encoding="utf-8")


@budget(load="subprocess")
def test_module_run_writes_the_log(tmp_path):
    # python -m runs cli.py as __main__, which the in-process tests above never do.
    cfg = {
        **config.cached(),
        "logging": {**config.cached()["logging"], "dir": str(tmp_path / "logs")},
        "lock": {"enabled": True, "path": str(tmp_path / "run.lock")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(path)],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "started" in (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")


@budget(load="fs")
def test_run_lock_held_exits_3(capsys):
    with lock.held(config.cached()):
        assert cli.main([]) == 3, "a second run must stop at once while another holds the lock"
    assert "another run holds" in capsys.readouterr().err


@budget(load="fs")
def test_bad_logging_value_is_a_usage_error(tmp_path, capsys):
    cfg = {
        **config.cached(),
        "logging": {**config.cached()["logging"], "level": "LOUD"},
        "lock": {"enabled": True, "path": str(tmp_path / "run.lock")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--config", str(path)])
    assert exit_.value.code == 2 and "logging.level" in capsys.readouterr().err


@budget(load="fs")
def test_set_and_env_reach_the_run(monkeypatch):
    monkeypatch.setenv(f"{config.ENV_PREFIX}_LOGGING_KEEP_DAYS", "3")
    assert cli.main(["--set", "logging.level=WARNING"]) == 0
    assert (config.cached()["logging"]["level"], config.cached()["logging"]["keep_days"]) == ("WARNING", 3)


@budget(load="fs")
def test_bad_set_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--set", "logging.keep_days=14days"])
    assert exit_.value.code == 2 and "--set logging.keep_days: must be int" in capsys.readouterr().err


@budget(load="fs")
def test_hard_preflight_failure_exits_4(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(preflight, "CHECKS", [preflight.Check("stub", preflight.HARD, lambda cfg: "missing")])
    assert cli.main(["--set", f"logging.dir={tmp_path}"]) == 4
    assert "preflight failed: stub: missing" in capsys.readouterr().err
    text = (tmp_path / "application.log").read_text(encoding="utf-8")
    assert "stub: missing" in text and "started" not in text, "the work must not start after a hard failure"


@budget(load="pure")
def test_help_exits_cleanly():
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    assert exit_.value.code == 0


@budget(load="fs")
def test_bad_config_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "config.yaml"
    bad.write_text(
        'logging: 1\nlock: {enabled: true, path: "run.lock"}\npreflight: {min_free_mb: 0}\n', encoding="utf-8"
    )
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--config", str(bad)])
    assert exit_.value.code == 2
    assert "logging must be a mapping" in capsys.readouterr().err
