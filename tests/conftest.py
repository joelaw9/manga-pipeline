"""pytest wiring (SPEC "Tests"): the no-network guard, a fresh process config per test and the per-test timing report
(tests/timing.py). Under pytest-xdist each worker tags its tests' reports with their budgets (user_properties) and the
controller, which receives every report, writes the 1 report."""

import socket
from pathlib import Path

import pytest

from manga_pipeline import config

from tests import timing

TIMINGS = Path(__file__).resolve().parent.parent / "state" / "test-timings.json"
_rows, _tags = {}, {}
TAG = "timing_budget"  # user_properties name carrying a test's budget tag from a worker to the controller


class NetworkBlocked(AssertionError):
    pass


def _refuse(*args, **kwargs):
    raise NetworkBlocked("tests are offline: a network connection was attempted")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


@pytest.fixture(autouse=True)
def fresh_config():
    yield
    config.reset()  # a test's init(..., sets) must not reach the next test


def pytest_runtest_logreport(report):
    _rows[report.nodeid] = _rows.get(report.nodeid, 0.0) + report.duration  # setup + call + teardown
    for name, value in report.user_properties:
        if name == TAG:
            _tags[report.nodeid] = value


def pytest_collection_modifyitems(items):
    for item in items:
        item.user_properties.append((TAG, timing.budget_for(getattr(item, "obj", None), getattr(item, "cls", None))))


def pytest_terminal_summary(terminalreporter, config):
    if hasattr(config, "workerinput"):  # an xdist worker: the controller reports
        return
    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    rows = [timing.row(i, s, _tags.get(i)) for i, s in _rows.items()]
    parallel = bool(getattr(config.option, "numprocesses", None))  # budgets hold for a serial run only
    timing.report(rows, terminalreporter, TIMINGS, check_budgets=not parallel)
