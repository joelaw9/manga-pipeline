"""`junk review` with a fake claude (SPEC "Junk decisions", review; "CLI" exit codes).

What Claude's process gets, when Claude is started at all, and how reviews are counted.
"""

import copy
import json
import os
import shutil
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml

from manga_pipeline import cli, config, db, fleet, intake, preflight, process
from manga_pipeline.clients import kavita as kavita_client
from manga_pipeline.clients import suwayomi as suwayomi_client
from manga_pipeline.junk import groups, ocr, review, review_run
from manga_pipeline.metadata import sources

from tests.builders import config_file, problem_text, run_part
from tests.test_junk_cut import oracle, real_head, sheets_of
from tests.test_junk_stage import KEYWORD_TEXT, Site
from tests.timing import budget
from tests.world import World, cbz

FAKE = Path(__file__).with_name("fake_claude.py")


def fake_claude(folder, cfg, actions, exit_code=0, reply=None):
    """Write a claude executable in `folder` running fake_claude.py with this plan; return its path."""
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
    monkeypatch.setattr(suwayomi_client, "from_config", lambda _cfg: w.suwayomi)
    monkeypatch.setattr(kavita_client, "from_config", lambda _cfg: w.kavita)
    monkeypatch.setattr(sources, "new_sources", lambda: w.sources)
    w.kavita_series("Alpha")
    manga = w.manga("Alpha")
    w.download(manga, "Chapter 2", data=cbz(credit=True))
    run_part(w.cfg, intake)
    run_part(w.cfg, process)
    return w


