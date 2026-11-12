"""preflight.run: every check runs; soft problems warn, hard ones stop the run (SPEC "Preflight")."""

import logging
import shutil

import pytest

from manga_pipeline import config, preflight
from manga_pipeline.preflight import HARD, SOFT, Check

from tests.timing import budget


def ok(cfg):
    return None


def broken(text):
    return lambda cfg: text


def with_min_free(mb, folder=None):
    cfg = {**config.cached(), "preflight": {"min_free_mb": mb}}
    if folder is not None:
        cfg["logging"] = {**cfg["logging"], "dir": str(folder)}
    return cfg


@budget(load="pure")
def test_shipped_checks():
    got = [(c.name, c.level, c.run) for c in preflight.CHECKS]
    want = [("python version", HARD, preflight.python_version), ("free disk space", SOFT, preflight.free_space)]
    assert got == want, "SPEC 'Preflight' lists these checks with these kinds"


@budget(load="pure")
def test_all_passing_returns_no_problems():
    assert preflight.run(config.cached(), [Check("a", HARD, ok), Check("b", SOFT, ok)]) == []


@budget(load="pure")
def test_soft_problem_is_logged_and_the_run_goes_on(caplog):
    with caplog.at_level(logging.WARNING, logger="manga_pipeline"):
        soft = preflight.run(config.cached(), [Check("disk", SOFT, broken("low"))])
    assert soft == ["disk: low"]
    assert "disk: low" in caplog.text


@budget(load="pure")
def test_hard_problems_stop_the_run_after_every_check_ran():
    seen = []
    checks = [
        Check("a", HARD, broken("first")),
        Check("b", SOFT, lambda cfg: seen.append("b")),
        Check("c", HARD, broken("second")),
    ]
    with pytest.raises(preflight.PreflightFailed, match="a: first; c: second"):
        preflight.run(config.cached(), checks)
    assert seen == ["b"], "a hard failure must not skip the later checks"


@budget(load="pure")
def test_unknown_level_is_refused_before_any_check_runs():
    with pytest.raises(ValueError, match="HARD or SOFT: needs-x"):
        preflight.run(config.cached(), [Check("needs-x", "Hard", broken("missing"))])


@budget(load="pure")
def test_python_version_refuses_older_than_3_13():
    assert preflight.python_version(config.cached(), (3, 12, 9)) == "Python 3.13 or later is required, this is 3.12"
    assert preflight.python_version(config.cached(), (3, 13, 0)) is None


@budget(load="fs")
def test_free_space_compares_mib(tmp_path):
    free = shutil.disk_usage(tmp_path).free // preflight.MIB
    # 2% margins: other writers may change the free space meanwhile; MB instead of MiB would be 4.9% off.
    assert "below preflight.min_free_mb" in preflight.free_space(with_min_free(int(free * 1.02) + 1, tmp_path))
    assert preflight.free_space(with_min_free(int(free * 0.98), tmp_path)) is None


@budget(load="fs")
def test_zero_skips_the_check_and_negative_is_a_problem(tmp_path):
    missing = tmp_path / "missing"
    assert preflight.free_space(with_min_free(0, missing)) is None, "0 skips the check without reading the disk"
    assert preflight.free_space(with_min_free(-5, tmp_path)) == "preflight.min_free_mb must be 0 or more, got -5"


@budget(load="fs")
def test_free_space_reports_an_unreadable_folder(tmp_path):
    assert preflight.free_space(with_min_free(1, tmp_path / "missing")).startswith("cannot read free space at")
