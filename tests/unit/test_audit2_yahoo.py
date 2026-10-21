from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpResponse
from financial_intelligence.infrastructure.market import yahoo_chart
from financial_intelligence.infrastructure.market.yahoo_chart import YahooChartMarketDataAdapter
from tests.unit.market_fixtures import APPLE_COMPANY_ID, apple_listing
from tests.unit.test_market_live import FakeTransport

_DAY = 86_400
_START = 1_754_323_200


def _payload(n: int, *, ragged: bool = False, price: float = 10.0) -> dict[str, Any]:
    ts = [_START + i * _DAY for i in range(n)]
    closes = [price + i for i in range(n)]
    quote = {
        "open": list(closes),
        "high": list(closes),
        "low": list(closes),
        "close": list(closes),
        "volume": [1000] * n,
    }
    if ragged:
        quote["volume"] = quote["volume"][:-2]
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [quote]}}]}}


def _adapter(payload: dict[str, Any], history_days: int = 30) -> YahooChartMarketDataAdapter:
    def handler(method: str, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        return HttpResponse(200, json.dumps(payload).encode(), "application/json", {})

    http = BoundedHttpClient(
        FakeTransport(handler), timeout_seconds=1, max_retries=0, user_agent="t"
    )
    return YahooChartMarketDataAdapter(
        http, history_days=history_days, clock=lambda: datetime(2026, 10, 14, tzinfo=UTC)
    )


def _fetch(adapter: YahooChartMarketDataAdapter):  # type: ignore[no-untyped-def]
    return adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID)


def test_ragged_arrays_do_not_crash_and_keep_aligned_rows() -> None:
    series = _fetch(_adapter(_payload(5, ragged=True)))
    assert series is not None
    assert len(series.bars) == 3


def test_max_bars_keeps_newest_sessions() -> None:
    n = yahoo_chart._MAX_BARS + 30
    series = _fetch(_adapter(_payload(n), history_days=365))
    assert series is not None
    assert len(series.bars) == yahoo_chart._MAX_BARS
    newest = datetime.fromtimestamp(_START + (n - 1) * _DAY, tz=UTC).date()
    assert series.bars[-1].session_date == newest


def test_float32_noise_is_rounded() -> None:
    from decimal import Decimal

    series = _fetch(_adapter(_payload(2, price=1311.800048828125)))
    assert series is not None
    assert series.bars[0].close == Decimal("1311.8")
    assert str(series.bars[0].high) == "1311.8"
