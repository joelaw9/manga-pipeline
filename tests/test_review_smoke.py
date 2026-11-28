"""scripts/review_smoke.py (plan phase-10-8, D3)."""

import json
import os
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from manga_pipeline import config, db
from manga_pipeline.junk import candidates
from scripts import review_smoke

from tests import pg
from tests.builders import adopted
from tests.test_w5_scripts import AT, candidate
from tests.timing import budget

OK = "ok: 8 turn(s), 17 s, 84742 tokens, start 17919, peak 23734 of 1000000, reads 4, shown 2"  # a real line


def unit(cfg, conn, n, text, verdict):
    """Add a candidate showing text, decided verdict (None: undecided); return its id."""
    cid = candidate(conn, review_smoke.SERIES, "block", n)
    folder = candidates.folder(cfg, f"f{n}")
    folder.mkdir(parents=True)
    info = {"series": review_smoke.SERIES, "kind": "block", "reason": "r", "text": text, "chapters": 3}
    (folder / "info.json").write_bytes(json.dumps(info).encode())
    if verdict is not None:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO junk_decision (series_key, kind, key, verdict, decider, decided_at)
                SELECT series_key, kind, key, %s, 'claude', %s FROM junk_candidate WHERE id = %s""",
                (verdict, AT, cid),
            )
    return cid


@pytest.mark.parametrize(
    ("result", "credit", "localization", "outcome"),
    [
        (OK, "junk", "keep", (True, False)),
        (OK, "keep", "keep", (False, True)),  # a verdict miss: run again
        (OK, "junk", None, (False, True)),
        (OK.replace("reads 4", "reads 3"), "junk", "keep", (False, False)),  # read too few images
        (OK.replace("shown 2", "shown 1"), "junk", "keep", (False, False)),  # show not run on each
        (OK.replace("ok: 8 turn(s),", "error: claude exited 1;"), "junk", "keep", (False, False)),
    ],
)
@budget(load="db", seconds=10)
def test_the_smoke_passes_on_an_ok_review_that_read_and_showed_both_and_judged_each_right(
    tmp_path, result, credit, localization, outcome
):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        unit(cfg, conn, 1, " ".join(review_smoke.CREDIT), credit)
        unit(cfg, conn, 2, " ".join(review_smoke.LOCALIZATION), localization)
        with db.transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('junk-review.last_result', %s)", (result,))
    lines, passed, verdict_miss = review_smoke.judge(cfg)
    assert (passed, verdict_miss) == outcome, lines
    assert f"review: {result}" in lines, "the result line is printed"


@budget()
def test_a_verdict_miss_runs_it_once_more_and_a_second_miss_fails(monkeypatch):
    answers = iter([(["first"], False, True), (["second"], False, True)])
    monkeypatch.setattr(review_smoke, "attempt", lambda _machine, _run: next(answers))
    assert review_smoke.smoke(config.load(), print) == (["first", "a verdict missed: run again", "second"], False)
    answers = iter([(["first"], False, False)])
    assert review_smoke.smoke(config.load(), print) == (["first"], False), "any other failure is not run again"


@budget(load="db", seconds=10)
def test_an_unexpected_candidate_fails_the_smoke(tmp_path):
    cfg = adopted(tmp_path)
    with closing(db.connect(cfg)) as conn:
        unit(cfg, conn, 1, " ".join(review_smoke.CREDIT), "junk")
        unit(cfg, conn, 2, " ".join(review_smoke.LOCALIZATION), "keep")
        unit(cfg, conn, 3, "a noise panel read as text", None)
        with db.transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('junk-review.last_result', %s)", (OK,))
    lines, passed, verdict_miss = review_smoke.judge(cfg)
    assert (passed, verdict_miss) == (False, False), "the fixture opens exactly its 2 candidates"
    assert "other candidates: undecided" in lines


@budget()
def test_the_scratch_config_holds_the_test_login_and_every_path_under_its_folder(tmp_path):
    folder = tmp_path / "smoke"
    folder.mkdir()
    cfg = config.load(review_smoke.scratch_config(config.load(), folder), env={})
    assert dict(cfg["database"]) == {**review_smoke.LOGIN, "name": review_smoke.DATABASE}, "the test server only"
    paths = {k: str(v) for k, v in cfg["paths"].items() if k != "kavita_path_map"}
    assert paths, "it has paths"
    assert all(Path(v).is_relative_to(folder) for v in paths.values()), paths
    assert Path(cfg["logging"]["dir"]).is_relative_to(folder)


@budget(load="db", seconds=30)
def test_a_failed_run_leaves_no_scratch_database_or_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(review_smoke, "DATABASE", f"mp_test_smoke_{os.getpid()}")  # not a hand run's
    monkeypatch.setattr(review_smoke.fleet, "store", lambda _conn, _cfg: None)  # the test machine's tools are no tools
    seen = []

    def fail(cfg_path, args):
        seen.append(Path(cfg_path).parent)
        msg = f"{args} failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="process"):
        review_smoke.attempt(adopted(tmp_path), fail)
    assert not seen[0].exists(), "the scratch folder is removed"
    with closing(pg.connect(db.MAINTENANCE_DATABASE)) as conn:
        left = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (review_smoke.DATABASE,)).fetchone()
    assert left is None, "the scratch database is dropped"


@budget()
def test_the_commands_get_the_token_alone_of_the_pipelines_variables(monkeypatch, tmp_path):
    monkeypatch.setenv(review_smoke.TOKEN, "from the environment")
    monkeypatch.setenv(f"{config.ENV_PREFIX}_DATABASE_HOST", "the pipeline's server")
    monkeypatch.setenv("SMOKE_OTHER", "kept")
    got = {}
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: got.update(argv=argv, env=kw["env"]))
    review_smoke.command(tmp_path / "config.yaml", ["process"], "the machine's")
    assert [k for k in got["env"] if k.startswith(config.ENV_PREFIX)] == [review_smoke.TOKEN], (
        "no other MANGA_PIPELINE_ variable reaches a command: the scratch config alone says where it works"
    )
    assert got["env"][review_smoke.TOKEN] == "the machine's", "the token is the machine config's"
    assert got["env"]["SMOKE_OTHER"] == "kept", "every other variable is kept: Python and Claude need them"
    assert got["argv"][-3:] == ["--config", str(tmp_path / "config.yaml"), "process"], "the scratch config's command"
