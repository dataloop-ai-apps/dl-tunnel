"""
Smoke test against the deployed dl-tunnel service on Dataloop.

Flow (matches ollama-server test_app.py):
  1. GET the gate URL with Bearer token → 302 redirect to apps URL
  2. All subsequent requests go directly to the apps URL (no Bearer needed)
     — just send the JWT-APP cookie.

Login first:  python -c "import dtlpy as dl; dl.setenv('rc'); dl.login()"
Run:          python tests/test_deployed.py
"""

import asyncio
import json
import sys
import time

import dtlpy as dl
import requests
import websockets
from websockets.exceptions import InvalidStatus, InvalidStatusCode


GATE_BASE = (
    "https://gate.dataloop.ai/api/v1/apps/"
    "dl-tunnel-service-69eb31af4d5549438f428bbb/panels/relay/"
)


# ------------------------------------------------------------------
# 1) Resolve the apps URL + JWT-APP cookie
# ------------------------------------------------------------------

def resolve_apps_url() -> tuple[str, str]:
    dl.setenv("prod")
    if dl.token_expired():
        dl.login(callback_port=7364)

    session = requests.Session()
    resp = session.get(GATE_BASE, headers=dl.client_api.auth, allow_redirects=True)
    apps_url = resp.url.rstrip("/")
    jwt_app = session.cookies.get("JWT-APP") or ""

    print(f"Probe status:   {resp.status_code}")
    print(f"Resolved URL:   {apps_url}")
    print(f"JWT-APP cookie: {'yes' if jwt_app else 'no'} ({len(jwt_app)} chars)")
    print()

    if not jwt_app:
        raise SystemExit("JWT-APP cookie missing - is the service reachable?")

    return apps_url, jwt_app


# ------------------------------------------------------------------
# 2) Build the WSS URL + cookie header
# ------------------------------------------------------------------

def to_wss(apps_url: str) -> str:
    if apps_url.startswith("https://"):
        return "wss://" + apps_url[len("https://") :]
    if apps_url.startswith("http://"):
        return "ws://" + apps_url[len("http://") :]
    return apps_url


# ------------------------------------------------------------------
# 3) Test: register + connect roundtrip through the deployed relay
# ------------------------------------------------------------------

async def _handshake(ws, action: str, endpoint: str, token: str) -> dict:
    await ws.send(json.dumps({"action": action, "endpoint": endpoint, "token": token}))
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    return json.loads(raw)


async def run_roundtrip(wss_url: str, jwt_app: str, dl_token: str) -> None:
    """
    Open a register socket, then a connect socket, then pipe bytes both ways.

    Uses the ops `additional_headers` to send the JWT-APP cookie (like the
    ollama example SDK does via `default_headers`). The DL JWT payload is
    sent in the WebSocket handshake JSON.
    """
    headers = {"Cookie": f"JWT-APP={jwt_app}"}
    endpoint_name = f"pytest-{int(time.time())}"

    print(f"--- opening register socket -> {wss_url} ---")
    async with websockets.connect(wss_url, additional_headers=headers) as reg_ws:
        resp = await _handshake(reg_ws, "register", endpoint_name, dl_token)
        print(f"  register response: {resp}")
        assert resp.get("status") == "ok", f"register failed: {resp}"

        print(f"--- opening connect socket -> {wss_url} ---")
        async with websockets.connect(wss_url, additional_headers=headers) as conn_ws:
            resp2 = await _handshake(conn_ws, "connect", endpoint_name, dl_token)
            print(f"  connect response:  {resp2}")
            assert resp2.get("status") == "ok", f"connect failed: {resp2}"

            probe_dev = b"hello from developer side"
            await conn_ws.send(probe_dev)
            received = await asyncio.wait_for(reg_ws.recv(), timeout=5)
            assert received == probe_dev, f"dev->onprem mismatch: {received!r}"
            print(f"  dev -> onprem:  OK ({len(received)} bytes)")

            probe_onprem = b"hello from onprem side"
            await reg_ws.send(probe_onprem)
            received2 = await asyncio.wait_for(conn_ws.recv(), timeout=5)
            assert received2 == probe_onprem, f"onprem->dev mismatch: {received2!r}"
            print(f"  onprem -> dev:  OK ({len(received2)} bytes)")

    print("\nAll tests passed - deployed relay is working end-to-end.")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    apps_url, jwt_app = resolve_apps_url()
    wss_url = to_wss(apps_url)
    dl_token = dl.client_api.token

    try:
        asyncio.run(run_roundtrip(wss_url, jwt_app, dl_token))
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    except (InvalidStatus, InvalidStatusCode) as exc:
        print(f"FAIL: WebSocket upgrade rejected: {exc}", file=sys.stderr)
        print("  (Gate may not proxy WebSocket upgrades - check the panel config.)")
        sys.exit(2)
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
