"""Suffix-free ticker / name resolution for the dashboard inputs."""

from __future__ import annotations

from typing import Any

from amrp.dashboard.client import ApiError, resolve_any, resolved_symbol, strip_suffix, yahoo_symbol

RELIANCE = {
    "display_name": "Reliance Industries",
    "securities": [
        {
            "listings": [
                {"exchange": "BSE", "ticker": "500325", "is_primary": False},
                {"exchange": "NSE", "ticker": "RELIANCE", "is_primary": True},
            ]
        }
    ],
}
APPLE = {
    "display_name": "Apple",
    "securities": [{"listings": [{"exchange": "NASDAQ", "ticker": "AAPL", "is_primary": True}]}],
}
KNOWN = {"RELIANCE": RELIANCE, "Reliance Industries": RELIANCE, "AAPL": APPLE, "TCS": APPLE}


class FakeApi:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        q = params["q"]
        self.asked.append(q)
        if q in KNOWN:
            return {"status": "RESOLVED", "company": KNOWN[q]}
        if q == "boom":
            raise ApiError(500, "x")
        return {"status": "NOT_FOUND"}


def no_lookup(text: str) -> list[tuple[str, str]]:
    return []


def test_symbol_helpers() -> None:
    assert yahoo_symbol("RELIANCE", "NSE") == "RELIANCE.NS"
    assert yahoo_symbol("500325", "BSE") == "500325.BO"
    assert yahoo_symbol("AAPL", "NASDAQ") == "AAPL"
    assert strip_suffix("reliance.ns") == "reliance"
    assert strip_suffix("TCS.BO") == "TCS"
    assert strip_suffix("AAPL") == "AAPL"
    assert resolved_symbol({"company": RELIANCE}) == "RELIANCE.NS"
    assert resolved_symbol(None) == ""


def test_bare_ticker_resolves_to_exchange_symbol() -> None:
    res = resolve_any(FakeApi(), "RELIANCE", ("RELIANCE",), lookup=no_lookup)
    assert res is not None and res["resolved_symbol"] == "RELIANCE.NS"
    assert res["matched_phrase"] == "RELIANCE"


def test_us_ticker_and_name() -> None:
    assert resolve_any(FakeApi(), "AAPL", ("AAPL",), lookup=no_lookup)["resolved_symbol"] == "AAPL"
    res = resolve_any(FakeApi(), "Reliance Industries", ("Reliance Industries",), lookup=no_lookup)
    assert res["resolved_symbol"] == "RELIANCE.NS"


def test_suffixed_input_falls_back_to_bare_symbol() -> None:
    api = FakeApi()
    res = resolve_any(api, "RELIANCE.NS", ("RELIANCE.NS",), lookup=no_lookup)
    assert res is not None and res["matched_phrase"] == "RELIANCE"
    assert api.asked == ["RELIANCE.NS", "RELIANCE"]


def test_yahoo_search_fallback_for_unknown_text() -> None:
    def lookup(text: str) -> list[tuple[str, str]]:
        assert text == "reliance ind"
        return [("RELIANCE.NS", "Reliance Industries")]

    res = resolve_any(FakeApi(), "reliance ind", ("reliance ind",), lookup=lookup)
    assert res is not None and res["resolved_symbol"] == "RELIANCE.NS"


def test_unresolvable_returns_none() -> None:
    assert resolve_any(FakeApi(), "boom", ("boom",), lookup=no_lookup) is None
