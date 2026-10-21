"""Optional NSE/BSE live quote overlay for Indian listings (default off).

* NSE public quote JSON (``www.nseindia.com/api/quote-equity``) is tried first.
  NSE requires browser-like headers and session cookies, so the client primes
  cookies from the NSE home page, sends them back, and re-primes once on
  401/403.
* BSE (``api.bseindia.com``) is the fallback, using a static scrip-code map.
* The overlay wraps a base ``MarketDataPort`` (Yahoo live chart with fixture
  fallback in composition). The latest exchange quote replaces or appends the
  current IST session bar; when both exchanges fail the base series is
  returned untouched (Yahoo fallback). Nothing is fabricated.

Only allowlisted hosts are contacted, every request goes through the existing
``BoundedHttpClient`` (timeouts, size caps, bounded retries), requests to each
host are spaced by a minimum interval, and quotes are cached for a short TTL.
Automated tests inject fake transports; CI never calls NSE or BSE.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.cookies import CookieError, SimpleCookie
from threading import Lock
from urllib.parse import urlencode

from financial_intelligence.application.ports import MarketDataPort
from financial_intelligence.domain.identity import CompanyId, ListingIdentity
from financial_intelligence.domain.market import (
    DataOrigin,
    MarketDataAvailability,
    MarketObservationSeries,
    OhlcvBar,
)
from financial_intelligence.domain.sources import SourceId
from financial_intelligence.infrastructure.http import (
    BoundedHttpClient,
    HttpFailureKind,
    HttpTransportError,
)
from financial_intelligence.infrastructure.http.feeds import HostRateLimiter, assert_allowlisted
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from financial_intelligence.observability.logging import get_logger

__all__ = [
    "ALLOWED_HOSTS",
    "BROWSER_USER_AGENT",
    "BseScripResolver",
    "ExchangeQuote",
    "HostRateLimiter",
    "IndiaExchangeQuoteClient",
    "IndiaLiveQuoteMarketDataAdapter",
]

logger = get_logger("financial_intelligence.infrastructure.market.india_quotes")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

NSE_HOST = "www.nseindia.com"
BSE_HOST = "api.bseindia.com"
ALLOWED_HOSTS = frozenset({NSE_HOST, BSE_HOST})

_NSE_HOME = f"https://{NSE_HOST}/"
_NSE_QUOTE = f"https://{NSE_HOST}/api/quote-equity"
_BSE_QUOTE = f"https://{BSE_HOST}/BseIndiaAPI/api/getScripHeaderData/w"

#: Browser-like User-Agent required by NSE/BSE edge protection.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

#: Seed BSE scrip codes (ticker -> scrip code); others resolve dynamically.
BSE_SCRIP_CODES: dict[str, str] = {
    "RELIANCE": "500325",
    "TCS": "532540",
    "INFY": "500209",
    "HDFCBANK": "500180",
}

_BSE_SCRIP_LIST = f"https://{BSE_HOST}/BseIndiaAPI/api/ListofScripData/w"
_BSE_TRADING = f"https://{BSE_HOST}/BseIndiaAPI/api/StockTrading/w"
_BSE_HEADERS = {
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept-Language": "en-US,en;q=0.9",
}
_VOLUME_UNITS = {"": 1, "lakh": 100_000, "lakhs": 100_000, "cr": 10_000_000, "crore": 10_000_000}
_INDIAN_EXCHANGES = frozenset({"NSE", "BSE"})
_COOKIE_TTL = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class ExchangeQuote:
    """Normalized intraday quote from an Indian exchange."""

    provider_name: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    last: Decimal
    volume: Decimal
    as_of: datetime


def _assert_allowlisted(url: str) -> None:
    assert_allowlisted(url, ALLOWED_HOSTS)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        msg = "missing numeric value"
        raise ValueError(msg)
    text = str(value).replace(",", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        msg = f"invalid numeric value: {text!r}"
        raise ValueError(msg) from exc
    if not number.is_finite() or number < 0:
        msg = "numeric value must be finite and non-negative"
        raise ValueError(msg)
    return number


def _volume(value: object) -> Decimal:
    try:
        return _decimal(value).to_integral_value()
    except ValueError:
        return Decimal("0")


def _scaled_volume(raw: object, multiplier: int) -> Decimal:
    try:
        return (_decimal(raw) * multiplier).to_integral_value()
    except ValueError:
        return Decimal("0")


def _as_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"missing {name} object"
        raise ValueError(msg)
    return value


_IST_FORMATS = (
    "%d-%b-%Y %H:%M:%S",  # NSE metadata.lastUpdateTime
    "%d %b %Y | %H:%M",
    "%d %b %y | %H:%M",  # BSE Header.Ason, e.g. "14 Oct 26 | 16:00"
    "%d %b %y | %I:%M %p",
)


def _parse_ist(text: object, fallback: datetime) -> datetime:
    """Parse an exchange IST timestamp; never return a time after ``fallback``."""

    if isinstance(text, str):
        for fmt in _IST_FORMATS:
            try:
                parsed = datetime.strptime(text.strip(), fmt).replace(tzinfo=IST).astimezone(UTC)
            except ValueError:
                continue
            return min(parsed, fallback)
    return fallback


class BseScripResolver:
    """Resolve NSE-style tickers / ISINs to BSE scrip codes.

    Order: static seed map, then the BSE active equity scrip list
    (``ListofScripData``), matched by ISIN when known and otherwise by BSE
    scrip id. The list is fetched at most once per ``list_ttl`` (default 24h);
    misses are cached too so unknown tickers never hammer BSE.
    """

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        rate_limiter: HostRateLimiter,
        list_ttl: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] | None = None,
        seed: dict[str, str] | None = None,
    ) -> None:
        self._http = http
        PROVIDER_HEALTH.register("nse_quote_equity")
        PROVIDER_HEALTH.register("bse_scrip_header")
        self._limiter = rate_limiter
        self._list_ttl = list_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._seed = dict(BSE_SCRIP_CODES if seed is None else seed)
        self._by_id: dict[str, str] = {}
        self._by_isin: dict[str, str] = {}
        self._loaded_at: datetime | None = None
        self._lock = Lock()

    def resolve(self, ticker: str, *, isin: str | None = None) -> str | None:
        key = ticker.upper()
        if key in self._seed:
            return self._seed[key]
        with self._lock:
            self._ensure_list()
            if isin and isin.upper() in self._by_isin:
                return self._by_isin[isin.upper()]
            return self._by_id.get(key)

    def _ensure_list(self) -> None:
        now = self._clock()
        if self._loaded_at is not None and now - self._loaded_at < self._list_ttl:
            return
        self._loaded_at = now  # also throttles retries after a failure
        query = {"Group": "", "Scripcode": "", "industry": "", "segment": "Equity"}
        url = f"{_BSE_SCRIP_LIST}?{urlencode({**query, 'status': 'Active'})}"
        try:
            _assert_allowlisted(url)
            self._limiter.acquire(BSE_HOST)
            response = self._http.get_response(
                url, accept="application/json", extra_headers=_BSE_HEADERS
            )
            rows = json.loads(response.body.decode("utf-8"))
        except HttpTransportError as exc:
            logger.info("bse_scrip_list_unavailable", extra={"failure_kind": exc.kind.value})
            return
        except (UnicodeDecodeError, ValueError):
            logger.info("bse_scrip_list_invalid_payload")
            return
        if not isinstance(rows, list):
            return
        by_id: dict[str, str] = {}
        by_isin: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("SCRIP_CD") or "").strip()
            if not code.isdigit():
                continue
            scrip_id = str(row.get("scrip_id") or "").strip().upper()
            isin_value = str(row.get("ISIN_NUMBER") or "").strip().upper()
            if scrip_id:
                by_id.setdefault(scrip_id, code)
            if isin_value:
                by_isin.setdefault(isin_value, code)
        self._by_id, self._by_isin = by_id, by_isin


class IndiaExchangeQuoteClient:
    """Fetch NSE quotes (cookie-primed) with BSE fallback, cached and rate limited."""

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        rate_limiter: HostRateLimiter,
        cache_ttl: timedelta = timedelta(seconds=15),
        clock: Callable[[], datetime] | None = None,
        scrip_resolver: BseScripResolver | None = None,
    ) -> None:
        self._http = http
        self._limiter = rate_limiter
        self._cache_ttl = cache_ttl
        self._isins: dict[str, str] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cookies: dict[str, str] = {}
        self._cookies_at: datetime | None = None
        self._cache: dict[str, tuple[datetime, ExchangeQuote | None]] = {}
        # Per-ticker locks: concurrent requests for different tickers proceed in
        # parallel (host spacing is still enforced by the rate limiter).
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._cookie_lock = Lock()
        self._scrips = scrip_resolver or BseScripResolver(
            http, rate_limiter=rate_limiter, clock=self._clock
        )

    # -- public -----------------------------------------------------------
    def get_quote(self, ticker: str) -> ExchangeQuote | None:
        key = ticker.upper()
        now = self._clock()
        with self._ticker_lock(key):
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self._cache_ttl:
                return cached[1]
            quote = self._fetch_nse(key)
            if quote is None:
                quote = self._fetch_bse(key)
            self._cache[key] = (now, quote)
            return quote

    def _ticker_lock(self, key: str) -> Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = Lock()
            return lock

    # -- NSE --------------------------------------------------------------
    def _nse_headers(self) -> dict[str, str]:
        headers = {
            "Referer": f"https://{NSE_HOST}/get-quotes/equity",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(self._cookies.items()))
        return headers

    def _prime_nse_cookies(self, *, force: bool = False) -> None:
        with self._cookie_lock:
            self._prime_nse_cookies_locked(force=force)

    def _prime_nse_cookies_locked(self, *, force: bool) -> None:
        now = self._clock()
        if not force and self._cookies_at is not None and now - self._cookies_at < _COOKIE_TTL:
            return
        _assert_allowlisted(_NSE_HOME)
        self._limiter.acquire(NSE_HOST)
        response = self._http.get_response(
            _NSE_HOME, extra_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        cookies: dict[str, str] = {}
        for line in response.headers.get("set-cookie", "").splitlines():
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(line)
            except CookieError:
                continue
            cookies.update({name: morsel.value for name, morsel in jar.items()})
        self._cookies = cookies
        self._cookies_at = now

    def _fetch_nse(self, ticker: str) -> ExchangeQuote | None:
        url = f"{_NSE_QUOTE}?{urlencode({'symbol': ticker})}"
        try:
            _assert_allowlisted(url)
            self._prime_nse_cookies()
            try:
                self._limiter.acquire(NSE_HOST)
                payload = self._http.get_json(url, extra_headers=self._nse_headers())
            except HttpTransportError as exc:
                if exc.kind is not HttpFailureKind.UNAUTHORIZED:
                    raise
                self._prime_nse_cookies(force=True)
                self._limiter.acquire(NSE_HOST)
                payload = self._http.get_json(url, extra_headers=self._nse_headers())
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("isin"), str):
                self._isins[ticker] = str(info["isin"]).upper()
            quote = self._normalize_nse(payload)
            PROVIDER_HEALTH.record_success("nse_quote_equity")
            return replace(quote, volume=self._nse_volume(ticker))
        except HttpTransportError as exc:
            PROVIDER_HEALTH.record_failure("nse_quote_equity", exc.kind.value, exc.status_code)
            self._log("nse_quote_unavailable", ticker, exc.kind.value, exc.status_code)
        except (TypeError, ValueError, KeyError) as exc:
            PROVIDER_HEALTH.record_failure("nse_quote_equity", "invalid_payload")
            self._log("nse_quote_invalid_payload", ticker, type(exc).__name__, None)
        return None

    def _normalize_nse(self, payload: dict[str, object]) -> ExchangeQuote:
        price = _as_dict(payload.get("priceInfo"), "priceInfo")
        hilo = _as_dict(price.get("intraDayHighLow"), "intraDayHighLow")
        meta = payload.get("metadata")
        as_of = _parse_ist(
            meta.get("lastUpdateTime") if isinstance(meta, dict) else None, self._clock()
        )
        # Day volume comes from the separate trade_info section (see _nse_volume);
        # preOpenMarket.totalTradedVolume is only the pre-open auction quantity.
        volume = Decimal("0")
        return self._build(
            "nse_quote_equity",
            price.get("open"),
            hilo.get("max"),
            hilo.get("min"),
            price.get("lastPrice"),
            volume,
            as_of,
        )

    def _nse_volume(self, ticker: str) -> Decimal:
        """Best-effort day traded quantity from NSE ``section=trade_info`` (0 if absent)."""

        url = f"{_NSE_QUOTE}?{urlencode({'symbol': ticker, 'section': 'trade_info'})}"
        try:
            _assert_allowlisted(url)
            self._limiter.acquire(NSE_HOST)
            payload = self._http.get_json(url, extra_headers=self._nse_headers())
        except HttpTransportError:
            return Decimal("0")
        dp = payload.get("securityWiseDP")
        if isinstance(dp, dict) and dp.get("quantityTraded") is not None:
            return _volume(dp.get("quantityTraded"))
        book = payload.get("marketDeptOrderBook")
        info = book.get("tradeInfo") if isinstance(book, dict) else None
        if isinstance(info, dict):
            # tradeInfo.totalTradedVolume is reported in lakhs of shares.
            return _scaled_volume(info.get("totalTradedVolume"), _VOLUME_UNITS["lakh"])
        return Decimal("0")

    # -- BSE --------------------------------------------------------------
    def _fetch_bse(self, ticker: str) -> ExchangeQuote | None:
        scrip = self._scrips.resolve(ticker, isin=self._isins.get(ticker))
        if scrip is None:
            return None
        url = f"{_BSE_QUOTE}?{urlencode({'Debtflag': '', 'scripcode': scrip, 'seriesid': ''})}"
        try:
            _assert_allowlisted(url)
            self._limiter.acquire(BSE_HOST)
            payload = self._http.get_json(url, extra_headers=_BSE_HEADERS)
            quote = self._normalize_bse(payload)
            PROVIDER_HEALTH.record_success("bse_scrip_header")
            return replace(quote, volume=self._bse_volume(scrip))
        except HttpTransportError as exc:
            PROVIDER_HEALTH.record_failure("bse_scrip_header", exc.kind.value, exc.status_code)
            self._log("bse_quote_unavailable", ticker, exc.kind.value, exc.status_code)
        except (TypeError, ValueError, KeyError) as exc:
            PROVIDER_HEALTH.record_failure("bse_scrip_header", "invalid_payload")
            self._log("bse_quote_invalid_payload", ticker, type(exc).__name__, None)
        return None

    def _normalize_bse(self, payload: dict[str, object]) -> ExchangeQuote:
        header = _as_dict(payload.get("Header"), "Header")
        current = payload.get("CurrRate")
        last = current.get("LTP") if isinstance(current, dict) else None
        if last in (None, ""):
            last = header.get("LTP")
        as_of = _parse_ist(header.get("Ason"), self._clock())
        return self._build(
            "bse_scrip_header",
            header.get("Open"),
            header.get("High"),
            header.get("Low"),
            last,
            Decimal("0"),
            as_of,
        )

    def _bse_volume(self, scrip: str) -> Decimal:
        """Best-effort total traded quantity from BSE ``StockTrading`` (0 if absent)."""

        url = f"{_BSE_TRADING}?{urlencode({'flag': '', 'quotetype': 'EQ', 'scripcode': scrip})}"
        try:
            _assert_allowlisted(url)
            self._limiter.acquire(BSE_HOST)
            payload = self._http.get_json(url, extra_headers=_BSE_HEADERS)
        except HttpTransportError:
            return Decimal("0")
        raw = payload.get("TTQ")
        unit = str(payload.get("TTQin") or "").strip().lower()
        multiplier = _VOLUME_UNITS.get(unit)
        if multiplier is None:
            return Decimal("0")
        return _scaled_volume(raw, multiplier)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _build(
        provider: str,
        open_v: object,
        high_v: object,
        low_v: object,
        last_v: object,
        volume: Decimal,
        as_of: datetime,
    ) -> ExchangeQuote:
        last = _decimal(last_v)
        open_ = _decimal(open_v)
        high = max(_decimal(high_v), open_, last)
        low = min(_decimal(low_v), open_, last)
        if last == 0 or low == 0:
            msg = "zero price in quote"
            raise ValueError(msg)
        return ExchangeQuote(
            provider_name=provider,
            session_date=as_of.astimezone(IST).date(),
            open=open_,
            high=high,
            low=low,
            last=last,
            volume=volume,
            as_of=as_of,
        )

    @staticmethod
    def _log(event: str, ticker: str, kind: str, status: int | None) -> None:
        logger.info(event, extra={"ticker": ticker, "failure_kind": kind, "status_code": status})


class IndiaLiveQuoteMarketDataAdapter:
    """MarketDataPort overlay: latest NSE/BSE quote on top of a base series."""

    def __init__(
        self,
        base: MarketDataPort,
        quotes: IndiaExchangeQuoteClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._base = base
        self._quotes = quotes
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_ohlcv_series(
        self,
        listing: ListingIdentity,
        *,
        company_id: CompanyId,
    ) -> MarketObservationSeries | None:
        base = self._base.get_ohlcv_series(listing, company_id=company_id)
        if listing.exchange.as_text() not in _INDIAN_EXCHANGES:
            return base
        if listing.currency.as_text() != "INR":
            return base
        try:
            quote = self._quotes.get_quote(listing.ticker.as_text())
        except Exception as exc:  # never let the overlay break the base path
            logger.warning("india_quote_failure", extra={"error_type": type(exc).__name__})
            return base
        if quote is None:
            return base
        try:
            return self._merge(base, quote, listing=listing, company_id=company_id)
        except ValueError as exc:
            logger.info("india_quote_rejected", extra={"error": str(exc)[:200]})
            return base

    def _merge(
        self,
        base: MarketObservationSeries | None,
        quote: ExchangeQuote,
        *,
        listing: ListingIdentity,
        company_id: CompanyId,
    ) -> MarketObservationSeries:
        live_bar = OhlcvBar(
            session_date=quote.session_date,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.last,
            volume=quote.volume,
            currency=listing.currency,
        )
        usable_base = (
            base is not None
            and base.availability is MarketDataAvailability.AVAILABLE
            and bool(base.bars)
            # Never splice a real quote onto sample/fixture history and call it live.
            and base.data_origin in (DataOrigin.LIVE, DataOrigin.CACHED_LIVE)
        )
        if base is not None and usable_base:
            if base.bars[-1].session_date > quote.session_date:
                msg = "exchange quote is older than base series"
                raise ValueError(msg)
            same_day = [b for b in base.bars if b.session_date == quote.session_date]
            if live_bar.volume == 0 and same_day and same_day[-1].volume > 0:
                # Exchange volume unavailable (e.g. BSE StockTrading blocked):
                # keep the live base provider's same-session volume.
                live_bar = replace(live_bar, volume=same_day[-1].volume)
            history = tuple(b for b in base.bars if b.session_date != quote.session_date)
            return replace(
                base,
                bars=(*history, live_bar),
                as_of=max(base.as_of, quote.as_of),
                retrieved_at=self._clock(),
                source_id=SourceId.new(),
                provider_name=f"{quote.provider_name}+{base.provider_name}",
                data_origin=DataOrigin.LIVE,
            )
        return MarketObservationSeries(
            company_id=company_id,
            security_id=listing.security_id,
            listing_id=listing.listing_id,
            exchange=listing.exchange,
            ticker=listing.ticker,
            currency=listing.currency,
            as_of=quote.as_of,
            retrieved_at=self._clock(),
            source_id=SourceId.new(),
            bars=(live_bar,),
            provider_name=quote.provider_name,
            availability=MarketDataAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )
