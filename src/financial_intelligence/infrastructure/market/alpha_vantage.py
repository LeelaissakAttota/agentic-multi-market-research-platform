"""Optional Alpha Vantage daily OHLCV adapter (second live market source).

Used only when ``ALPHA_VANTAGE_API_KEY`` is configured. The free tier allows
25 requests per day, so this adapter:

* caches every series in memory for ``cache_ttl`` (default 12h),
* keeps a per-UTC-day request budget persisted on disk (default 25),
* stops calling for the rest of the day when Alpha Vantage reports a
  rate-limit/"Information"/"Note" response.

Coverage: US listings (bare ticker) and Indian listings via the ``.BSE``
suffix (Alpha Vantage does not serve NSE; NSE listings use the BSE price as a
clearly-labelled proxy). The API key is never logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode

from pydantic import SecretStr

from financial_intelligence.domain.identity import CompanyId, ListingIdentity
from financial_intelligence.domain.market import (
    DataOrigin,
    MarketDataAvailability,
    MarketObservationSeries,
    OhlcvBar,
)
from financial_intelligence.domain.sources import SourceId
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpTransportError
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from financial_intelligence.observability.logging import get_logger

logger = get_logger("financial_intelligence.infrastructure.market.alpha_vantage")

ALPHA_VANTAGE_HOST = "www.alphavantage.co"
_QUERY_URL = f"https://{ALPHA_VANTAGE_HOST}/query"
_MAX_BARS = 100
_LIMIT_KEYS = ("Information", "Note")


def alpha_vantage_symbol(listing: ListingIdentity) -> str:
    """Map a canonical listing to an Alpha Vantage symbol."""

    ticker = listing.ticker.as_text()
    if listing.exchange.as_text() in {"NSE", "BSE"}:
        return f"{ticker}.BSE"
    return ticker


class DailyRequestBudget:
    """Per-UTC-day request counter, optionally persisted to a JSON file."""

    def __init__(
        self,
        limit: int,
        *,
        path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if limit < 0:
            msg = "limit must be non-negative"
            raise ValueError(msg)
        self._limit = limit
        self._path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = Lock()
        self._day, self._used = self._load()

    def _today(self) -> str:
        return self._clock().astimezone(UTC).date().isoformat()

    def _load(self) -> tuple[str, int]:
        if self._path is not None:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("used"), int):
                    return str(raw.get("day", "")), int(raw["used"])
            except (OSError, ValueError):
                pass
        return self._today(), 0

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"day": self._day, "used": self._used}), "utf-8")
        except OSError:
            logger.info("alpha_vantage_budget_not_persisted")

    def try_consume(self) -> bool:
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self._limit:
                return False
            self._used += 1
            self._save()
            return True

    def exhaust(self) -> None:
        with self._lock:
            self._day, self._used = self._today(), self._limit
            self._save()

    @property
    def remaining(self) -> int:
        with self._lock:
            if self._today() != self._day:
                return self._limit
            return max(0, self._limit - self._used)


class AlphaVantageMarketDataAdapter:
    """Live MarketDataPort adapter for Alpha Vantage TIME_SERIES_DAILY."""

    provider_name = "alpha_vantage_daily"

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        api_key: SecretStr,
        budget: DailyRequestBudget,
        cache_ttl: timedelta = timedelta(hours=12),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key.get_secret_value():
            msg = "Alpha Vantage adapter requires an API key"
            raise ValueError(msg)
        self._http = http
        PROVIDER_HEALTH.register(self.provider_name)
        self._api_key = api_key
        self._budget = budget
        self._cache_ttl = cache_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[str, tuple[datetime, tuple[tuple[OhlcvBar, ...], datetime] | None]] = {}
        self._lock = Lock()

    def get_ohlcv_series(
        self,
        listing: ListingIdentity,
        *,
        company_id: CompanyId,
    ) -> MarketObservationSeries | None:
        symbol = alpha_vantage_symbol(listing)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None and now - cached[0] < self._cache_ttl:
                result = cached[1]
            else:
                result = self._fetch(symbol, listing)
                if result is not None or self._budget.remaining > 0:
                    self._cache[symbol] = (now, result)
        if result is None:
            return None
        bars, as_of = result
        provider = self.provider_name
        if listing.exchange.as_text() == "NSE":
            provider = f"{self.provider_name}(bse_proxy)"
        return MarketObservationSeries(
            company_id=company_id,
            security_id=listing.security_id,
            listing_id=listing.listing_id,
            exchange=listing.exchange,
            ticker=listing.ticker,
            currency=listing.currency,
            as_of=as_of,
            retrieved_at=now,
            source_id=SourceId.new(),
            bars=bars,
            provider_name=provider,
            availability=MarketDataAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )

    def _fetch(
        self, symbol: str, listing: ListingIdentity
    ) -> tuple[tuple[OhlcvBar, ...], datetime] | None:
        if not self._budget.try_consume():
            PROVIDER_HEALTH.record_failure(AlphaVantageMarketDataAdapter.provider_name, "budget")
            logger.info("alpha_vantage_daily_budget_exhausted", extra={"symbol": symbol})
            return None
        query = {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "compact",
            "apikey": self._api_key.get_secret_value(),
        }
        try:
            payload = self._http.get_json(f"{_QUERY_URL}?{urlencode(query)}")
        except HttpTransportError as exc:
            PROVIDER_HEALTH.record_failure(self.provider_name, exc.kind.value, exc.status_code)
            logger.info(
                "alpha_vantage_unavailable",
                extra={"symbol": symbol, "failure_kind": exc.kind.value},
            )
            return None
        if any(key in payload for key in _LIMIT_KEYS):
            PROVIDER_HEALTH.record_failure(self.provider_name, "rate_limited")
            logger.info("alpha_vantage_rate_limited", extra={"symbol": symbol})
            self._budget.exhaust()
            return None
        try:
            result = self._normalize(payload, listing)
        except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
            PROVIDER_HEALTH.record_failure(self.provider_name, "invalid_payload")
            logger.info(
                "alpha_vantage_invalid_payload",
                extra={"symbol": symbol, "error_type": type(exc).__name__},
            )
            return None
        PROVIDER_HEALTH.record_success(self.provider_name)
        return result

    @staticmethod
    def _normalize(
        payload: dict[str, object], listing: ListingIdentity
    ) -> tuple[tuple[OhlcvBar, ...], datetime]:
        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            msg = "missing daily series"
            raise ValueError(msg)
        bars: list[OhlcvBar] = []
        for day_text in sorted(series)[-_MAX_BARS:]:
            row = series[day_text]
            if not isinstance(row, dict):
                continue
            bars.append(
                OhlcvBar(
                    session_date=date.fromisoformat(day_text),
                    open=Decimal(str(row["1. open"])),
                    high=Decimal(str(row["2. high"])),
                    low=Decimal(str(row["3. low"])),
                    close=Decimal(str(row["4. close"])),
                    volume=Decimal(str(row["5. volume"])).to_integral_value(),
                    currency=listing.currency,
                )
            )
        if not bars:
            msg = "no usable bars"
            raise ValueError(msg)
        last = bars[-1].session_date
        return tuple(bars), datetime.combine(last, datetime.min.time(), tzinfo=UTC)
