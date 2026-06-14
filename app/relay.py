"""
WebSocket relay router.

Three-channel design for multi-user SSH tunnels:

  Control channel  — target registers with a password; the WS stays open
                     for the tunnel lifetime. The relay sends
                     {"type": "open_session", "session_id": "..."} over it
                     whenever a developer connects.

  Connect channel  — developer connects with the shared password. The relay
                     validates, notifies the target via the control channel,
                     then waits for the matching data channel to arrive.

  Data channel     — target opens one WS per session (keyed by session_id +
                     password). The relay pipes connect-WS ↔ data-WS.

Endpoint key is the bare machine name. Anyone with the correct password can
connect. No per-user namespace.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets

from auth import hash_password, verify_password

log = logging.getLogger("relay")

# websockets >= 14 uses ServerConnection; earlier versions used WebSocketServerProtocol.
_WS = Any


@dataclass
class _PendingSession:
    """State for one developer waiting for the target to open a data channel."""
    connect_ws: _WS
    data_ready: asyncio.Event = field(default_factory=asyncio.Event)
    data_ws: _WS | None = None


@dataclass
class _Registration:
    control_ws: _WS
    password_hash: str
    sessions: dict[str, _PendingSession] = field(default_factory=dict)


class Relay:
    def __init__(self) -> None:
        self._registry: dict[str, _Registration] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public handler — entry point for every new WebSocket connection
    # ------------------------------------------------------------------

    async def handle(self, ws: _WS) -> None:
        """Dispatch incoming connection based on the action field."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            await _send_error(ws, "handshake timeout")
            return
        except (json.JSONDecodeError, TypeError):
            await _send_error(ws, "expected JSON handshake")
            return

        action = msg.get("action")
        endpoint = msg.get("endpoint", "")
        password = msg.get("password", "")
        session_id = msg.get("session_id", "")

        if not endpoint:
            await _send_error(ws, "missing endpoint name")
            return
        if not password:
            await _send_error(ws, "missing password")
            return

        if action == "register":
            await self._register(ws, endpoint, password)
        elif action == "connect":
            await self._connect(ws, endpoint, password)
        elif action == "data":
            if not session_id:
                await _send_error(ws, "missing session_id")
                return
            await self._data(ws, endpoint, session_id, password)
        else:
            await _send_error(ws, f"unknown action: {action!r}")

    # ------------------------------------------------------------------
    # Register (target / on-prem side) — control channel
    # ------------------------------------------------------------------

    async def _register(self, ws: _WS, endpoint: str, password: str) -> None:
        pw_hash = hash_password(password)

        async with self._lock:
            if endpoint in self._registry:
                await _send_error(ws, f"endpoint already registered: {endpoint}")
                return
            reg = _Registration(control_ws=ws, password_hash=pw_hash)
            self._registry[endpoint] = reg

        log.info("registered  endpoint=%s", endpoint)
        await _send_ok(ws, f"registered as {endpoint}")

        try:
            # Drain any unexpected incoming frames; holds the control channel open.
            async for _ in ws:
                pass
        finally:
            async with self._lock:
                self._registry.pop(endpoint, None)
            # Wake any _connect coroutines blocked on data_ready so they can fail fast.
            for session in reg.sessions.values():
                session.data_ready.set()  # data_ws is None → _connect will report error
            log.info("unregistered endpoint=%s", endpoint)

    # ------------------------------------------------------------------
    # Connect (developer side)
    # ------------------------------------------------------------------

    async def _connect(self, ws: _WS, endpoint: str, password: str) -> None:
        async with self._lock:
            reg = self._registry.get(endpoint)

        if reg is None or not verify_password(password, reg.password_hash):
            await _send_error(ws, "authentication failed")
            return

        session_id = str(uuid.uuid4())
        session = _PendingSession(connect_ws=ws)

        async with self._lock:
            reg.sessions[session_id] = session

        log.info("connect      endpoint=%s session=%s", endpoint, session_id)

        try:
            await reg.control_ws.send(
                json.dumps({"type": "open_session", "session_id": session_id})
            )
        except Exception:
            async with self._lock:
                reg.sessions.pop(session_id, None)
            await _send_error(ws, "target unavailable")
            return

        try:
            await asyncio.wait_for(session.data_ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            async with self._lock:
                reg.sessions.pop(session_id, None)
            await _send_error(ws, "timeout waiting for target data channel")
            return

        if session.data_ws is None:
            # Target disconnected while we were waiting.
            async with self._lock:
                reg.sessions.pop(session_id, None)
            await _send_error(ws, "target disconnected")
            return

        await _send_ok(ws, "connected")
        log.info("session pipe endpoint=%s session=%s", endpoint, session_id)
        await _pipe(ws, session.data_ws)

        async with self._lock:
            reg.sessions.pop(session_id, None)
        log.info("session end  endpoint=%s session=%s", endpoint, session_id)

    # ------------------------------------------------------------------
    # Data channel (target side, one per session)
    # ------------------------------------------------------------------

    async def _data(self, ws: _WS, endpoint: str, session_id: str, password: str) -> None:
        async with self._lock:
            reg = self._registry.get(endpoint)

        if reg is None or not verify_password(password, reg.password_hash):
            await _send_error(ws, "authentication failed")
            return

        async with self._lock:
            session = reg.sessions.get(session_id)

        if session is None:
            await _send_error(ws, f"unknown session: {session_id}")
            return

        session.data_ws = ws
        session.data_ready.set()

        await _send_ok(ws, "data channel ready")
        log.info("data channel endpoint=%s session=%s", endpoint, session_id)

        # Hold open until _connect's _pipe closes this WS.
        await ws.wait_closed()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _send_ok(ws: _WS, msg: str) -> None:
    await ws.send(json.dumps({"status": "ok", "message": msg}))


async def _send_error(ws: _WS, msg: str) -> None:
    try:
        await ws.send(json.dumps({"status": "error", "message": msg}))
    except Exception:
        pass
    await ws.close()


async def _pipe(ws_a: _WS, ws_b: _WS) -> None:
    """Copy frames bidirectionally between two WebSocket connections."""

    async def forward(src: _WS, dst: _WS) -> None:
        try:
            async for msg in src:
                await dst.send(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await dst.close()

    await asyncio.gather(
        forward(ws_a, ws_b),
        forward(ws_b, ws_a),
        return_exceptions=True,
    )
