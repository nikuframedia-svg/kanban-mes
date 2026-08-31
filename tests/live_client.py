"""Cliente HTTP real para testes, sem depender do TestClient do Starlette."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn


class LiveTestClient:
    def __init__(self, app, *, follow_redirects: bool = False):
        self.app = app
        self.follow_redirects = follow_redirects
        self._socket = None
        self._server = None
        self._thread = None
        self._client = None

    def __enter__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        port = self._socket.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(
            self.app, log_level="error", lifespan="on", access_log=False,
        ))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and self._thread.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("Uvicorn de teste não arrancou em 10 segundos")
            time.sleep(0.01)
        if not self._thread.is_alive():
            raise RuntimeError("Uvicorn de teste terminou durante o arranque")
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            follow_redirects=self.follow_redirects,
            timeout=15,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._client:
            self._client.close()
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)
        if self._socket:
            self._socket.close()

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)
