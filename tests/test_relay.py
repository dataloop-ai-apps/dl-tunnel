"""
Integration tests for the dl-tunnel relay server.

Spins up a real WebSocket server in-process on a random port for each test.
Tests cover the full register → connect → pipe flow plus auth/error cases.
"""

import asyncio
import json
import os
import sys
import time

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from relay import Relay  # noqa: E402
from auth import AuthError  # noqa: E402,F401


# ------------------------------------------------------------------
# Helpers: build minimal JWTs without a real DL backend
# ------------------------------------------------------------------

import base64


def _make_fake_jwt(
    email: str = "dev@example.com",
    sub: str = "google-oauth2|dev",
    expired: bool = False,
) -> str:
    """
    Build a minimal unsigned JWT for testing.

    The relay uses verify_signature=False so we just need a valid structure
    with the right claims (email, sub, exp).
    """
    import json as _json

    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()

    exp = int(time.time()) + (-10 if expired else 3600)
    payload_data = {"email": email, "sub": sub, "exp": exp}
    payload = (
        base64.urlsafe_b64encode(_json.dumps(payload_data).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.fakesig"


VALID_TOKEN = _make_fake_jwt()
EXPIRED_TOKEN = _make_fake_jwt(expired=True)


def _make_no_identity_jwt() -> str:
    """JWT with no email and no sub -- should be rejected."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    exp = int(time.time()) + 3600
    import json as _json
    payload_data = {"exp": exp}
    payload = (
        base64.urlsafe_b64encode(_json.dumps(payload_data).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.fakesig"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def relay_url(unused_tcp_port):
    return f"ws://localhost:{unused_tcp_port}"


@pytest.fixture
async def relay_server(unused_tcp_port):
    """Start a real relay server on a random port, yield URL, then stop."""
    relay = Relay()
    server = await websockets.serve(relay.handle, "localhost", unused_tcp_port)
    yield f"ws://localhost:{unused_tcp_port}"
    server.close()
    await server.wait_closed()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _handshake(ws, action: str, endpoint: str, token: str) -> dict:
    await ws.send(json.dumps({"action": action, "endpoint": endpoint, "token": token}))
    raw = await asyncio.wait_for(ws.recv(), timeout=3)
    return json.loads(raw)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_and_connect_appear_in_registry(relay_server):
    """On-prem registers, developer connects — both handshakes succeed."""
    url = relay_server

    async with websockets.connect(url) as reg_ws:
        resp = await _handshake(reg_ws, "register", "my-machine", VALID_TOKEN)
        assert resp["status"] == "ok"

        async with websockets.connect(url) as conn_ws:
            resp2 = await _handshake(conn_ws, "connect", "my-machine", VALID_TOKEN)
            assert resp2["status"] == "ok"


@pytest.mark.asyncio
async def test_bidirectional_pipe(relay_server):
    """Data flows both ways through the relay."""
    url = relay_server

    async with websockets.connect(url) as reg_ws:
        await _handshake(reg_ws, "register", "pipe-test", VALID_TOKEN)

        async with websockets.connect(url) as conn_ws:
            await _handshake(conn_ws, "connect", "pipe-test", VALID_TOKEN)

            # Developer → on-prem
            await conn_ws.send(b"hello from developer")
            received = await asyncio.wait_for(reg_ws.recv(), timeout=2)
            assert received == b"hello from developer"

            # On-prem → developer
            await reg_ws.send(b"hello from onprem")
            received2 = await asyncio.wait_for(conn_ws.recv(), timeout=2)
            assert received2 == b"hello from onprem"


@pytest.mark.asyncio
async def test_connect_to_unregistered_endpoint_returns_error(relay_server):
    """Connecting to a non-existent endpoint returns a clear error."""
    url = relay_server

    async with websockets.connect(url) as ws:
        resp = await _handshake(ws, "connect", "does-not-exist", VALID_TOKEN)
        assert resp["status"] == "error"
        assert "not found" in resp["message"]


@pytest.mark.asyncio
async def test_expired_token_rejected(relay_server):
    """Expired JWT is rejected at handshake."""
    url = relay_server

    async with websockets.connect(url) as ws:
        resp = await _handshake(ws, "register", "any-machine", EXPIRED_TOKEN)
        assert resp["status"] == "error"
        assert "expired" in resp["message"]


@pytest.mark.asyncio
async def test_missing_token_rejected(relay_server):
    """Missing token is rejected."""
    url = relay_server

    async with websockets.connect(url) as ws:
        resp = await _handshake(ws, "register", "any-machine", "")
        assert resp["status"] == "error"


@pytest.mark.asyncio
async def test_missing_identity_rejected(relay_server):
    """JWT with no email and no sub is rejected."""
    url = relay_server
    token = _make_no_identity_jwt()

    async with websockets.connect(url) as ws:
        resp = await _handshake(ws, "register", "any-machine", token)
        assert resp["status"] == "error"
        assert "email/sub" in resp["message"]


@pytest.mark.asyncio
async def test_duplicate_registration_rejected(relay_server):
    """Second registration of the same endpoint is rejected while first is alive."""
    url = relay_server

    async with websockets.connect(url) as first_ws:
        resp1 = await _handshake(first_ws, "register", "dup-test", VALID_TOKEN)
        assert resp1["status"] == "ok"

        async with websockets.connect(url) as second_ws:
            resp2 = await _handshake(second_ws, "register", "dup-test", VALID_TOKEN)
            assert resp2["status"] == "error"
            assert "already registered" in resp2["message"]


@pytest.mark.asyncio
async def test_user_isolation(relay_server):
    """Endpoint registered by user A is not visible to user B."""
    url = relay_server
    token_a = _make_fake_jwt(sub="user-a")
    token_b = _make_fake_jwt(sub="user-b")

    async with websockets.connect(url) as reg_ws:
        await _handshake(reg_ws, "register", "shared-name", token_a)

        # user-b tries to connect -- resolves to user-b:shared-name, not user-a:shared-name
        async with websockets.connect(url) as conn_ws:
            resp = await _handshake(conn_ws, "connect", "shared-name", token_b)
            assert resp["status"] == "error"
            assert "not found" in resp["message"]


@pytest.mark.asyncio
async def test_unknown_action_rejected(relay_server):
    """Unknown action field returns error."""
    url = relay_server

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"action": "destroy", "endpoint": "x", "token": VALID_TOKEN}))
        raw = await asyncio.wait_for(ws.recv(), timeout=3)
        resp = json.loads(raw)
        assert resp["status"] == "error"
