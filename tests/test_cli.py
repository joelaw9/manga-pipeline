"""The entry point runs each command in its row of the command table: config (with env and --set), its locks, its log
file, its preflight checks, its handler; a bad config is a usage error, a held lock exits 3, a hard preflight problem
exits 4 (SPEC "CLI", "Run lock", "Logging", "Preflight")."""

import dataclasses
import errno
import logging
import subprocess
import sys
from contextlib import closing

import pytest
import yaml

from manga_pipeline import cli, config, db, discovery, lock, preflight

from tests.timing import budget

log = logging.getLogger("manga_pipeline.cli")
ARGV = {
    "intake": ["intake"],
    "process": ["process"],
    "export": ["export"],
    "status": ["status"],
    "retry": ["retry", "1"],
    "chapter use": ["chapter", "use", "1", "2"],
    "chapter drop": ["chapter", "drop", "1"],
    "smoke": ["smoke"],
    "series link": ["series", "link", "1", "2"],
    "state reset": ["state", "reset", "--yes"],
    "state adopt": ["state", "adopt", "--yes"],
}
PARTS = ("intake", "process", "export", "junk-review")
LOCKED = ["runtime version", "free disk space", "database location", "folder layout", "share folders"]
PART_CHECKS = [*LOCKED, "workflow settings", "share ownership"]
# SPEC "Run lock", "Logging", "Preflight": (locks held, log file, checks run) per command.
SPEC = {
    "intake": ({"intake"}, "intake.log", [*PART_CHECKS, "service settings"]),
    "process": (
        {"process"},
        "process.log",
        [*PART_CHECKS, "OCR device", "junk word lists", "image tools", "scorer", "scratch space"],
    ),
    "export": ({"export"}, "export.log", [*PART_CHECKS, "service settings"]),
    "status": (set(), None, []),
    "retry": (set(), None, []),
    "chapter use": (set(), None, []),
    "chapter drop": (set(), None, []),
    "smoke": (set(), None, ["workflow settings", "service settings"]),
    "series link": (set(), None, ["workflow settings", "service settings"]),
    "state reset": (set(PARTS), None, [*LOCKED, "share ownership"]),
    "state adopt": (set(PARTS), None, [*LOCKED, "staging folder"]),
}


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    for handler in logging.getLogger("manga_pipeline").handlers[:]:
        logging.getLogger("manga_pipeline").removeHandler(handler)
        handler.close()


@pytest.fixture
def site(tmp_path):
    """--set items giving a run its own folders under tmp_path, with the staging folder present (the share is
    mounted) and no owner file yet."""
    (tmp_path / "share" / "staging").mkdir(parents=True)
    (tmp_path / "share" / "downloads" / "mangas").mkdir(parents=True)
    (tmp_path / "share" / "retention").mkdir()
    (tmp_path / "share" / "library").mkdir()
    (tmp_path / "overrides").mkdir()
    return [
        f"logging.dir={tmp_path / 'logs'}",
        f"lock.dir={tmp_path / 'locks'}",
        f"database.path={tmp_path / 'pipeline.sqlite3'}",
        *(f"paths.{key}={tmp_path / 'share' / key}" for key in preflight.SHARE_FOLDERS),
        f"paths.scratch={tmp_path / 'scratch'}",
        f"paths.naming_overrides={tmp_path / 'overrides'}",
    ]


def run(site, command, *extra):
    return cli.main([*(a for item in site for a in ("--set", item)), *extra, *ARGV[command]])


def site_cfg(site):
    return config.load(sets=site)


def log_files(site):
    folder = config.abs_path(site_cfg(site)["logging"]["dir"])
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@budget(load="fs")
@pytest.mark.parametrize("command", list(ARGV))
def test_each_command_runs_in_its_table_row(site, command, monkeypatch):
    seen = {}

    def probe(cfg, args):
        held = set()
        for name in PARTS:
            try:
                with lock.held(cfg, name):
                    pass
            except lock.LockHeld:
                held.add(name)
        seen["locks"] = held
        log.info("probe ran")

    monkeypatch.setattr(preflight, "run", lambda cfg, checks: seen.setdefault("checks", [c.name for c in checks]))
    monkeypatch.setitem(cli.COMMANDS, command, dataclasses.replace(cli.COMMANDS[command], handler=probe))
    assert run(site, command) == 0
    locks, log_file, checks = SPEC[command]
    assert (seen["locks"], seen["checks"]) == (locks, checks), "the command takes its locks and runs its checks"
    assert log_files(site) == ([log_file] if log_file else []), "only a command with its own lock has a log file"


@budget(load="db")
def test_part_logs_that_it_ran(site):
    assert run(site, "state adopt") == 0
    assert run(site, "intake") == 0
    log_file = config.abs_path(site_cfg(site)["logging"]["dir"]) / "intake.log"
    assert "intake: run ended: ok" in log_file.read_text(encoding="utf-8")


@budget(load="subprocess")
def test_module_run_writes_the_log(site, tmp_path):
    # python -m runs cli.py as __main__, which the in-process tests above never do.
    assert run(site, "state adopt") == 0
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(site_cfg(site)), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "manga_pipeline.cli", "--config", str(path), "export"],
        cwd=config.ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert "export: run ended: ok" in (tmp_path / "logs" / "export.log").read_text(encoding="utf-8")


