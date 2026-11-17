"""pytest wiring (SPEC "Tests"): the no-network guard, a fresh process config per test and the timing report.

The per-test timing report is tests/timing.py's. Under pytest-xdist each worker tags its tests' reports with their
budgets (user_properties) and the controller, which receives every report, writes the 1 report.
"""

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
    """A test attempted a network connection; tests are offline."""


def _refuse(*_args, **_kwargs):
    msg = "tests are offline: a network connection was attempted"
    raise NetworkBlockedError(msg)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every socket connection in every test: tests are offline."""
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)


def pytest_addoption(parser):
    """Add --vship PATH: the real Vship library, which runs the gpu tests."""
    parser.addoption("--vship", default=None, help="path of the real Vship library: runs the gpu tests")


def _runs_tests(config):
    """Return whether this process runs tests: an xdist worker or a serial run.

    The xdist controller only hands out tests.
    """
    return hasattr(config, "workerinput") or not getattr(config.option, "numprocesses", None)


def pytest_configure(config):
    """Register the gpu marker; a process that runs tests takes its pool databases (tests/pg.py)."""
    config.addinivalue_line("markers", "gpu: needs the real Vship library and a GPU (run with --vship PATH)")
    if _runs_tests(config):
        pg.process = pg.Process()  # takes its pool databases here, outside every test's timing


def pytest_sessionfinish(session):
    """Fail the run when a test built the unassigned database tests/test_config.yaml names (dropped now)."""
    if not hasattr(session.config, "workerinput") and pg.unassigned_built():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        session.config.get_terminal_writer().line(
            f"error: a test built the database tests/test_config.yaml names ({pg.UNASSIGNED}, dropped now): "
            "take the database from site_cfg",
            red=True,
        )


def pytest_unconfigure():
    """Close this process's pool databases."""
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
    """Install fakes of the convert stage's tools and scorer (tests/fakes/convert.py), except in a gpu test.

    A gpu test runs only with --vship and gets the real scorer.
    """
    if request.node.get_closest_marker("gpu"):
        if request.config.getoption("--vship") is None:
            pytest.skip("gpu test: run with --vship PATH")
        return
    fake_convert.install(monkeypatch.setattr)


@pytest.fixture(autouse=True)
def long_paths_enabled(monkeypatch):
    """Make the machine's long-path setting read as on, so no test depends on it; its own tests set it."""
    monkeypatch.setattr(preflight, "long_paths_value", lambda: 1)


@pytest.fixture
def vship_path(request):
    """Provide the real Vship library's path, given with --vship."""
    return Path(request.config.getoption("--vship"))


@pytest.fixture(autouse=True)
def fresh_config():
    """Reset the process config after every test."""
    yield
    config.reset()  # a test's init(..., sets) must not reach the next test


def pytest_runtest_logreport(report):
    """Add a report's duration (setup, call and teardown) to its test's time, and keep its budget tag."""
    _rows[report.nodeid] = _rows.get(report.nodeid, 0.0) + report.duration  # setup + call + teardown
    for name, value in report.user_properties:
        if name == TAG:
            _tags[report.nodeid] = value


def pytest_collection_modifyitems(items):
    """Tag every collected test with its time budget (tests/timing.py), carried in user_properties."""
    for item in items:
        item.user_properties.append((TAG, timing.budget_for(getattr(item, "obj", None), getattr(item, "cls", None))))


def pytest_terminal_summary(terminalreporter, config):
    """Print the timing report and write it to TIMINGS, budgets checked for a serial run only; the controller's job."""
    if hasattr(config, "workerinput"):  # an xdist worker: the controller reports
        return
    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    rows = [timing.row(i, s, _tags.get(i)) for i, s in _rows.items()]
    parallel = bool(getattr(config.option, "numprocesses", None))  # budgets hold for a serial run only
    timing.report(rows, terminalreporter, TIMINGS, check_budgets=not parallel)
