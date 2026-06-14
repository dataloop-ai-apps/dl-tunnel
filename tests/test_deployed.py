"""
Smoke test against the deployed dl-tunnel service on Dataloop.

Flow:
  1. GET the gate URL with Bearer token → 302 redirect to apps URL + JWT-APP cookie.
  2. All subsequent WebSocket connections use the JWT-APP cookie for gateway auth.
  3. Tunnel auth uses a shared password (not the JWT).
  4. The test simulates both sides (target + developer) in-process.

Login first:  python -c "import dtlpy as dl; dl.setenv('prod'); dl.login()"
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

TEST_PASSWORD = "deployed-smoke-test-pw"


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
# 2) Build the WSS URL
# ------------------------------------------------------------------

def to_wss(apps_url: str) -> str:
    if apps_url.startswith("https://"):
        return "wss://" + apps_url[len("https://"):]
    if apps_url.startswith("http://"):
        return "ws://" + apps_url[len("http://"):]
    return apps_url


# ------------------------------------------------------------------
# 3) Test helpers
# ------------------------------------------------------------------

async def _send_recv(ws, payload: dict, timeout: float = 10.0) -> dict:
    await ws.send(json.dumps(payload))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _open_data_channel(
    wss_url: str,
    headers: dict,
    endpoint: str,
    session_id: str,
    password: str,
    echo: bool = False,
) -> None:
    async with websockets.connect(wss_url, additional_headers=headers) as data_ws:
        resp = await _send_recv(data_ws, {
            "action": "data", "endpoint": endpoint,
            "session_id": session_id, "password": password,
        })
        print(f"  data channel response: {resp}")
        assert resp.get("status") == "ok", f"data channel failed: {resp}"
        if echo:
            async for msg in data_ws:
                await data_ws.send(msg)


# ------------------------------------------------------------------
# 4) Full three-channel roundtrip
# ------------------------------------------------------------------

async def run_roundtrip(wss_url: str, jwt_app: str) -> None:
    """
    Simulate target + developer in-process to verify the deployed relay.

    Target side: register (control WS) → respond to open_session (data WS, echo mode).
    Developer side: connect WS → send probe → verify echo.
    """
    headers = {"Cookie": f"JWT-APP={jwt_app}"}
    endpoint_name = f"pytest-{int(time.time())}"
    target_ready = asyncio.Event()

    async def target_side() -> None:
        async with websockets.connect(wss_url, additional_headers=headers) as ctrl_ws:
            resp = await _send_recv(ctrl_ws, {
                "action": "register", "endpoint": endpoint_name, "password": TEST_PASSWORD,
            })
            print(f"  register response: {resp}")
            assert resp.get("status") == "ok", f"register failed: {resp}"
            target_ready.set()

            async for raw in ctrl_ws:
                msg = json.loads(raw)
                if msg.get("type") == "open_session":
                    asyncio.ensure_future(
                        _open_data_channel(
                            wss_url, headers, endpoint_name,
                            msg["session_id"], TEST_PASSWORD, echo=True,
                        )
                    )
                    break  # one session is enough for the smoke test

    print(f"--- starting target (endpoint={endpoint_name!r}) ---")
    target_task = asyncio.create_task(target_side())
    await asyncio.wait_for(target_ready.wait(), timeout=10)

    print(f"--- opening connect socket -> {wss_url} ---")
    async with websockets.connect(wss_url, additional_headers=headers) as conn_ws:
        resp2 = await _send_recv(conn_ws, {
            "action": "connect", "endpoint": endpoint_name, "password": TEST_PASSWORD,
        }, timeout=15)
        print(f"  connect response:  {resp2}")
        assert resp2.get("status") == "ok", f"connect failed: {resp2}"

        probe = b"hello from developer side"
        await conn_ws.send(probe)
        received = await asyncio.wait_for(conn_ws.recv(), timeout=10)
        assert received == probe, f"echo mismatch: {received!r}"
        print(f"  echo OK ({len(received)} bytes)")

    await asyncio.wait_for(target_task, timeout=5)
    print("\nAll tests passed - deployed relay is working end-to-end.")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    apps_url, jwt_app = resolve_apps_url()
    wss_url = to_wss(apps_url)

    try:
        asyncio.run(run_roundtrip(wss_url, jwt_app))
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
