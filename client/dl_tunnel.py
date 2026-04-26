"""dl-tunnel client.

Two commands, no flags for credentials, nothing on disk:

    dl-tunnel start target --name <name> [--local 127.0.0.1:22]
    dl-tunnel start local  --name <name> [--port 0]

Both prompt for a Dataloop JWT, resolve the relay URL from the installed
``dl-tunnel`` DPK, and run the tunnel until the JWT exp claim is reached
or the process exits.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from getpass import getpass
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import TemporaryDirectory

import websockets

DPK_NAME = "dl-tunnel"
HANDSHAKE_TIMEOUT = 10
RECONNECT_DELAY = 2

log = logging.getLogger("dl-tunnel")


# ---------------------------------------------------------------------------
# Token input
# ---------------------------------------------------------------------------

def prompt_token() -> str:
    """Read the JWT once. getpass on a TTY, raw stdin when piped."""
    raw = getpass("Dataloop token: ") if sys.stdin.isatty() else sys.stdin.read()
    token = raw.strip()
    if not token:
        raise SystemExit("empty token")
    return token


def jwt_exp(token: str) -> int:
    """Decode the JWT exp claim. Fail loud on anything malformed or expired."""
    parts = token.split(".")
    if len(parts) != 3:
        raise SystemExit("token is not a JWT")
    payload_b64 = parts[1]
    pad = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    except (ValueError, json.JSONDecodeError):
        raise SystemExit("token payload is not valid base64 JSON")
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise SystemExit("token has no exp claim")
    if exp <= time.time():
        raise SystemExit("token already expired")
    return exp


# ---------------------------------------------------------------------------
# Relay resolution
# ---------------------------------------------------------------------------

def resolve_relay(token: str) -> tuple[str, dict[str, str]]:
    """Return (wss_url, headers) for the installed dl-tunnel app."""
    gate_url = _lookup_gate_url(token)
    return _probe_gate(gate_url, token)


def _lookup_gate_url(token: str) -> str:
    from dtlpy import Filters
    from dtlpy.new_instance import Dtlpy

    with TemporaryDirectory() as tmp:
        sdk = Dtlpy(cookie_filepath=str(Path(tmp) / "dl-cookie"))
        sdk.setenv("prod")
        sdk.login_token(token=token)

        dpk = sdk.dpks.get(dpk_name=DPK_NAME)
        filters = Filters(field="dpkName", values=dpk.name, resource="apps")
        apps = list(sdk.apps.list(filters=filters).all())
        if not apps:
            raise SystemExit(f"no installed app for DPK {DPK_NAME!r}")
        if len(apps) > 1:
            raise SystemExit(f"multiple apps installed for DPK {DPK_NAME!r}")

        app = apps[0]
        if "relay" not in app.routes:
            raise SystemExit(f"app {app.name!r} has no 'relay' route")
        return app.routes["relay"]


def _probe_gate(gate_url: str, token: str) -> tuple[str, dict[str, str]]:
    """Bearer-probe the gate URL once to capture the JWT-APP cookie."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(gate_url, headers={"Authorization": f"Bearer {token}"})

    final_url = gate_url
    try:
        with opener.open(req, timeout=15) as resp:
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        # WS-only handlers typically 5xx on plain HTTP; cookies are already in the jar.
        final_url = getattr(e, "url", None) or gate_url

    jwt_app = next((c.value for c in jar if c.name == "JWT-APP"), "")
    if not jwt_app:
        raise SystemExit(f"gateway did not set JWT-APP cookie at {gate_url}")

    wss_url = final_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return wss_url, {"Cookie": f"JWT-APP={jwt_app}"}


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------

async def _handshake(ws, action: str, name: str, token: str) -> None:
    await ws.send(json.dumps({"action": action, "endpoint": name, "token": token}))
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT)
    msg = json.loads(raw)
    if msg.get("status") != "ok":
        raise SystemExit(f"relay error: {msg.get('message', 'unknown')}")


async def _bridge(ws, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Pipe a WebSocket and a TCP stream bidirectionally until either closes."""

    async def ws_to_tcp() -> None:
        try:
            async for msg in ws:
                writer.write(msg if isinstance(msg, bytes) else msg.encode())
                await writer.drain()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def tcp_to_ws() -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send(data)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)


# ---------------------------------------------------------------------------
# target: register and forward to a local TCP service (sshd by default)
# ---------------------------------------------------------------------------

async def cmd_target(name: str, local_host: str, local_port: int) -> None:
    token = prompt_token()
    exp = jwt_exp(token)
    wss_url, headers = resolve_relay(token)
    log.info("target %r registered against %s; forwarding to %s:%d",
             name, wss_url, local_host, local_port)

    async def loop() -> None:
        while True:
            try:
                async with websockets.connect(wss_url, additional_headers=headers) as ws:
                    await _handshake(ws, "register", name, token)
                    reader, writer = await asyncio.open_connection(local_host, local_port)
                    await _bridge(ws, reader, writer)
            except (websockets.exceptions.WebSocketException, OSError) as e:
                log.warning("relay error: %s; retrying in %ds", e, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    await _run_until(loop(), exp)


# ---------------------------------------------------------------------------
# local: open a TCP listener; each accepted socket becomes a connect session
# ---------------------------------------------------------------------------

async def cmd_local(name: str, port: int) -> None:
    token = prompt_token()
    exp = jwt_exp(token)
    wss_url, headers = resolve_relay(token)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with websockets.connect(wss_url, additional_headers=headers) as ws:
                await _handshake(ws, "connect", name, token)
                await _bridge(ws, reader, writer)
        except (websockets.exceptions.WebSocketException, OSError) as e:
            log.warning("session failed: %s", e)
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    host, bound_port = server.sockets[0].getsockname()[:2]
    print(f"listening on {host}:{bound_port}", flush=True)
    log.info("local listener %s:%d -> target %r", host, bound_port, name)

    async with server:
        await _run_until(server.serve_forever(), exp)


# ---------------------------------------------------------------------------
# Lifetime
# ---------------------------------------------------------------------------

async def _run_until(coro, exp: int) -> None:
    deadline = exp - time.time()
    if deadline <= 0:
        raise SystemExit("token already expired")
    try:
        await asyncio.wait_for(coro, timeout=deadline)
    except asyncio.TimeoutError:
        pass
    raise SystemExit("token expired; tunnel closed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_host_port(hp: str) -> tuple[str, int]:
    host, _, port = hp.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"--local must be HOST:PORT, got {hp!r}")
    return host, int(port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="dl-tunnel", description="Dataloop FaaS SSH tunnel")
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start", help="start a tunnel role")
    role = start.add_subparsers(dest="role", required=True)

    target = role.add_parser("target", help="register this machine as the SSH target")
    target.add_argument("--name", required=True)
    target.add_argument("--local", default="127.0.0.1:22",
                        help="local HOST:PORT to forward to (default 127.0.0.1:22)")

    local = role.add_parser("local", help="open a local SSH listener pointing at a target")
    local.add_argument("--name", required=True)
    local.add_argument("--port", type=int, default=0,
                       help="local TCP port (default 0 = pick a free port)")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.role == "target":
        host, port = _parse_host_port(args.local)
        asyncio.run(cmd_target(args.name, host, port))
    else:
        asyncio.run(cmd_local(args.name, args.port))


if __name__ == "__main__":
    main()
