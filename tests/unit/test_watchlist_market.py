"""Watchlist market symbols, inline trend cell, Neutral cross badge, setup guide."""

from __future__ import annotations

from datetime import UTC, datetime

from amrp.dashboard import setup_guide as sg
from amrp.dashboard import trend as tr
from amrp.dashboard.markets import IBar


def _bar(day: int, hour: int, o: float, c: float) -> IBar:
    return IBar(datetime(2026, 10, day, hour, tzinfo=UTC), o, max(o, c), min(o, c), c)


def test_market_symbol_detection() -> None:
    for s in ("GC=F", "eurusd=x", "BTC-USD", "^NSEI", "ETH-USDT"):
        assert tr.is_market_symbol(s)
    for s in ("AAPL", "RELIANCE", "Reliance Industries", "TCS"):
        assert not tr.is_market_symbol(s)


def test_day_move_absolute_and_percent() -> None:
    bars = [_bar(28, 10, 90, 95), _bar(29, 10, 100, 101), _bar(29, 11, 101, 102)]
    last, d_abs, d_pct = tr.day_move(bars)
    assert last == 102 and d_abs == 2 and abs(d_pct - 0.02) < 1e-9
    assert tr.day_move([]) is None


def test_inline_cell_colours_and_icons() -> None:
    bull = tr.inline_html(tr.classify(0.01))
    bear = tr.inline_html(tr.classify(-0.01))
    neu = tr.inline_html(tr.classify(0.0))
    assert "Bullish" in bull and "#22c55e" in bull and "\U0001f402" in bull
    assert "Bearish" in bear and "#ef4444" in bear and "\U0001f43b" in bear
    assert "Neutral" in neu and "#facc15" in neu and "\u2716" in neu
    assert "\u2696" not in neu
    assert "n/a" in tr.inline_html(None)


def test_neutral_badge_is_yellow_cross() -> None:
    h = tr.badge_html(tr.classify(0.0), datetime.now(UTC))
    assert "\u2716" in h and "#facc15" in h


def test_setup_guide_covers_providers_without_secrets() -> None:
    names = " ".join(r["Key / provider"] for r in sg.GUIDE)
    for n in ("OpenRouter", "OpenAI", "Anthropic", "Gemini", "Finnhub", "Alpha Vantage", "Ollama"):
        assert n in names
    for r in sg.GUIDE:
        assert set(r) == {"Key / provider", "Used for", "Needed?", "Get it", "Connect & test"}
        assert r["Get it"] == "-" or r["Get it"].startswith("https://")
        assert "sk-" not in " ".join(r.values())
