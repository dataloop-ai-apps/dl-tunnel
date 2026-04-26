"""
WebSocket relay router.

Maintains a registry of registered on-prem endpoints. When a developer
connects and requests an endpoint, the two WebSocket streams are piped
bidirectionally until either side disconnects.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import websockets

from auth import AuthError, Identity, endpoint_key, validate_token

log = logging.getLogger("relay")

# websockets >= 14 uses ServerConnection; earlier versions used WebSocketServerProtocol.
# Use Any to stay compatible with both.
_WS = Any


@dataclass
class _Registration:
    ws: _WS
    identity: Identity
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class Relay:
    def __init__(self) -> None:
        self._registry: dict[str, _Registration] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public handler — entry point for every new WebSocket connection
    # ------------------------------------------------------------------

    async def handle(self, ws: _WS) -> None:
        """Dispatch incoming connection to register or connect."""
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
        token = msg.get("token", "")
        machine = msg.get("endpoint", "")

        try:
            identity = validate_token(token)
        except AuthError as exc:
            await _send_error(ws, f"auth: {exc}")
            return

        if not machine:
            await _send_error(ws, "missing endpoint name")
            return

        key = endpoint_key(identity, machine)

        if action == "register":
            await self._register(ws, key, identity)
        elif action == "connect":
            await self._connect(ws, key, identity)
        else:
            await _send_error(ws, f"unknown action: {action!r}")

    # ------------------------------------------------------------------
    # Register (on-prem side)
    # ------------------------------------------------------------------

    async def _register(
        self,
        ws: _WS,
        key: str,
        identity: Identity,
    ) -> None:
        async with self._lock:
            if key in self._registry:
                await _send_error(ws, f"endpoint already registered: {key}")
                return
            reg = _Registration(ws=ws, identity=identity)
            self._registry[key] = reg

        log.info("registered  key=%s user=%s", key, identity.email)
        await _send_ok(ws, f"registered as {key}")
        reg.ready.set()

        try:
            # Hold the connection open. The on-prem side just pings/waits.
            await ws.wait_closed()
        finally:
            async with self._lock:
                self._registry.pop(key, None)
            log.info("unregistered key=%s", key)

    # ------------------------------------------------------------------
    # Connect (developer side)
    # ------------------------------------------------------------------

    async def _connect(
        self,
        ws: _WS,
        key: str,
        identity: Identity,
    ) -> None:
        async with self._lock:
            reg = self._registry.get(key)

        if reg is None:
            await _send_error(ws, f"endpoint not found: {key}")
            return

        log.info("connecting   key=%s user=%s", key, identity.email)
        await _send_ok(ws, "connected")

        await _pipe(ws, reg.ws)
        log.info("session ended key=%s", key)


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
