"""The automation's pause (SPEC "Parts", "Run lock"): state pause / resume, and what a --scheduled run does."""

import configparser
import dataclasses
import shutil
import socket
from contextlib import ExitStack, closing
from pathlib import Path

import pytest

from manga_pipeline import cli, db, lock, pause, preflight, status

from tests import pg
from tests.builders import running
from tests.test_cli import adopt, close_logs, log_files, make_site, site_cfg
from tests.timing import budget

UNITS = Path(__file__).parent.parent / "deploy" / "systemd"
AUTOMATED = {"intake", "process", "export", "junk review", "watch"}
ARGV = {"intake": ["intake"], "process": ["process"], "watch": ["watch"]}


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    close_logs()


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


@pytest.fixture(autouse=True)
def checks(monkeypatch):
    """Record each preflight run in place of running it (watch's checks want a Discord webhook)."""
    runs = []
    monkeypatch.setattr(preflight, "run", lambda _cfg, checks: runs.append([c.name for c in checks]))
    return runs


def main(site, *argv):
    return cli.main([*(a for item in site for a in ("--set", item)), *argv])


def ran(monkeypatch, command):
    """Replace command's handler with one that records each call; return the list of calls."""
    calls = []
    monkeypatch.setitem(
        cli.COMMANDS, command, dataclasses.replace(cli.COMMANDS[command], handler=lambda *a: calls.append(a))
    )
    return calls


