from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from amrp.config.settings import Settings
from amrp.infrastructure.http import BoundedHttpClient, HttpResponse
from amrp.infrastructure.market.alpha_vantage import (
    AlphaVantageMarketDataAdapter,
    DailyRequestBudget,
    alpha_vantage_symbol,
)
from tests.unit.market_fixtures import (
    APPLE_COMPANY_ID,
    apple_listing,
    reliance_nse_listing,
)
from tests.unit.test_market_live import FakeTransport

SECRET = "AV-SECRET-KEY-should-never-leak"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _daily(days: int = 3) -> dict[str, Any]:
    rows = {
        f"2026-10-{10 + i:02d}": {
            "1. open": "100.0",
            "2. high": "110.0",
            "3. low": "99.0",
            "4. close": f"{100 + i}.5",
            "5. volume": "12345",
        }
        for i in range(days)
    }
    return {"Meta Data": {}, "Time Series (Daily)": rows}


def _adapter(
    responses: list[dict[str, Any]],
    *,
    limit: int = 25,
    clock: _Clock | None = None,
    path: Path | None = None,
) -> tuple[AlphaVantageMarketDataAdapter, FakeTransport]:
    def handler(method: str, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        return HttpResponse(200, json.dumps(responses.pop(0)).encode(), "application/json", {})

    transport = FakeTransport(handler)
    clk = clock or _Clock()
    http = BoundedHttpClient(transport, timeout_seconds=1, max_retries=0, user_agent="t")
    budget = DailyRequestBudget(limit, path=path, clock=clk)
    adapter = AlphaVantageMarketDataAdapter(
        http, api_key=SecretStr(SECRET), budget=budget, cache_ttl=timedelta(hours=12), clock=clk
    )
    return adapter, transport


def test_symbol_mapping() -> None:
    assert alpha_vantage_symbol(apple_listing()) == "AAPL"
    assert alpha_vantage_symbol(reliance_nse_listing()) == "RELIANCE.BSE"


def test_normalizes_and_caches() -> None:
    adapter, transport = _adapter([_daily()])
    first = adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID)
    second = adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID)
    assert first is not None and second is not None
    assert first.bars[-1].close == Decimal("102.5")
    assert first.provider_name == "alpha_vantage_daily"
    assert transport.calls == 1


def test_budget_blocks_calls_and_resets_next_day(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "budget.json"
    adapter, transport = _adapter([_daily(), _daily()], limit=1, clock=clock, path=path)
    assert adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID) is not None
    assert adapter.get_ohlcv_series(reliance_nse_listing(), company_id=APPLE_COMPANY_ID) is None
    assert transport.calls == 1
    assert json.loads(path.read_text())["used"] == 1
    # persisted budget survives a restart
    assert DailyRequestBudget(1, path=path, clock=clock).remaining == 0
    clock.now += timedelta(days=1)
    assert DailyRequestBudget(1, path=path, clock=clock).remaining == 1


def test_rate_limit_note_exhausts_budget() -> None:
    adapter, transport = _adapter([{"Information": "25 requests per day"}], limit=25)
    assert adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID) is None
    assert adapter.get_ohlcv_series(reliance_nse_listing(), company_id=APPLE_COMPANY_ID) is None
    assert transport.calls == 1


def test_nse_listing_labelled_as_bse_proxy() -> None:
    adapter, _ = _adapter([_daily()])
    series = adapter.get_ohlcv_series(reliance_nse_listing(), company_id=APPLE_COMPANY_ID)
    assert series is not None
    assert "bse_proxy" in series.provider_name


def test_settings_defaults_and_key_not_in_log_context() -> None:
    s = Settings(_env_file=None, APP_ENV="test", ALPHA_VANTAGE_API_KEY=SECRET)
    assert s.alpha_vantage_daily_limit == 25
    assert s.alpha_vantage_cache_ttl_seconds == 43_200
    assert SECRET not in json.dumps(s.safe_log_context(), default=str)


def test_composition_wires_alpha_only_with_key(tmp_path: Path) -> None:
    from amrp.composition import _build_alpha_vantage

    budget = str(tmp_path / "b.json")
    no_key = Settings(_env_file=None, APP_ENV="test", ALPHA_VANTAGE_BUDGET_PATH=budget)
    assert _build_alpha_vantage(no_key, None) is None
    keyed = Settings(
        _env_file=None,
        APP_ENV="test",
        ALPHA_VANTAGE_API_KEY=SECRET,
        ALPHA_VANTAGE_BUDGET_PATH=budget,
    )
    assert isinstance(_build_alpha_vantage(keyed, None), AlphaVantageMarketDataAdapter)
