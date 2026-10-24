"""Finnhub appears on /health/providers like the other live providers."""

from __future__ import annotations

from pydantic import SecretStr

from amrp.infrastructure.http.provider_health import PROVIDER_HEALTH
from amrp.infrastructure.news.finnhub import FinnhubSource
from tests.unit.test_v6_dashboard_features import (
    EARN,
    NEWS,
    REC,
    Clock,
    FakeTransport,
    company,
    http_client,
    json_response,
    no_wait_limiter,
)


def _src(transport: FakeTransport, key: str = "fh-secret") -> FinnhubSource:
    return FinnhubSource(
        http_client(transport),
        api_key=SecretStr(key),
        rate_limiter=no_wait_limiter(),
        cache_ttl_seconds=60,
        lookback_days=7,
        clock=Clock(),
    )


def _state(name: str) -> dict[str, object] | None:
    return next((p for p in PROVIDER_HEALTH.snapshot() if p["provider"] == name), None)


def _ok_handler(url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    if "/stock/recommendation" in url:
        return json_response(REC)
    if "/calendar/earnings" in url:
        return json_response(EARN)
    return json_response(NEWS)


def test_configured_finnhub_listed_unused_then_ok() -> None:
    PROVIDER_HEALTH.reset()
    src = _src(FakeTransport(_ok_handler))
    state = _state("finnhub")
    assert state is not None and state["state"] == "unused"
    src.fetch(company("AAPL"))
    state = _state("finnhub")
    assert state is not None and state["state"] == "ok" and state["successes"] >= 1
    assert "fh-secret" not in str(PROVIDER_HEALTH.snapshot())


def test_finnhub_without_key_not_listed() -> None:
    PROVIDER_HEALTH.reset()
    _src(FakeTransport(_ok_handler), key="")
    assert _state("finnhub") is None


def test_finnhub_failure_recorded() -> None:
    PROVIDER_HEALTH.reset()
    src = _src(FakeTransport(lambda u, h: json_response({"error": "x"}, status=500)))
    assert src.fetch(company("AAPL")) == ()
    state = _state("finnhub")
    assert state is not None and state["state"] == "failing" and state["failures"] >= 1
