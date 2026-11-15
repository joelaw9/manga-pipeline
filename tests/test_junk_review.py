"""`junk review` with a fake claude (SPEC "Junk decisions", review; "CLI" exit codes): what Claude's process gets,
when Claude is started at all, and how reviews are counted."""

import json
import os
import shutil
import sys
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from manga_pipeline import cli, config, db, fleet, intake, process
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.junk import review, review_run
from manga_pipeline.metadata import check

from tests.builders import config_file, run_part
from tests.timing import budget
from tests.world import World, cbz

FAKE = Path(__file__).with_name("fake_claude.py")


def fake_claude(folder, cfg, actions, exit_code=0, reply=None):
    """A claude executable in `folder` running fake_claude.py with this plan; returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(FAKE, folder / "fake_claude.py")
    plan = {
        "dump": str(folder / "dump"),
        "scratch": str(config.abs_path(cfg["paths"]["scratch"])),
        "actions": actions,
        "exit": exit_code,
        "reply": {"is_error": False, "num_turns": 3, "permission_denials": []} if reply is None else reply,
    }
    (folder / "plan.json").write_bytes(json.dumps(plan).encode())
    if os.name == "nt":
        exe = folder / "claude.cmd"
        exe.write_bytes(f'@"{sys.executable}" "{folder / "fake_claude.py"}" %*\r\n'.encode())
    else:
        exe = folder / "claude"
        exe.write_bytes(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "fake_claude.py"}" "$@"\n'.encode())
        exe.chmod(0o755)
    return exe


def dumps(folder, what):
    return [json.loads(p.read_bytes()) for p in sorted((folder / "dump").glob(f"{what}-*.json"))]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(suwayomi_client, "from_config", lambda cfg: w.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda cfg: w.kavita)
    monkeypatch.setattr(check, "new_sources", lambda: w.sources)
    w.kavita_series("Alpha")
    manga = w.manga("Alpha")
    w.download(manga, "Chapter 2", data=cbz(credit=True))
    run_part(w.cfg, intake)
    run_part(w.cfg, process)
    return w


def review_cmd(tmp_path, cfg, exe, env=None):
    cfg = {**cfg, "junk": {**cfg["junk"], "claude_path": str(exe)}}
    path = config_file(tmp_path, cfg)
    return cli.main(["--config", str(path), "junk", "review"]), cfg


def candidate_rows(cfg):
    with closing(db.connect(cfg)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM junk_candidate")]


@budget(load="subprocess", seconds=20)
def test_claude_gets_no_pipeline_variable_and_no_api_key_and_its_commands_work(world, tmp_path, monkeypatch):
    monkeypatch.setenv("MANGA_PIPELINE_LOGGING_LEVEL", "INFO")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list", "decide:keep"])
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 0
    (env,) = dumps(tmp_path / "claude", "env")
    assert not [k for k in env if k.upper().startswith("MANGA_PIPELINE_")], "no MANGA_PIPELINE_ variable reaches Claude"
    assert "ANTHROPIC_API_KEY" not in env, "no API key reaches Claude"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == world.cfg["junk"]["claude_token"], "the token from the config"
    (log,) = dumps(tmp_path / "claude", "log")
    assert log[0][1] == 0 and len(log[0][2]) == 1, "Claude's junk list runs on the secret-free config"
    assert log[1][1] == 0, "Claude's junk decide records its verdict"
    with closing(db.connect(cfg)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT decider, verdict FROM junk_decision")] == [("claude", "keep")]
    assert not (config.abs_path(cfg["paths"]["scratch"]) / "junk-review").exists(), "the config copy is deleted"


@budget(load="subprocess", seconds=20)
def test_the_child_config_holds_no_secret_and_absolute_paths(world):
    child = review_run.child_config(world.cfg)
    assert child["kavita"]["api_key"] is None and child["junk"]["claude_token"] is None, "secrets are null"
    assert child["database"]["password"] is None, "the database password is a secret too"
    assert Path(child["logging"]["dir"]).is_absolute() and Path(child["paths"]["candidates"]).is_absolute()


@budget(load="subprocess", seconds=20)
def test_nothing_listable_starts_no_claude(world, tmp_path):
    with closing(db.connect(world.cfg)) as conn, db.transaction(conn):
        conn.execute("UPDATE junk_candidate SET verdict = 'unsure'")
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"])
    assert review_cmd(tmp_path, world.cfg, exe)[0] == 0
    assert dumps(tmp_path / "claude", "argv") == [], "only unsure (or stuck) candidates: Claude is not started"


@budget(load="subprocess", seconds=20)
def test_a_crash_before_list_counts_nothing_and_exits_1(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["crash"], exit_code=3, reply=None)
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 1, "a failed Claude run exits 1"
    assert [r["reviews"] for r in candidate_rows(cfg)] == [0], "a review that never listed the candidate counts nothing"


@budget(load="subprocess", seconds=20)
def test_2_lists_in_1_review_count_once_and_the_limit_stops_listing(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list", "list"])
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 0
    assert [(r["reviews"], r["review_open"]) for r in candidate_rows(cfg)] == [(1, 0)], "counted once, when it ended"
    review_cmd(tmp_path, world.cfg, exe)
    assert [r["reviews"] for r in candidate_rows(cfg)] == [2], "a second review counts again"
    assert review_cmd(tmp_path, world.cfg, exe)[0] == 0
    assert len(dumps(tmp_path / "claude", "argv")) == 2, "past the review limit, Claude is not started again"
    with closing(db.connect(cfg)) as conn:
        assert review.listable(conn, cfg) == [], "a stuck candidate is never listed"


@budget(load="subprocess", seconds=20)
def test_an_error_result_exits_1_and_records_no_success(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"], reply={"is_error": True, "subtype": "error_auth"})
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 1
    with closing(db.connect(cfg)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'junk-review.%'").fetchall())
    assert meta["junk-review.last_result"].startswith("error") and "junk-review.last_success" not in meta


@budget(load="subprocess", seconds=20)
def test_claude_may_read_only_the_candidates_folder_and_run_3_junk_commands(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, [])
    _, cfg = review_cmd(tmp_path, world.cfg, exe)
    (argv,) = dumps(tmp_path / "claude", "argv")
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--add-dir")]
    candidates = review_run.rule_path(config.abs_path(cfg["paths"]["candidates"]))
    assert candidates.startswith("//") and allowed[0] == f"Read({candidates}/**)", "Read: the candidates folder only"
    assert [a.split(" junk ")[1] for a in allowed[1:]] == ["list)", "show *)", "decide *)"], (
        "the 3 junk commands; list exactly, so never list --all"
    )
    assert argv[argv.index("--tools") + 1] == "Read,Bash" and "--permission-prompts" in argv


@budget(load="pure")
def test_review_checks_need_claude_and_its_token(tmp_path):
    from manga_pipeline import preflight

    cfg = {
        **config.cached(),
        "junk": {**config.cached()["junk"], "claude_path": str(tmp_path / "none"), "claude_token": None},
    }
    problem = preflight.claude_settings(cfg)
    assert "claude_path" in problem and "claude_token" in problem, "both are needed to start Claude"
    blank = {**cfg, "junk": {**cfg["junk"], "claude_token": "  "}}
    assert "claude_token" in preflight.claude_settings(blank), "a blank token is no token"
    assert preflight.CLAUDE_SETTINGS in cli.COMMANDS["junk review"].checks


@budget(load="subprocess", seconds=20)
@pytest.mark.parametrize(
    "reply, want", [(None, 0), ({"is_error": True, "subtype": "error_auth"}, 1)], ids=["success", "error"]
)
def test_the_token_is_never_in_the_log_or_an_error(world, tmp_path, capsys, monkeypatch, reply, want):
    monkeypatch.setenv("MANGA_PIPELINE_LOGGING_LEVEL", "DEBUG")
    token = "tok-SECRET-4711"
    cfg = {**world.cfg, "junk": {**world.cfg["junk"], "claude_token": token}}
    exe = fake_claude(tmp_path / "claude", cfg, ["list", "decide:keep"], reply=reply)
    code, cfg = review_cmd(tmp_path, cfg, exe)
    assert code == want
    log_text = (config.abs_path(cfg["logging"]["dir"]) / "junk-review.log").read_bytes().decode("utf-8")
    assert "junk review" in log_text and token not in log_text, "the token is never logged"
    captured = capsys.readouterr()
    assert token not in captured.err and token not in captured.out, "nor in an error message"


@budget(load="subprocess", seconds=20)
def test_the_review_child_sees_the_reviews_set_values(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"])
    cfg = {**world.cfg, "junk": {**world.cfg["junk"], "claude_path": str(exe)}}
    path = config_file(tmp_path, cfg)
    with closing(db.connect(cfg)) as conn, db.transaction(conn):  # review_batch is shared: the fleet's version has 7
        fleet.store(conn, {**cfg, "junk": {**cfg["junk"], "review_batch": 7}})
    assert cli.main(["--config", str(path), "--set", "junk.review_batch=7", "junk", "review"]) == 0
    (child,) = [yaml.safe_load(p.read_bytes()) for p in sorted((tmp_path / "claude" / "dump").glob("config-*.yaml"))]
    assert child["junk"]["review_batch"] == 7, "Claude's junk commands run with the review's --set values"
    for section, key in config.secret_keys():
        assert child[section][key] is None, f"the config file Claude can read holds no secret: {section}.{key}"


@budget(load="pure")
@pytest.mark.parametrize(
    "path, want",
    [(r"F:\data\candidates", "//f/data/candidates"), ("/srv/manga/candidates", "//srv/manga/candidates")],
)
def test_an_absolute_rule_path_starts_with_two_slashes(path, want):
    from pathlib import PurePosixPath, PureWindowsPath

    p = PureWindowsPath(path) if ":" in path else PurePosixPath(path)
    assert review_run.rule_path(p) == want, "Claude Code reads //path as absolute; F: becomes //f"


@budget(load="pure")
def test_the_junk_command_prefix_is_posix_with_forward_slashes():
    """Claude's Bash tool is Git Bash on Windows: backslashes in a path are lost there (7a probe)."""
    got = review_run.join_command(
        [r"C:\Python313\python.exe", "-m", "manga_pipeline.cli", "--config", r"F:\s\c 1.yaml"]
    )
    assert got == "C:/Python313/python.exe -m manga_pipeline.cli --config 'F:/s/c 1.yaml'", got
