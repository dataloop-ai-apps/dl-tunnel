"""
dl-tunnel relay server — Dataloop FaaS entrypoint.

Runs as a scale-to-one custom server (isCustomServer: true). The Runner
__init__ starts an asyncio WebSocket server in a background thread and
returns immediately. The server then runs for the lifetime of the pod.

Environment variables:
  DL_TUNNEL_PORT   TCP port to listen on (default: 8765)
  DL_TUNNEL_HOST   Bind address (default: 0.0.0.0)
  LOG_LEVEL        Python log level (default: INFO)
"""

import asyncio
import logging
import os
import threading

import dtlpy as dl
import websockets

from relay import Relay

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("dl-tunnel")

HOST = os.environ.get("DL_TUNNEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("DL_TUNNEL_PORT", "3000"))


class Runner(dl.BaseServiceRunner):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._relay = Relay()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()

        self._thread = threading.Thread(
            target=self._run_loop,
            name="dl-tunnel-relay",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(timeout=10):
            raise RuntimeError("WebSocket server failed to start within 10s")

        log.info("dl-tunnel relay ready on %s:%d", HOST, PORT)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async with websockets.serve(self._relay.handle, HOST, PORT):
            self._ready.set()
            # Run forever — scale-to-one pod never exits voluntarily.
            await asyncio.Future()

    def dummy(self):
        """Placeholder — required by the DPK module function list."""
        pass


if __name__ == "__main__":
    # Local dev: run without the FaaS harness.
    runner = Runner()
    runner._thread.join()