def pending_step(tmp_path, monkeypatch):
    """Provide the program's schema steps plus a step the database lacks; return its name."""
    folder = tmp_path / "schema"
    shutil.copytree(db.SCHEMA, folder)
    (folder / "9999_pending.sql").write_text("CREATE TABLE pending_probe (x int);", encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA", folder)
    return "9999_pending"


def steps(cfg):
    with closing(db.open_database(cfg, builds=False)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM schema_step")}


@pytest.fixture
def paused(site):
    adopt(site)
    assert main(site, "state", "pause") == 0, "set up: paused, nothing running"
    return site


# --- a scheduled run while paused


@budget(load="db")
@pytest.mark.parametrize("command", list(ARGV))
def test_a_paused_scheduled_run_does_nothing(paused, command, monkeypatch, capsys, checks):
    calls = ran(monkeypatch, command)
    checks.clear()
    capsys.readouterr()
    assert main(paused, "--scheduled", *ARGV[command]) == 0, "SPEC 'Parts': a paused scheduled run exits 0"
    assert "automation paused since" in capsys.readouterr().out, "it says the automation is paused"
    assert calls == [], "it does no work"
    assert checks == [], "it runs no preflight"
    assert log_files(paused) == [], "it opens no log file"


@budget(load="db")
@pytest.mark.parametrize("command", list(ARGV))
def test_a_paused_scheduled_run_applies_no_schema_step(paused, command, tmp_path, monkeypatch):
    pending = pending_step(tmp_path, monkeypatch)
    assert main(paused, "--scheduled", *ARGV[command]) == 0
    assert pending not in steps(site_cfg(paused)), "SPEC 'Parts': it applies no schema step"


@budget(load="db")
@pytest.mark.parametrize("command", ["process", "watch"])
def test_a_manual_run_works_while_paused(paused, command, monkeypatch):
    calls = ran(monkeypatch, command)
    assert main(paused, *ARGV[command]) == 0
    assert len(calls) == 1, "SPEC 'Parts': a command run without --scheduled works during a pause"


@budget(load="db")
def test_after_resume_the_scheduled_run_works(paused, monkeypatch):
    calls = ran(monkeypatch, "process")
    assert main(paused, "state", "resume") == 0
    assert main(paused, "--scheduled", "process") == 0
    assert len(calls) == 1, "resumed: the scheduled run works"


@budget(load="db")
def test_a_paused_run_takes_no_part_lock(paused, monkeypatch):
    calls = ran(monkeypatch, "process")
    with running(site_cfg(paused), "process"):  # a manual process of this machine holds its lock and IDLE
        assert main(paused, "--scheduled", "process") == 0, "it reads the pause before its locks: not exit 3"
    assert calls == []


@budget(load="db")
def test_an_unpaused_scheduled_run_meets_a_held_lock_as_today(site, monkeypatch):
    adopt(site)
    ran(monkeypatch, "process")
    with running(site_cfg(site), "process"):
        assert main(site, "--scheduled", "process") == 3


@budget(load="db")
@pytest.mark.parametrize("command", ["process", "watch"])
def test_a_scheduled_run_holds_automation_from_its_read_to_its_end(site, command, monkeypatch):
    adopt(site)
    cfg = site_cfg(site)
    seen = {}
    with closing(db.open_database(cfg, builds=False)) as other:
        read = pause.paused

        def spy(conn):
            seen["at read"] = pause.waiting(other)
            return read(conn)

        monkeypatch.setattr(pause, "paused", spy)
        monkeypatch.setitem(
            cli.COMMANDS,
            command,
            dataclasses.replace(
                cli.COMMANDS[command], handler=lambda *_a: seen.setdefault("at work", pause.waiting(other))
            ),
        )
        assert main(site, "--scheduled", *ARGV[command]) == 0
        assert seen == {"at read": 1, "at work": 1}, "SPEC 'Run lock': automation is held before the read, to the end"
        seen.clear()
        assert main(site, *ARGV[command]) == 0
        assert seen == {"at work": 0}, "a manual run holds no automation lock and reads no pause"


@budget(load="db")
def test_a_scheduled_watch_with_no_database_exits_4(tmp_path, capsys):
    sets = [f"database.name={pg.current().database(tmp_path.resolve(), built=False)}"]
    assert main(sets, "--scheduled", "watch") == 4, "SPEC 'CLI': as a command with locks"
    assert "error:" in capsys.readouterr().err


@budget(load="db")
def test_with_locks_off_the_pause_still_holds_and_state_pause_is_refused(paused, monkeypatch, capsys):
    calls = ran(monkeypatch, "process")
    off = [*paused, "lock.enabled=false"]
    assert main(off, "--scheduled", "process") == 0
    assert calls == [], "the pause is read with locks off too"
    assert main(off, "state", "pause") == 2, "with no locks state pause cannot tell when runs end"
    assert "lock.enabled is false" in capsys.readouterr().err


@budget(load="pure")
def test_only_the_automated_commands_take_scheduled():
    assert {name for name, c in cli.COMMANDS.items() if c.automated} == AUTOMATED
    with pytest.raises(SystemExit) as e:
        cli.main(["--scheduled", "status"])
    assert e.value.code == 2, "--scheduled on another command is a usage error"


# --- state pause and state resume


@budget(load="db")
def test_state_pause_waits_for_a_scheduled_run_only(site, monkeypatch, capsys):
    adopt(site)
    cfg = site_cfg(site)
    sleeps = []
    with ExitStack() as scheduled, running(cfg, "process"):  # a manual process holds IDLE: not waited for
        conn = scheduled.enter_context(closing(db.open_database(cfg, builds=False)))
        scheduled.enter_context(lock.held(conn, cfg, (), automation=True))

        def sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) < 3, "state pause still waits once the scheduled run has ended"  # never hang
            scheduled.close()  # the scheduled run ends during the 1st wait

        monkeypatch.setattr(cli.time, "sleep", sleep)
        assert main(site, "state", "pause") == 0
    assert sleeps == [cli.PAUSE_POLL], "it waited while the scheduled run held automation, then returned"
    out = capsys.readouterr().out
    assert "waiting for 1 scheduled run(s) to end" in out
    assert "no scheduled run is running" in out


@budget(load="db")
def test_a_second_pause_keeps_the_first(paused):
    cfg = site_cfg(paused)
    with closing(db.open_database(cfg, builds=False)) as conn:
        first = pause.paused(conn)
        assert main(paused, "state", "pause") == 0
        assert pause.paused(conn) == first, "a pause already set keeps its time"
        assert first is not None
        assert first.endswith(f" {socket.gethostname()}"), "the pause names its host"


@budget(load="db")
def test_state_resume_ends_the_pause_and_is_refused_without_one(paused, capsys):
    assert main(paused, "state", "resume") == 0
    assert main(paused, "state", "resume") == 2, "no pause to end"
    assert "not paused" in capsys.readouterr().err


@budget(load="db")
def test_pause_and_resume_apply_no_step_and_work_beside_a_newer_database(site, tmp_path, monkeypatch):
    adopt(site)
    cfg = site_cfg(site)
    pending = pending_step(tmp_path, monkeypatch)
    with closing(db.open_database(cfg, builds=False)) as conn, db.transaction(conn):
        conn.execute("INSERT INTO schema_step (name, applied_at) VALUES ('9999_newer', 'x')")
    assert main(site, "state", "pause") == 0, "a step the program lacks refuses nothing"
    assert main(site, "state", "resume") == 0
    assert pending not in steps(cfg), "neither applies a step"


@budget(load="db")
@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_and_resume_need_a_built_database(tmp_path, action):
    sets = [f"database.name={pg.current().database(tmp_path.resolve(), built=False)}"]
    assert main(sets, "state", action) == 2, "no database built: refused"


@budget(load="db")
def test_status_shows_the_pause(paused):
    with closing(db.open_database(site_cfg(paused), builds=False)) as conn:
        lines = status.report(conn)
    assert lines[0].startswith("Automation paused since"), "SPEC 'CLI': status lists the pause first"


# --- the units


@budget(load="fs")
@pytest.mark.parametrize("command", [*sorted(AUTOMATED - {"junk review"}), "junk-review"])
def test_every_unit_runs_its_command_scheduled(command):
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(UNITS / f"manga-pipeline-{command}.service", encoding="utf-8")
    words = unit["Service"]["ExecStart"].split()
    assert words[1:] == ["-m", "manga_pipeline.cli", "--scheduled", *command.replace("-", " ").split()], (
        "SPEC 'Parts': the timers start each command with --scheduled"
    )
