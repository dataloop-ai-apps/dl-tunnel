"""
Integration tests for the dl-tunnel relay server.

Spins up a real WebSocket server in-process on a random port for each test.
Tests cover the full three-channel flow (register → connect → data → pipe)
plus all auth/error cases.
"""

import asyncio
import json
import os
import sys

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from relay import Relay  # noqa: E402


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
async def relay_server(unused_tcp_port):
    """Start a real relay server on a random port, yield URL, then stop."""
    relay = Relay()
    server = await websockets.serve(relay.handle, "localhost", unused_tcp_port)
    yield f"ws://localhost:{unused_tcp_port}"
    server.close()
    await server.wait_closed()


# ------------------------------------------------------------------
# Low-level helpers
# ------------------------------------------------------------------

async def _send_recv(ws, payload: dict, timeout: float = 3.0) -> dict:
    await ws.send(json.dumps(payload))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _open_data_channel(
    url: str,
    endpoint: str,
    session_id: str,
    password: str,
    echo: bool = False,
) -> None:
    """Open a data channel for the given session. Optionally echo frames back."""
    async with websockets.connect(url) as data_ws:
        resp = await _send_recv(data_ws, {
            "action": "data", "endpoint": endpoint,
            "session_id": session_id, "password": password,
        })
        if resp["status"] != "ok":
            return
        if echo:
            async for msg in data_ws:
                await data_ws.send(msg)


async def _run_target(
    url: str,
    endpoint: str,
    password: str,
    ready: asyncio.Event,
    n_sessions: int = 1,
    echo: bool = False,
) -> None:
    """Simulate a target: register, then handle up to n_sessions open_session messages."""
    data_tasks: list[asyncio.Task] = []
    async with websockets.connect(url) as ctrl_ws:
        resp = await _send_recv(ctrl_ws, {
            "action": "register", "endpoint": endpoint, "password": password,
        })
        assert resp["status"] == "ok", f"register failed: {resp}"
        ready.set()

        handled = 0
        async for raw in ctrl_ws:
            msg = json.loads(raw)
            if msg.get("type") == "open_session":
                t = asyncio.create_task(
                    _open_data_channel(url, endpoint, msg["session_id"], password, echo=echo)
                )
                data_tasks.append(t)
                handled += 1
                if handled >= n_sessions:
                    break

        if data_tasks:
            await asyncio.gather(*data_tasks, return_exceptions=True)


# ------------------------------------------------------------------
# Tests — registration
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_ok(relay_server):
    """Target registers with a password → relay acknowledges."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "register", "endpoint": "box", "password": "pw"})
        assert resp["status"] == "ok"
        assert "box" in resp["message"]


@pytest.mark.asyncio
async def test_duplicate_registration_rejected(relay_server):
    """Second register of the same endpoint name is rejected while the first is alive."""
    async with websockets.connect(relay_server) as first_ws:
        resp1 = await _send_recv(first_ws, {"action": "register", "endpoint": "dup", "password": "pw"})
        assert resp1["status"] == "ok"

        async with websockets.connect(relay_server) as second_ws:
            resp2 = await _send_recv(second_ws, {"action": "register", "endpoint": "dup", "password": "pw"})
            assert resp2["status"] == "error"
            assert "already registered" in resp2["message"]


# ------------------------------------------------------------------
# Tests — connect auth
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_wrong_password(relay_server):
    """Connect with wrong password is rejected with a generic auth error."""
    ready = asyncio.Event()
    target = asyncio.create_task(_run_target(relay_server, "box", "correct", ready))
    await asyncio.wait_for(ready.wait(), timeout=3)

    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "connect", "endpoint": "box", "password": "wrong"})
        assert resp["status"] == "error"
        assert "authentication failed" in resp["message"]

    target.cancel()
    await asyncio.gather(target, return_exceptions=True)


@pytest.mark.asyncio
async def test_connect_unregistered_endpoint(relay_server):
    """Connecting to an endpoint that was never registered returns auth error."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "connect", "endpoint": "ghost", "password": "pw"})
        assert resp["status"] == "error"
        assert "authentication failed" in resp["message"]