def review_cmd(tmp_path, cfg: config.Config, exe):
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
    assert log[0][1] == 0, "Claude's junk list runs on the secret-free config"
    assert len(log[0][2]) == 1, "Claude's junk list runs on the secret-free config"
    assert log[1][1] == 0, "Claude's junk decide records its verdict"
    with closing(db.connect(cfg)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT decider, verdict FROM junk_decision")] == [("claude", "keep")]
    assert not (config.abs_path(cfg["paths"]["scratch"]) / "junk-review").exists(), "the config copy is deleted"


@budget(load="pure")
def test_claudes_environment_needs_the_token():
    """A run without junk.claude_token (preflight refuses one) fails before Claude starts: it could not log in."""
    with pytest.raises(review_run.ReviewError, match="claude_token"):
        review_run.child_env(None, "r1", {})
    assert review_run.child_env("t", "r1", {})["CLAUDE_CODE_OAUTH_TOKEN"] == "t", "the token is passed on"  # noqa: S105  # a fake test token


@budget(load="pure")
def test_the_command_line_needs_an_executable_claude(tmp_path):
    """A junk.claude_path that is no executable (preflight refuses one) fails before Claude starts."""
    cfg: config.Config = {**config.cached(), "junk": {**config.cached()["junk"], "claude_path": str(tmp_path / "none")}}
    with pytest.raises(review_run.ReviewError, match="no executable"):
        review_run.command(cfg, "junk")


@budget(load="subprocess", seconds=20)
def test_the_child_config_holds_no_secret_and_absolute_paths(world):
    child = review_run.child_config(world.cfg)
    assert child["kavita"]["api_key"] is None, "secrets are null"
    assert child["junk"]["claude_token"] is None, "secrets are null"
    assert child["database"]["password"] is None, "the database password is a secret too"
    assert Path(child["logging"]["dir"]).is_absolute()
    assert Path(child["paths"]["candidates"]).is_absolute()


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
    assert meta["junk-review.last_result"].startswith("error")
    assert "peak" in meta["junk-review.last_result"], "an error result names the usage reached too"
    assert "junk-review.last_success" not in meta


@budget(load="subprocess", seconds=20)
def test_a_review_with_a_refused_call_is_an_error_and_still_counts(world, tmp_path):
    """A refused call (a chained command) fails the review; it still counts, so review_limit stops a loop."""
    refused = {"tool_name": "Bash", "tool_input": {"command": 'C="junk decide"; $C 1 junk; $C 2 keep'}}
    reply = {"is_error": False, "num_turns": 3, "permission_denials": [refused]}
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"], reply=reply)
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 1, "a refused call fails the review"
    assert [(r["reviews"], r["review_open"]) for r in candidate_rows(cfg)] == [(1, 0)], "counted, as every review"
    with closing(db.connect(cfg)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'junk-review.%'").fetchall())
    assert meta["junk-review.last_result"].startswith("error: claude was refused 1 tool call(s)"), meta
    assert "junk-review.last_success" not in meta


@budget(load="pure")
def test_the_prompt_asks_for_1_whole_command_per_call():
    text = review_run.PROMPT.read_text(encoding="utf-8")
    assert "1 command per Bash call" in text, "a chained or variable-built command is refused"
    assert "no `;`, `&&`, `||` or `|`" in text, text


@budget(load="subprocess", seconds=20)
def test_a_review_removes_every_sheets_folder(world, tmp_path):
    stale = config.abs_path(world.cfg["paths"]["candidates"]) / f"{groups.FOLDER_PREFIX}old"
    stale.mkdir(parents=True)
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"])
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 0
    left = list(config.abs_path(cfg["paths"]["candidates"]).glob(f"{groups.FOLDER_PREFIX}*"))
    assert left == [], "a killed review's sheets go"


@budget(load="subprocess", seconds=20)
def test_a_run_cut_short_records_the_contexts_it_reached(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list", "call:500", "call:700", "crash"], exit_code=3)
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 1
    with closing(db.connect(cfg)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'junk-review.%'").fetchall())
    assert meta["junk-review.last_result"] == "error: claude exited 3; start 500, peak 700", (
        "a run with no result still records the first call's context and the largest"
    )


@budget(load="subprocess", seconds=20)
def test_the_review_result_records_turns_seconds_tokens_start_and_peak(world, tmp_path):
    reply = {
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 4500,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 900, "output_tokens": 5},
        "modelUsage": {"claude": {"contextWindow": 1000000}},
    }
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list", "call:500", "call:700", "call:650"], reply=reply)
    code, cfg = review_cmd(tmp_path, world.cfg, exe)
    assert code == 0
    with closing(db.connect(cfg)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'junk-review.%'").fetchall())
    assert meta["junk-review.last_result"] == "ok: 3 turn(s), 4 s, 915 tokens, start 500, peak 700 of 1000000", (
        "the result names turns, seconds, tokens, the first call's context and the largest (each message once)"
    )


@budget(load="subprocess", seconds=20)
def test_a_review_that_cannot_start_claude_records_why(world, tmp_path):
    """No claude executable (preflight refuses that run): the review fails and records the reason, not "not run"."""
    cfg = copy.deepcopy(world.cfg)
    cfg["junk"]["claude_path"] = str(tmp_path / "none")
    with closing(db.connect(cfg)) as conn, pytest.raises(review_run.ReviewError, match="no executable"):
        review_run.run(cfg, conn)
    with closing(db.connect(cfg)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key LIKE 'junk-review.%'").fetchall())
    assert "is no executable" in meta["junk-review.last_result"], "the reason Claude did not start is recorded"
    assert "junk-review.last_success" not in meta


@budget(load="subprocess", seconds=20)
def test_claude_may_read_only_the_candidates_folder_and_run_4_junk_commands(world, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, [])
    _, cfg = review_cmd(tmp_path, world.cfg, exe)
    (argv,) = dumps(tmp_path / "claude", "argv")
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--add-dir")]
    candidates = review_run.rule_path(config.abs_path(cfg["paths"]["candidates"]))
    assert candidates.startswith("//"), "Read: the candidates folder only"
    assert allowed[0] == f"Read({candidates}/**)", "Read: the candidates folder only"
    assert [a.split(" junk ")[1] for a in allowed[1:]] == ["list)", "show *)", "decide *)", "cut *)"], (
        "the 4 junk commands; list exactly, so never list --all"
    )
    assert argv[argv.index("--tools") + 1] == "Read,Bash"
    assert "--permission-prompts" in argv


@budget(load="pure")
def test_review_checks_need_claude_and_its_token(tmp_path):
    cfg: config.Config = {
        **config.cached(),
        "junk": {**config.cached()["junk"], "claude_path": str(tmp_path / "none"), "claude_token": None},
    }
    problem = preflight.claude_settings(cfg)
    assert "claude_path" in problem_text(problem), "both are needed to start Claude"
    assert "claude_token" in problem_text(problem), "both are needed to start Claude"
    blank: config.Config = {**cfg, "junk": {**cfg["junk"], "claude_token": "  "}}
    assert "claude_token" in problem_text(preflight.claude_settings(blank)), "a blank token is no token"
    assert preflight.CLAUDE_SETTINGS in cli.COMMANDS["junk review"].checks


def _review_hiding_the_token(world: World, tmp_path, capsys, monkeypatch, reply):
    """Run a review whose Claude replies `reply`; return its exit code.

    Asserts the token is in neither the log nor the output.
    """
    monkeypatch.setenv("MANGA_PIPELINE_LOGGING_LEVEL", "DEBUG")
    token = "tok-SECRET-4711"  # noqa: S105  # a fake test token
    cfg: config.Config = {**world.cfg, "junk": {**world.cfg["junk"], "claude_token": token}}
    exe = fake_claude(tmp_path / "claude", cfg, ["list", "decide:keep"], reply=reply)
    code, cfg = review_cmd(tmp_path, cfg, exe)
    log_text = (config.abs_path(cfg["logging"]["dir"]) / "junk-review.log").read_bytes().decode("utf-8")
    assert "junk review" in log_text, "the token is never logged"
    assert token not in log_text, "the token is never logged"
    captured = capsys.readouterr()
    assert token not in captured.err, "nor in an error message"
    assert token not in captured.out, "nor in an error message"
    return code


@budget(load="subprocess", seconds=20)
def test_the_token_is_never_in_the_log_of_a_review(world, tmp_path, capsys, monkeypatch):
    assert _review_hiding_the_token(world, tmp_path, capsys, monkeypatch, None) == 0, "the review succeeded"


@budget(load="subprocess", seconds=20)
def test_the_token_is_never_in_an_error(world, tmp_path, capsys, monkeypatch):
    reply = {"is_error": True, "subtype": "error_auth"}
    assert _review_hiding_the_token(world, tmp_path, capsys, monkeypatch, reply) == 1, "the review hit the error"


@budget(load="subprocess", seconds=20)
def test_the_review_child_sees_the_reviews_set_values(world: World, tmp_path):
    exe = fake_claude(tmp_path / "claude", world.cfg, ["list"])
    cfg: config.Config = {**world.cfg, "junk": {**world.cfg["junk"], "claude_path": str(exe)}}
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
    ("path", "want"),
    [(r"F:\data\candidates", "//f/data/candidates"), ("/srv/manga/candidates", "//srv/manga/candidates")],
)
def test_an_absolute_rule_path_starts_with_two_slashes(path, want):
    p = PureWindowsPath(path) if ":" in path else PurePosixPath(path)
    assert review_run.rule_path(p) == want, "Claude Code reads //path as absolute; F: becomes //f"


@budget(load="pure")
def test_the_junk_command_prefix_is_posix_with_forward_slashes():
    """Claude's Bash tool is Git Bash on Windows: backslashes in a path are lost there (7a probe)."""
    got = review_run.join_command(
        [r"C:\Python313\python.exe", "-m", "manga_pipeline.cli", "--config", r"F:\s\c 1.yaml"]
    )
    assert got == "C:/Python313/python.exe -m manga_pipeline.cli --config 'F:/s/c 1.yaml'", got


@budget(load="pure")
def test_the_prompt_is_version_8_and_names_the_3_cut_forms():
    text = review_run.PROMPT.read_text(encoding="utf-8")
    assert "prompt version 8" in text, "the version the cut text came in"
    for form in ("{prefix} cut --spans ID", "{prefix} cut CHAPTER:START:END[:keep]", "--record DIGEST"):
        assert form in text, f"the prompt names the cut form {form!r}"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "text", lambda _im, _device: KEYWORD_TEXT)
    return Site(tmp_path, monkeypatch)


def cut_rows(cfg):
    with closing(db.connect(cfg)) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT decider, written_by FROM junk_decision WHERE kind = 'cut' AND retired_by IS NULL ORDER BY id"
            )
        ]


@budget(load="subprocess", seconds=30)
def test_a_review_draws_proposes_and_records_a_cut_then_keeps_the_candidate(site, tmp_path):
    """The real-head notice block: spans, the oracle's cut proposed and recorded with its digest, then keep."""
    chapter = real_head(site)
    ((cid,),) = site.rows("SELECT id FROM junk_candidate")
    plan = ["list", f"spans:{cid}", "cut:" + " ".join(oracle(chapter)), "decide:keep"]
    exe = fake_claude(tmp_path / "claude", site.cfg, plan)
    code, cfg = review_cmd(tmp_path, site.cfg, exe)
    (log,) = dumps(tmp_path / "claude", "log")
    assert code == 0, f"the review ends ok, no call refused: {log}"
    assert [(e[0], e[1]) for e in log] == [("list", 0), ("spans", 0), ("cut", 0), ("record", 0), ("decide", 0)], log
    (act,) = {a for _, a in cut_rows(cfg)}
    assert cut_rows(cfg) == [("claude", act)], "the review's cut is recorded, by claude"
    assert site.rows("SELECT verdict, decider FROM junk_decision WHERE source = %s", f"candidate {cid}") == [
        ("keep", "claude")
    ], "then the candidate is kept"
    (env,) = dumps(tmp_path / "claude", "env")
    folder = groups.folder(cfg, f"{env[review.REVIEW_ID_VARIABLE]}-cut")
    sheets = sheets_of(log[1][2]) + sheets_of(log[2][2])
    assert sheets, log
    assert all(p.parent == folder for p in sheets), "the review's sheets go to review-<id>-cut/"
    assert not folder.exists(), "and are removed when the review ends"


@budget(load="subprocess", seconds=45)
def test_a_review_records_2_cuts_in_turn(site, tmp_path):
    first = real_head(site, 2, "Long")
    second = real_head(site, 2, "Other")
    plan = ["list", "cut:" + " ".join(oracle(first)), "cut:" + " ".join(oracle(second)), "decide:keep"]
    exe = fake_claude(tmp_path / "claude", site.cfg, plan)
    code, cfg = review_cmd(tmp_path, site.cfg, exe)
    (log,) = dumps(tmp_path / "claude", "log")
    assert code == 0, log
    rows = cut_rows(cfg)
    assert [d for d, _ in rows] == ["claude", "claude"], "both cuts active"
    assert len({a for _, a in rows}) == 2, "each in its own act"


@budget(load="subprocess", seconds=30)
def test_a_refused_cut_is_no_refused_tool_call_and_the_review_ends_ok(site, tmp_path):
    """A cut naming a chapter that does not exist is refused by junk cut (exit 2), not by the permission rules."""
    chapter = real_head(site)
    plan = ["list", f"cut:{chapter + 1000}:0:100", "decide:unsure"]
    exe = fake_claude(tmp_path / "claude", site.cfg, plan)
    code, cfg = review_cmd(tmp_path, site.cfg, exe)
    (log,) = dumps(tmp_path / "claude", "log")
    assert [(e[0], e[1]) for e in log] == [("list", 0), ("cut", 2), ("decide", 0)], log
    assert code == 0, f"the command's refusal is no refused tool call: the review ends ok: {log}"
    assert cut_rows(cfg) == [], "nothing is recorded"
