"""Parsers for the dashboard's public market/news helpers (no network)."""

from __future__ import annotations

import httpx

from financial_intelligence.dashboard import market_live as ml
from financial_intelligence.dashboard.providers import (
    detect_provider,
    models_request,
    parse_model_ids,
    provider,
)

CHART = {
    "chart": {
        "result": [
            {
                "meta": {"currency": "USD", "shortName": "S&P", "regularMarketTime": 1792768377},
                "timestamp": [1792122300, 1792381500, 1792554300],
                "indicators": {
                    "quote": [
                        {
                            "open": [10.0, 11.0, None],
                            "high": [12.0, 13.0, 1.0],
                            "low": [9.0, 10.0, 1.0],
                            "close": [11.0, 12.1, 1.0],
                            "volume": [100, 200, 1],
                        }
                    ]
                },
            }
        ]
    }
}


def test_parse_chart_skips_incomplete_bars_and_computes_change() -> None:
    d = ml.parse_chart(CHART, "^GSPC", "S&P 500")
    assert d is not None
    assert d.name == "S&P 500" and d.currency == "USD"
    assert len(d.bars) == 2
    assert d.change is not None and abs(d.change - 0.1) < 1e-9
    assert ml.parse_chart({}, "X") is None


def test_parse_trending_keeps_equities_only() -> None:
    payload = {
        "finance": {
            "result": [
                {
                    "quotes": [
                        {"symbol": "^DJI"},
                        {"symbol": "AAPL"},
                        {"symbol": "EURUSD=X"},
                        {"symbol": "BTC-USD"},
                    ]
                }
            ]
        }
    }
    assert ml.parse_trending(payload) == ["AAPL"]
    assert ml.parse_trending({}) == []


def test_rank_most_active_by_traded_value() -> None:
    def daily(sym: str, close: float, vol: float) -> ml.Daily:
        return ml.Daily(sym, sym, "INR", (ml.Bar("2026-10-23", 1, 1, 1, close, vol),), "")

    out = ml.rank_most_active([daily("A", 10, 5), daily("B", 2, 100), daily("C", 50, 0)], 5)
    assert [d.symbol for d in out] == ["B", "A"]


def test_parse_rss_and_failures_are_empty() -> None:
    rss = (
        "<rss><channel><item><title>Apple rises</title><link>https://x.test/a</link>"
        "<pubDate>Fri, 23 Oct 2026 15:08:19 +0000</pubDate></item>"
        "<item><title>bad</title><link>javascript:x</link></item></channel></rss>"
    )
    items = ml.parse_rss(rss, "AAPL")
    assert [(i.title, i.published) for i in items] == [("Apple rises", "2026-10-23T15:08+00:00")]
    assert ml.parse_rss("not xml", "AAPL") == []


def test_fetch_daily_returns_none_on_http_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert ml.fetch_daily("^NSEI", client=client) is None
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CHART)))
    assert ml.fetch_daily("^GSPC", client=ok) is not None


def test_detect_provider_from_key_format() -> None:
    cases = {
        "sk-or-v1-abc": "openrouter",
        "sk-ant-api03-x": "anthropic",
        "AIzaSyX": "gemini",
        "gsk_123": "groq",
        "sk-proj-1": "openai",
        "whatever": "custom",
        "": "custom",
    }
    for key, name in cases.items():
        assert detect_provider(key).key == name


def test_models_request_and_parse() -> None:
    url, _ = models_request(provider("openrouter"), "k")
    assert url == "https://openrouter.ai/api/v1/models"
    assert parse_model_ids({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}) == ["a", "b"]
    assert parse_model_ids({"models": [{"name": "models/gemini-2.0"}]}) == ["gemini-2.0"]
    assert parse_model_ids(None) == []
