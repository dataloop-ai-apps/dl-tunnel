"""pytest configuration for dl-tunnel tests."""

import socket

import pytest


@pytest.fixture
def unused_tcp_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]
