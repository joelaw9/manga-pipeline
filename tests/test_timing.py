"""timing.report: the per-test timing report judges budgets in a serial run only (tests/timing.py)."""

import io
import json

from tests import timing
from tests.timing import budget


def run_report(tmp_path, *tests, **kwargs):
    """Report tests given as (id, seconds, budget seconds); return the printed text and the JSON file's content."""
    out = io.StringIO()
    path = tmp_path / "timings.json"
    rows = [timing.row(name, seconds, {"seconds": limit, "load": "fs"}) for name, seconds, limit in tests]
    timing.report(rows, out, path, **kwargs)
    return out.getvalue(), json.loads(path.read_text(encoding="utf-8"))


@budget(load="fs")
def test_a_serial_report_lists_the_over_budget_test(tmp_path):
    printed, _ = run_report(tmp_path, ("slow", 0.9, 0.5), ("quick", 0.1, 0.5))
    assert "Over budget: 1\n" in printed, "a serial run counts the test that ran past its budget"
    assert "0.900s > 0.500s" in printed, "the over-budget line shows the measured time against the budget"


@budget(load="fs")
def test_a_serial_json_marks_the_over_budget_test(tmp_path):
    _, written = run_report(tmp_path, ("slow", 0.9, 0.5), ("quick", 0.1, 0.5))
    assert written["tests"] == [
        {"id": "slow", "seconds": 0.9, "load": "fs", "budget": 0.5, "over": True},
        {"id": "quick", "seconds": 0.1, "load": "fs", "budget": 0.5, "over": False},
    ], "a serial run's JSON marks only the test that ran past its budget as over"


@budget(load="fs")
def test_a_serial_json_says_budgets_were_checked(tmp_path):
    _, written = run_report(tmp_path, ("slow", 0.9, 0.5))
    assert written["budgets_checked"] is True, "a serial run's JSON says its over flags can be trusted"


@budget(load="fs")
def test_a_parallel_report_does_not_judge_budgets(tmp_path):
    printed, _ = run_report(tmp_path, ("slow", 0.9, 0.5), ("quick", 0.1, 0.5), check_budgets=False)
    assert "Over budget: not checked (parallel run)\n" in printed, "a parallel run's times include contention"
    assert "0.900s > 0.500s" not in printed, "no test is listed as over budget in a parallel run"


@budget(load="fs")
def test_a_parallel_report_still_lists_the_slowest_tests(tmp_path):
    printed, _ = run_report(tmp_path, ("slow", 0.9, 0.5), ("quick", 0.1, 0.5), check_budgets=False)
    assert "Slowest 2:\n" in printed, "a parallel run still prints the slowest tests"
    assert "0.900s  fs <= 0.500s" in printed, "the slowest list shows each time beside its budget"


@budget(load="fs")
def test_a_parallel_json_marks_no_test_over(tmp_path):
    _, written = run_report(tmp_path, ("slow", 0.9, 0.5), ("quick", 0.1, 0.5), check_budgets=False)
    assert written["tests"] == [
        {"id": "slow", "seconds": 0.9, "load": "fs", "budget": 0.5, "over": False},
        {"id": "quick", "seconds": 0.1, "load": "fs", "budget": 0.5, "over": False},
    ], "a parallel run's JSON keeps every time and budget and marks no test over"


@budget(load="fs")
def test_a_parallel_json_says_budgets_were_not_checked(tmp_path):
    _, written = run_report(tmp_path, ("slow", 0.9, 0.5), check_budgets=False)
    assert written["budgets_checked"] is False, '"none over" in a parallel run must not read as "all within budget"'
