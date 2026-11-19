"""Per-test time budgets: tag tests with @budget, run them through this module, get a timing report.

Copy this file into the project's test package (for example tests/timing.py). Python 3.12+ (unittest addDuration).

    python -m tests.timing discover -s tests -t .          # unittest; any `python -m unittest` arguments work
    python -m tests.timing --timings out.json tests.test_x  # custom report path

Over-budget tests are reported, never failed. Budgets are set for a serial run: a parallel run passes
check_budgets=False to report(). pytest wiring: see the skill's references/timing.md.
"""

import argparse
import json
import math
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import override

# Default budget in seconds per load: the costliest resource a test touches. "sleep" has no default.
LOADS = {"pure": 0.1, "fs": 0.5, "db": 1.0, "network": 2.0, "subprocess": 5.0, "pool": 5.0, "sleep": None}
ATTR = "_test_budget"
SLOWEST = 10
NO_TESTS_EXIT = 5  # unittest's exit code when nothing ran


def budget(seconds=None, load="pure"):
    """Tag a test function or class with its load and budget (seconds for setup + test + teardown)."""
    if load not in LOADS:
        msg = f"unknown load {load!r}; one of {sorted(LOADS)}"
        raise ValueError(msg)
    if seconds is None:
        seconds = LOADS[load]
        if seconds is None:
            msg = f"load {load!r} needs explicit seconds"
            raise ValueError(msg)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        msg = f"budget seconds must be a positive number, got {seconds!r}"
        raise ValueError(msg)

    def mark(obj):
        setattr(obj, ATTR, {"seconds": float(seconds), "load": load})
        return obj

    return mark


def budget_for(func, cls=None):
    """Return the budget of a test: its own tag, else its class's tag, else None."""
    return getattr(func, ATTR, None) or (getattr(cls, ATTR, None) if cls is not None else None)


def row(test_id, seconds, tag):
    """Return a test's timing row: its id, seconds, budget tag's load and seconds, and whether it ran over."""
    return {
        "id": test_id,
        "seconds": round(seconds, 4),
        "load": tag["load"] if tag else None,
        "budget": tag["seconds"] if tag else None,
        "over": bool(tag) and seconds > tag["seconds"],
    }


def report(rows, out, path, *, check_budgets=True):
    """Print the slowest tests, over-budget tests and untagged tests; write all rows to path as JSON.

    Budgets are set for a serial run. With check_budgets False (a parallel run, whose times include contention)
    no test is counted, listed or marked as over budget, and the JSON says budgets_checked false.
    """
    if not check_budgets:
        rows = [{**r, "over": False} for r in rows]
    rows = sorted(rows, key=lambda r: r["seconds"], reverse=True)
    total = sum(r["seconds"] for r in rows)
    out.write(f"\nTiming: {len(rows)} tests, {total:.1f}s total\n")
    out.write(f"Slowest {min(SLOWEST, len(rows))}:\n")
    for r in rows[:SLOWEST]:
        out.write(f"  {r['seconds']:8.3f}s  {_tag(r):<22}  {r['id']}\n")
    over = [r for r in rows if r["over"]]
    out.write(f"Over budget: {len(over)}\n" if check_budgets else "Over budget: not checked (parallel run)\n")
    for r in over:
        out.write(f"  {r['seconds']:8.3f}s > {r['budget']:.3f}s  {r['load']:<10}  {r['id']}\n")
    untagged = [r for r in rows if r["budget"] is None]
    out.write(f"No budget: {len(untagged)}\n")
    for r in untagged[:SLOWEST]:
        out.write(f"  {r['seconds']:8.3f}s  {r['id']}\n")
    if len(untagged) > SLOWEST:
        out.write(f"  ... {len(untagged) - SLOWEST} more in {path}\n")
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(
            {
                "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "total_seconds": round(total, 3),
                "budgets_checked": check_budgets,
                "tests": rows,
            },
            f,
            indent=1,
        )
    out.write(f"Timings written to {path}\n")


def _tag(r):
    return f"{r['load']} <= {r['budget']:.3f}s" if r["budget"] is not None else "no budget"


class TimedResult(unittest.TextTestResult):
    """Collects one row per test; elapsed covers setUp, the test, tearDown and cleanups (not setUpClass)."""

    def __init__(self, *args, **kwargs):
        """Build the TextTestResult with no timing rows yet."""
        super().__init__(*args, **kwargs)
        self.timing_rows = []

    @override
    def addDuration(self, test, elapsed):
        super().addDuration(test, elapsed)
        if isinstance(test, unittest.TestCase):
            # unittest has no public accessor for the running test's method name
            func = getattr(test, test._testMethodName, None)  # noqa: SLF001
            self.timing_rows.append(row(test.id(), elapsed, budget_for(func, type(test))))


class TimedRunner(unittest.TextTestRunner):
    """Passed to unittest.main as a class, so -v / -f / -b / --durations / warnings reach it like the default runner."""

    resultclass = TimedResult


def main(argv=None):
    """Run unittest with TimedRunner and report its timings (rows written to --timings); return the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timings", default="test-timings.json")
    opts, rest = parser.parse_known_args(argv)
    prog = unittest.main(module=None, argv=["timing", *rest], testRunner=TimedRunner, exit=False)
    result = prog.result
    if not isinstance(result, TimedResult):
        msg = f"the runner returned {type(result).__name__}, not a TimedResult"
        raise TypeError(msg)
    report(result.timing_rows, sys.stderr, opts.timings)
    if not result.wasSuccessful():
        return 1
    return NO_TESTS_EXIT if result.testsRun == 0 and not result.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
