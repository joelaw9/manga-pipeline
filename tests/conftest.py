"""pytest wiring (SPEC "Tests"): the no-network guard, a fresh process config per test and the per-test timing report
(tests/timing.py). Under pytest-xdist each worker tags its tests' reports with their budgets (user_properties) and the
controller, which receives every report, writes the 1 report."""

import socket
from pathlib import Path

import pytest

from manga_pipeline import config, preflight

from tests import pg, timing
from tests.fakes import convert as fake_convert

TIMINGS = Path(__file__).resolve().parent.parent / "state" / "test-timings.json"
_rows, _tags = {}, {}
TAG = "timing_budget"  # user_properties name carrying a test's budget tag from a worker to the controller


class NetworkBlockedError(AssertionError):
    pass


def _refuse(*_args, **_kwargs):
    msg = "tests are offline: a network connection was attempted"
    raise NetworkBlockedError(msg)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


def pytest_addoption(parser):
    parser.addoption("--vship", default=None, help="path of the real Vship library: runs the gpu tests")


def _runs_tests(config):
    """An xdist worker or a serial run; the xdist controller only hands out tests."""
    return hasattr(config, "workerinput") or not getattr(config.option, "numprocesses", None)


def pytest_configure(config):
    config.addinivalue_line("markers", "gpu: needs the real Vship library and a GPU (run with --vship PATH)")
    if _runs_tests(config):
        pg.process = pg.Process()  # takes its pool databases here, outside every test's timing


def pytest_sessionfinish(session):
    if not hasattr(session.config, "workerinput") and pg.unassigned_built():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        session.config.get_terminal_writer().line(
            f"error: a test built the database tests/test_config.yaml names ({pg.UNASSIGNED}, dropped now): "
            "take the database from site_cfg",
            red=True,
        )


def pytest_unconfigure():
    if pg.process is not None:
        pg.process.close()
        pg.process = None


@pytest.fixture(autouse=True)
def test_databases():
    """Every database the test used is emptied in its teardown (tests/pg.py)."""
    yield
    if pg.process is not None:
        pg.process.end_test()


@pytest.fixture(autouse=True)
def fake_image_tools(request, monkeypatch):
    """The convert stage's tools and scorer are fakes (tests/fakes/convert.py), except in a gpu test, which runs only
    with --vship and gets the real scorer."""
    if request.node.get_closest_marker("gpu"):
        if request.config.getoption("--vship") is None:
            pytest.skip("gpu test: run with --vship PATH")
        return
    fake_convert.install(monkeypatch.setattr)


@pytest.fixture(autouse=True)
def long_paths_enabled(monkeypatch):
    """The machine's long-path setting reads as on, so no test depends on it; its own tests set it."""
    monkeypatch.setattr(preflight, "long_paths_value", lambda: 1)


@pytest.fixture
def vship_path(request):
    return Path(request.config.getoption("--vship"))


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
