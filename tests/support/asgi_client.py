"""Synchronous in-process ASGI test client built on ``httpx.ASGITransport``.

Drop-in replacement for the subset of ``starlette.testclient.TestClient``
used by this suite. Starlette deprecated its httpx-backed TestClient in
favour of the separate ``httpx2`` package; this helper avoids that warning
without a new dependency. Each client owns one event loop so application
state created inside the app stays on a single loop (run in a background
thread, so the client is safe to share across threads like ``TestClient``),
and ``with`` blocks run the application lifespan exactly like ``TestClient``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any

import httpx
from fastapi import FastAPI

__all__ = ["TestClient"]


class TestClient:
    """Minimal synchronous ASGI client (``TestClient``-compatible subset)."""

    __test__ = False  # not a pytest test class

    def __init__(
        self,
        app: FastAPI,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_server_exceptions)
        self._client = httpx.AsyncClient(
            transport=transport, base_url=base_url, headers=headers, follow_redirects=True
        )
        self._lifespan: AbstractAsyncContextManager[Any] | None = None

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def __enter__(self) -> TestClient:
        self._lifespan = self.app.router.lifespan_context(self.app)
        self._run(self._lifespan.__aenter__())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._lifespan is not None:
                self._run(self._lifespan.__aexit__(exc_type, exc, tb))
                self._lifespan = None
        finally:
            self.close()

    def close(self) -> None:
        if not self._loop.is_closed():
            self._run(self._client.aclose())
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response: httpx.Response = self._run(self._client.request(method, url, **kwargs))
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)
