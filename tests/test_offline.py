"""Tests never open a network connection (SPEC "Tests"): conftest's guard refuses every socket connect."""

import socket

import pytest

from tests.conftest import NetworkBlocked
from tests.timing import budget


@budget(load="pure")
def test_network_connection_is_refused():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 80), timeout=1)  # TEST-NET-1: never routed
