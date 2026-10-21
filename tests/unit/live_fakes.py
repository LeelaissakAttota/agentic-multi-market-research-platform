"""Shared offline fakes for live-adapter tests (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

from financial_intelligence.domain.identity import CompanyIdentity, TickerSymbol
from financial_intelligence.infrastructure.company import InMemoryCompanyCatalog
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpResponse
from financial_intelligence.infrastructure.http.feeds import HostRateLimiter

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)

Handler = Callable[[str, dict[str, str]], HttpResponse]


def json_response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        body=json.dumps(payload).encode(),
        content_type="application/json",
        headers={},
    )


def xml_response(body: str, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        body=body.encode(),
        content_type="application/rss+xml; charset=utf-8",
        headers={},
    )


def html_home(cookie: str = "nsit=abc; Path=/") -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=b"<html></html>",
        content_type="text/html",
        headers={"set-cookie": cookie},
    )


class FakeTransport:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.urls: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        body: bytes | None = None,
    ) -> HttpResponse:
        assert method == "GET" and body is None
        self.urls.append(url)
        return self.handler(url, headers)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def http_client(transport: FakeTransport) -> BoundedHttpClient:
    return BoundedHttpClient(transport, timeout_seconds=2.0, max_retries=0, user_agent="test/1.0")


def no_wait_limiter() -> HostRateLimiter:
    return HostRateLimiter(0.0, sleep=lambda _s: None)


def catalog() -> InMemoryCompanyCatalog:
    return InMemoryCompanyCatalog()


def company(ticker: str) -> CompanyIdentity:
    matches = catalog().find_by_ticker(TickerSymbol(ticker))
    assert matches, ticker
    return matches[0]
