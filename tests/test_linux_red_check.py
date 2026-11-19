"""Throwaway, never merged: proves the tests-linux check goes red on a Linux-only failure."""

import sys

from tests.timing import budget


@budget(load="pure")
def test_fails_on_linux_only():
    """Fails everywhere but Windows."""
    assert sys.platform == "win32", "throwaway: a Linux-only failure must turn tests-linux red"
