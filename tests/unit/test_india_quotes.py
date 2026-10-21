"""Offline tests for the optional NSE/BSE live quote overlay (mocked transport)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest import TestCase

from financial_intelligence.config.settings import Settings
from financial_intelligence.domain.identity import CompanyId
from financial_intelligence.domain.market import (
    DataOrigin,
    MarketDataAvailability,
    MarketObservationSeries,
    OhlcvBar,
)
from financial_intelligence.domain.sources import SourceId
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpResponse
from financial_intelligence.infrastructure.market import InMemoryMarketDataAdapter
from financial_intelligence.infrastructure.market.india_quotes import (
    ALLOWED_HOSTS,
    BROWSER_USER_AGENT,
    HostRateLimiter,
    IndiaExchangeQuoteClient,
    IndiaLiveQuoteMarketDataAdapter,
)
from tests.unit.market_fixtures import (
    APPLE_COMPANY_ID,
    apple_listing,
    reliance_bse_listing,
    reliance_nse_listing,
)

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)  # 11:30 IST
RELIANCE_ID = CompanyId.from_string("22222222-2222-4222-8222-222222222099")

Handler = Callable[[str, dict[str, str]], HttpResponse]


def _json(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        body=json.dumps(payload).encode(),
        content_type="application/json",
        headers={},
    )


def _nse_payload(last: float = 2950.5) -> dict[str, object]:
    return {
        "info": {"symbol": "RELIANCE"},
        "metadata": {"lastUpdateTime": "14-Oct-2026 11:29:58"},
        "priceInfo": {
            "lastPrice": last,
            "open": 2930.0,
            "previousClose": 2925.0,
            "intraDayHighLow": {"min": 2921.1, "max": 2961.0},
        },
        "preOpenMarket": {"totalTradedVolume": 123456},
    }


def _bse_payload() -> dict[str, object]:
    return {
        "Header": {"Open": "2,931.00", "High": "2,960.00", "Low": "2,920.00", "PrevClose": "2925"},
        "CurrRate": {"LTP": "2,949.75"},
    }


class FakeTransport:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, str]]] = []

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
        self.calls.append((url, dict(headers)))
        return self.handler(url, headers)


def _home(cookie: str = "nsit=abc; Path=/\nnseappid=xyz; Path=/") -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=b"<html></html>",
        content_type="text/html",
        headers={"set-cookie": cookie},
    )


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _client(
    transport: FakeTransport, clock: _Clock | None = None, sleeps: list[float] | None = None
) -> IndiaExchangeQuoteClient:
    http = BoundedHttpClient(
        transport, timeout_seconds=2.0, max_retries=0, user_agent=BROWSER_USER_AGENT
    )
    monotonic = iter(float(i) * 0.1 for i in range(10_000))
    limiter = HostRateLimiter(
        1.0,
        monotonic=lambda: next(monotonic),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )
    return IndiaExchangeQuoteClient(
        http, rate_limiter=limiter, cache_ttl=timedelta(seconds=15), clock=clock or _Clock()
    )


class NseVolumeTests(TestCase):
    def test_pre_open_volume_is_not_used_as_day_volume(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if url.endswith(".com/"):
                return _home()
            if "section=trade_info" in url:
                return _json({"marketDeptOrderBook": {"tradeInfo": {"totalTradedVolume": 42.5}}})
            return _json(_nse_payload())

        quote = _client(FakeTransport(handler)).get_quote("RELIANCE")
        assert quote is not None
        self.assertEqual(quote.volume, Decimal("4250000"))  # 42.5 lakh shares

    def test_trade_info_failure_gives_zero_volume_not_preopen(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if url.endswith(".com/"):
                return _home()
            if "section=trade_info" in url:
                return _json({}, status=404)
            return _json(_nse_payload())

        quote = _client(FakeTransport(handler)).get_quote("RELIANCE")
        assert quote is not None
        self.assertEqual(quote.volume, Decimal("0"))


class NseQuoteTests(TestCase):
    def test_primes_cookies_sends_headers_and_normalizes(self) -> None:
        def handler(url: str, headers: dict[str, str]) -> HttpResponse:
            if url == "https://www.nseindia.com/":
                return _home()
            self.assertIn("/api/quote-equity?symbol=RELIANCE", url)
            if "section=trade_info" in url:
                return _json({"securityWiseDP": {"quantityTraded": 123456}})
            self.assertEqual(headers["Cookie"], "nseappid=xyz; nsit=abc")
            self.assertIn("nseindia.com", headers["Referer"])
            self.assertEqual(headers["User-Agent"], BROWSER_USER_AGENT)
            return _json(_nse_payload())

        transport = FakeTransport(handler)
        quote = _client(transport).get_quote("reliance")
        assert quote is not None
        self.assertEqual(quote.provider_name, "nse_quote_equity")
        self.assertEqual(quote.last, Decimal("2950.5"))
        self.assertEqual(quote.high, Decimal("2961.0"))
        self.assertEqual(quote.volume, Decimal("123456"))
        self.assertEqual(quote.session_date, date(2026, 10, 14))
        self.assertEqual(quote.as_of, datetime(2026, 10, 14, 5, 59, 58, tzinfo=UTC))
        self.assertEqual(len(transport.calls), 3)  # home + quote + trade_info

    def test_reprimes_once_on_403(self) -> None:
        state = {"quote_calls": 0, "home_calls": 0}

        def handler(url: str, headers: dict[str, str]) -> HttpResponse:
            if url.endswith("nseindia.com/"):
                state["home_calls"] += 1
                return _home(f"nsit=v{state['home_calls']}; Path=/")
            state["quote_calls"] += 1
            if state["quote_calls"] == 1:
                return _json({"error": "forbidden"}, status=403)
            self.assertEqual(headers["Cookie"], "nsit=v2")
            return _json(_nse_payload())

        quote = _client(FakeTransport(handler)).get_quote("RELIANCE")
        self.assertIsNotNone(quote)
        self.assertEqual(state, {"quote_calls": 3, "home_calls": 2})  # + trade_info

    def test_cache_and_rate_limit(self) -> None:
        clock = _Clock()
        sleeps: list[float] = []

        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            return _home() if url.endswith(".com/") else _json(_nse_payload())

        transport = FakeTransport(handler)
        client = _client(transport, clock, sleeps)
        client.get_quote("RELIANCE")
        client.get_quote("RELIANCE")
        self.assertEqual(len(transport.calls), 3)  # second call served from cache
        self.assertTrue(sleeps and all(0 < s <= 1.0 for s in sleeps))  # spacing enforced
        clock.now = NOW + timedelta(seconds=16)
        client.get_quote("RELIANCE")
        self.assertEqual(len(transport.calls), 5)  # cache expired; cookies still fresh


class BseFallbackTests(TestCase):
    def test_bse_used_when_nse_fails(self) -> None:
        def handler(url: str, headers: dict[str, str]) -> HttpResponse:
            if "nseindia" in url:
                return _json({}, status=503)
            self.assertIn("api.bseindia.com", url)
            self.assertIn("scripcode=500325", url)
            self.assertEqual(headers["Referer"], "https://www.bseindia.com/")
            return _json(_bse_payload())

        quote = _client(FakeTransport(handler)).get_quote("RELIANCE")
        assert quote is not None
        self.assertEqual(quote.provider_name, "bse_scrip_header")
        self.assertEqual(quote.last, Decimal("2949.75"))

    def test_unknown_scrip_and_invalid_payload_return_none(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if url.endswith(".com/"):
                return _home()
            return _json({"priceInfo": "garbage"})

        transport = FakeTransport(handler)
        self.assertIsNone(_client(transport).get_quote("NOTLISTED"))
        self.assertTrue(all(any(h in u for h in ALLOWED_HOSTS) for u, _ in transport.calls))


class OverlayTests(TestCase):
    def _adapter(self, handler: Handler) -> IndiaLiveQuoteMarketDataAdapter:
        return IndiaLiveQuoteMarketDataAdapter(
            InMemoryMarketDataAdapter(), _client(FakeTransport(handler)), clock=_Clock()
        )

    def test_fixture_base_is_not_mixed_with_live_quote(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            return _home() if url.endswith(".com/") else _json(_nse_payload())

        listing = reliance_nse_listing()
        series = self._adapter(handler).get_ohlcv_series(listing, company_id=RELIANCE_ID)
        assert series is not None
        self.assertEqual(series.bars[-1].close, Decimal("2950.5"))
        self.assertEqual(series.bars[-1].session_date, date(2026, 10, 14))
        self.assertEqual(series.data_origin, DataOrigin.LIVE)
        self.assertEqual(series.provider_name, "nse_quote_equity")
        self.assertEqual(len(series.bars), 1)

    def test_live_bar_appended_to_live_base_series(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            return _home() if url.endswith(".com/") else _json(_nse_payload())

        listing = reliance_nse_listing()
        live_base = MarketObservationSeries(
            company_id=RELIANCE_ID,
            security_id=listing.security_id,
            listing_id=listing.listing_id,
            exchange=listing.exchange,
            ticker=listing.ticker,
            currency=listing.currency,
            as_of=datetime(2026, 10, 13, 10, 0, tzinfo=UTC),
            retrieved_at=datetime(2026, 10, 13, 10, 0, tzinfo=UTC),
            source_id=SourceId.new(),
            bars=(
                OhlcvBar(
                    session_date=date(2026, 10, 13),
                    open=Decimal("2900"),
                    high=Decimal("2910"),
                    low=Decimal("2890"),
                    close=Decimal("2905"),
                    volume=Decimal("100"),
                    currency=listing.currency,
                ),
            ),
            provider_name="yahoo",
            availability=MarketDataAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )

        class _LiveBase:
            def get_ohlcv_series(self, _listing: object, *, company_id: object) -> object:
                return live_base

        adapter = IndiaLiveQuoteMarketDataAdapter(
            _LiveBase(),  # type: ignore[arg-type]
            _client(FakeTransport(handler)),
            clock=_Clock(),
        )
        series = adapter.get_ohlcv_series(listing, company_id=RELIANCE_ID)
        assert series is not None
        self.assertEqual(series.provider_name, "nse_quote_equity+yahoo")
        self.assertEqual(series.bars[-1].close, Decimal("2950.5"))
        self.assertGreater(len(series.bars), 1)

    def test_falls_back_to_base_when_exchanges_fail(self) -> None:
        def handler(_url: str, _h: dict[str, str]) -> HttpResponse:
            return _json({}, status=503)

        listing = reliance_bse_listing()
        base = InMemoryMarketDataAdapter().get_ohlcv_series(listing, company_id=RELIANCE_ID)
        series = self._adapter(handler).get_ohlcv_series(listing, company_id=RELIANCE_ID)
        self.assertEqual(series, base)

    def test_non_indian_listing_untouched_and_no_network(self) -> None:
        def handler(_url: str, _h: dict[str, str]) -> HttpResponse:
            raise AssertionError("no exchange call expected")

        adapter = self._adapter(handler)
        series = adapter.get_ohlcv_series(apple_listing(), company_id=APPLE_COMPANY_ID)
        base = InMemoryMarketDataAdapter().get_ohlcv_series(
            apple_listing(), company_id=APPLE_COMPANY_ID
        )
        self.assertEqual(series, base)


class SettingsTests(TestCase):
    def test_disabled_by_default(self) -> None:
        settings = Settings(_env_file=None, APP_ENV="test")  # type: ignore[call-arg]
        self.assertFalse(settings.india_live_quotes_enabled)
        self.assertEqual(settings.india_quotes_min_interval_seconds, 1.0)

    def test_min_interval_floor(self) -> None:
        with self.assertRaises(ValueError):
            Settings(  # type: ignore[call-arg]
                _env_file=None, APP_ENV="test", INDIA_QUOTES_MIN_INTERVAL_SECONDS="0"
            )

    def test_container_builds_with_feature_enabled_without_network(self) -> None:
        from financial_intelligence.composition import build_container

        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, APP_ENV="test", INDIA_LIVE_QUOTES_ENABLED="true"
        )
        container = build_container(settings)
        self.assertTrue(container.settings.india_live_quotes_enabled)


class SameDayVolumeTests(TestCase):
    def test_zero_exchange_volume_keeps_live_base_same_day_volume(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if url.endswith(".com/"):
                return _home()
            if "section=trade_info" in url:
                return _json({}, status=404)
            return _json(_nse_payload())

        listing = reliance_nse_listing()
        base = MarketObservationSeries(
            company_id=RELIANCE_ID,
            security_id=listing.security_id,
            listing_id=listing.listing_id,
            exchange=listing.exchange,
            ticker=listing.ticker,
            currency=listing.currency,
            as_of=datetime(2026, 10, 14, 5, 0, tzinfo=UTC),
            retrieved_at=datetime(2026, 10, 14, 5, 0, tzinfo=UTC),
            source_id=SourceId.new(),
            bars=(
                OhlcvBar(
                    session_date=date(2026, 10, 14),
                    open=Decimal("2900"),
                    high=Decimal("2990"),
                    low=Decimal("2890"),
                    close=Decimal("2905"),
                    volume=Decimal("777"),
                    currency=listing.currency,
                ),
            ),
            provider_name="yahoo",
            availability=MarketDataAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )

        class _Base:
            def get_ohlcv_series(self, _listing: object, *, company_id: object) -> object:
                return base

        adapter = IndiaLiveQuoteMarketDataAdapter(
            _Base(),  # type: ignore[arg-type]
            _client(FakeTransport(handler)),
            clock=_Clock(),
        )
        series = adapter.get_ohlcv_series(listing, company_id=RELIANCE_ID)
        assert series is not None
        self.assertEqual(series.bars[-1].close, Decimal("2950.5"))
        self.assertEqual(series.bars[-1].volume, Decimal("777"))