# ------------------------------------------------------------------
# Tests — missing fields
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_password_rejected(relay_server):
    """Handshake with no password field is rejected."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "register", "endpoint": "box"})
        assert resp["status"] == "error"
        assert "password" in resp["message"]


@pytest.mark.asyncio
async def test_missing_endpoint_rejected(relay_server):
    """Handshake with no endpoint field is rejected."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "register", "password": "pw"})
        assert resp["status"] == "error"
        assert "endpoint" in resp["message"]


@pytest.mark.asyncio
async def test_unknown_action_rejected(relay_server):
    """Unknown action field returns error."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {"action": "destroy", "endpoint": "x", "password": "pw"})
        assert resp["status"] == "error"


# ------------------------------------------------------------------
# Tests — full session (three-channel flow)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_session_data_flows(relay_server):
    """Developer connects, target opens data channel, bytes flow both ways."""
    ready = asyncio.Event()
    target = asyncio.create_task(
        _run_target(relay_server, "box", "pw", ready, n_sessions=1, echo=True)
    )
    await asyncio.wait_for(ready.wait(), timeout=3)

    async with websockets.connect(relay_server) as conn_ws:
        resp = await _send_recv(
            conn_ws,
            {"action": "connect", "endpoint": "box", "password": "pw"},
            timeout=5,
        )
        assert resp["status"] == "ok"

        await conn_ws.send(b"ping")
        echoed = await asyncio.wait_for(conn_ws.recv(), timeout=5)
        assert echoed == b"ping"

    await asyncio.wait_for(target, timeout=5)


@pytest.mark.asyncio
async def test_multi_user_independent_sessions(relay_server):
    """Two developers connect simultaneously; sessions are fully independent."""
    ready = asyncio.Event()
    target = asyncio.create_task(
        _run_target(relay_server, "box", "pw", ready, n_sessions=2, echo=True)
    )
    await asyncio.wait_for(ready.wait(), timeout=3)

    async def developer(data: bytes) -> bytes:
        async with websockets.connect(relay_server) as conn_ws:
            resp = await _send_recv(
                conn_ws,
                {"action": "connect", "endpoint": "box", "password": "pw"},
                timeout=5,
            )
            assert resp["status"] == "ok"
            await conn_ws.send(data)
            return await asyncio.wait_for(conn_ws.recv(), timeout=5)

    result_a, result_b = await asyncio.gather(
        developer(b"from-A"),
        developer(b"from-B"),
    )
    assert result_a == b"from-A"
    assert result_b == b"from-B"

    await asyncio.wait_for(target, timeout=5)


# ------------------------------------------------------------------
# Tests — target disconnect
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_target_disconnect_cancels_pending_connect(relay_server):
    """If the target disconnects while a developer is waiting, the developer gets an error."""
    ready = asyncio.Event()

    async def target_that_disconnects():
        async with websockets.connect(relay_server) as ctrl_ws:
            resp = await _send_recv(ctrl_ws, {
                "action": "register", "endpoint": "flaky", "password": "pw",
            })
            assert resp["status"] == "ok"
            ready.set()
            # Disconnect immediately without opening a data channel.

    target = asyncio.create_task(target_that_disconnects())
    await asyncio.wait_for(ready.wait(), timeout=3)
    await asyncio.wait_for(target, timeout=3)

    # Now the target is gone. Developer should get an error quickly.
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {
            "action": "connect", "endpoint": "flaky", "password": "pw",
        }, timeout=5)
        assert resp["status"] == "error"


# ------------------------------------------------------------------
# Tests — data channel errors
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_data_channel_unknown_session(relay_server):
    """Data channel with a fabricated session_id is rejected."""
    ready = asyncio.Event()
    target = asyncio.create_task(_run_target(relay_server, "box", "pw", ready))
    await asyncio.wait_for(ready.wait(), timeout=3)

    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {
            "action": "data", "endpoint": "box",
            "session_id": "nonexistent-uuid", "password": "pw",
        })
        assert resp["status"] == "error"

    target.cancel()
    await asyncio.gather(target, return_exceptions=True)


@pytest.mark.asyncio
async def test_data_channel_missing_session_id(relay_server):
    """Data action with no session_id field is rejected."""
    async with websockets.connect(relay_server) as ws:
        resp = await _send_recv(ws, {
            "action": "data", "endpoint": "box", "password": "pw",
        })
        assert resp["status"] == "error"
        assert "session_id" in resp["message"]