@budget(load="fs")
@pytest.mark.parametrize("command", ["intake", "state reset", "state adopt"])
def test_held_lock_exits_3_without_opening_a_log_file(site, command, capsys):
    with lock.held(site_cfg(site), "intake"):
        assert run(site, command) == 3, "a run must stop at once while another holds a lock it takes"
    err = capsys.readouterr().err
    assert "another run holds" in err and "intake.lock" in err, "the error names the held lock file"
    assert log_files(site) == [], "a refused run must not open the log file the holder writes"


@budget(load="db")
def test_adopt_lets_a_part_pass_preflight(site, capsys):
    assert run(site, "process") == 4, "a share no database adopted stops every part"
    assert "share ownership" in capsys.readouterr().err
    assert run(site, "state adopt") == 0
    assert run(site, "process") == 0


@budget(load="db")
def test_share_owned_by_another_database_exits_4(site, capsys):
    assert run(site, "state adopt") == 0
    preflight.owner_file(site_cfg(site)).write_text("another-database", encoding="utf-8")
    assert run(site, "export") == 4
    assert "holds database id 'another-database'" in capsys.readouterr().err


@budget(load="db")
def test_adopt_without_the_staging_folder_exits_4_and_creates_it_not(site, tmp_path, capsys):
    (tmp_path / "share" / "staging").rmdir()
    assert run(site, "state adopt") == 4, "an unmounted share is never adopted"
    assert "staging folder: paths.staging" in capsys.readouterr().err
    assert not (tmp_path / "share" / "staging").exists()


@budget(load="pure")
@pytest.mark.parametrize("command", ["state reset", "state adopt"])
def test_state_commands_refuse_without_yes(command, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(ARGV[command][:2])
    assert exit_.value.code == 2 and "--yes" in capsys.readouterr().err


@budget(load="fs")
def test_bad_logging_value_is_a_usage_error(site, capsys):
    with pytest.raises(SystemExit) as exit_:
        run(site, "status", "--set", "logging.level=LOUD")
    assert exit_.value.code == 2 and "logging.level" in capsys.readouterr().err


@budget(load="fs")
def test_set_and_env_reach_the_run(site, monkeypatch):
    monkeypatch.setenv(f"{config.ENV_PREFIX}_LOGGING_KEEP_DAYS", "3")
    assert run(site, "status", "--set", "logging.level=WARNING") == 0
    assert (config.cached()["logging"]["level"], config.cached()["logging"]["keep_days"]) == ("WARNING", 3)


@budget(load="fs")
def test_bad_set_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--set", "logging.keep_days=14days", "status"])
    assert exit_.value.code == 2 and "--set logging.keep_days: must be int" in capsys.readouterr().err


@budget(load="fs")
def test_hard_preflight_failure_exits_4(site, monkeypatch, capsys):
    stub = preflight.Check("stub", preflight.HARD, lambda cfg: "missing")
    monkeypatch.setitem(cli.COMMANDS, "intake", dataclasses.replace(cli.COMMANDS["intake"], checks=(stub,)))
    assert run(site, "intake") == 4
    assert "preflight failed: stub: missing" in capsys.readouterr().err
    text = (config.abs_path(site_cfg(site)["logging"]["dir"]) / "intake.log").read_text(encoding="utf-8")
    assert "stub: missing" in text and "run ended" not in text, "the work must not start after a hard failure"


@budget(load="db")
def test_crash_inside_a_part_exits_1_with_1_traceback_in_its_log(site, monkeypatch):
    def unreadable(ctx):
        raise PermissionError(errno.EACCES, "Permission denied", "downloads")

    assert run(site, "state adopt") == 0
    monkeypatch.setattr(discovery, "scan", unreadable)
    assert run(site, "intake") == 1, "SPEC 'CLI': an unexpected error exits 1"
    text = (config.abs_path(site_cfg(site)["logging"]["dir"]) / "intake.log").read_text(encoding="utf-8")
    assert text.count("Traceback") == 1 and "PermissionError" in text, "logged with its traceback, once"
    with closing(db.connect(site_cfg(site))) as conn:
        result = conn.execute("SELECT value FROM meta WHERE key = 'intake.last_result'").fetchone()[0]
    assert result == "error"


@budget(load="fs")
def test_unexpected_error_of_a_command_without_a_log_file_exits_1_with_its_traceback(site, capsys):
    config.abs_path(site_cfg(site)["database"]["path"]).write_bytes(b"not a database")
    assert run(site, "status") == 1
    err = capsys.readouterr().err
    assert "status: failed" in err and "Traceback" in err and "DatabaseError" in err
    assert log_files(site) == []


@budget(load="pure")
@pytest.mark.parametrize("argv", [["--help"], ["state", "--help"], ["retry", "--help"]])
def test_help_exits_cleanly(argv):
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == 0


@budget(load="pure")
def test_missing_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main([])
    assert exit_.value.code == 2 and "COMMAND" in capsys.readouterr().err


@budget(load="fs")
def test_bad_config_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump({**config.cached(), "logging": 1}), encoding="utf-8")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--config", str(bad), "status"])
    assert exit_.value.code == 2
    assert "logging must be a mapping" in capsys.readouterr().err
