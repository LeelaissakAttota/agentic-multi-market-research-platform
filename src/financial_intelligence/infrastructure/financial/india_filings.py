"""India filing foundation: authority hierarchy, fixture parsing, live announcements.

Fixture payloads remain explicitly labelled fixture/demo. Optional live
acquisition (``INDIA_FILINGS_LIVE_ENABLED``, default off) reads corporate
announcements from the NSE public JSON API (cookie-primed session) and the BSE
announcements API. Only allowlisted hosts are contacted, through the bounded
HTTP client, with per-host request spacing and a TTL cache. Announcements are
exchange disclosures (Tier 1) exposed as company events; numeric financial
statements are not scraped from attachments.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import quote, urlencode

from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.financial import (
    CompanyFinancialPackage,
    FilingFormType,
    FilingId,
    FilingMetadata,
    FinancialConcept,
    FinancialDataAvailability,
    FinancialScale,
    FinancialUnit,
    IncomeStatement,
    PeriodBasis,
    ReportingPeriod,
    build_fact,
)
from financial_intelligence.domain.identity import CompanyId, CompanyIdentity, CurrencyCode
from financial_intelligence.domain.news import QualitativeEvent
from financial_intelligence.domain.sources import SourceAuthorityTier, SourceId
from financial_intelligence.infrastructure.financial.concept_mapping import (
    map_india_results_label,
)
from financial_intelligence.infrastructure.http import (
    BoundedHttpClient,
    HttpFailureKind,
    HttpTransportError,
)
from financial_intelligence.infrastructure.http.feeds import (
    HostRateLimiter,
    TtlCache,
    assert_allowlisted,
    strip_html,
)
from financial_intelligence.infrastructure.market.india_quotes import (
    BSE_SCRIP_CODES,
    BseScripResolver,
)
from financial_intelligence.infrastructure.news.live_events import build_event
from financial_intelligence.observability.logging import get_logger

_filings_logger = get_logger("financial_intelligence.infrastructure.financial.india_filings")


class IndiaFilingAuthority(StrEnum):
    """Authority order for Indian disclosures (DATA_SOURCES.md Tier 1).

    Higher precedence when resolving conflicts among India sources:
    NSE exchange disclosure → BSE → SEBI → company IR.
    """

    NSE = "nse"
    BSE = "bse"
    SEBI = "sebi"
    COMPANY_IR = "company_ir"


INDIA_AUTHORITY_PRECEDENCE: tuple[IndiaFilingAuthority, ...] = (
    IndiaFilingAuthority.NSE,
    IndiaFilingAuthority.BSE,
    IndiaFilingAuthority.SEBI,
    IndiaFilingAuthority.COMPANY_IR,
)


def india_authority_rank(authority: IndiaFilingAuthority) -> int:
    """Lower rank = higher precedence."""

    return INDIA_AUTHORITY_PRECEDENCE.index(authority)


@dataclass(frozen=True, slots=True)
class IndiaResultsRow:
    """One labelled line item from an India results-style fixture payload."""

    label: str
    raw_value: Decimal
    unit: str
    scale: FinancialScale


def parse_india_results_fixture(
    payload: dict[str, Any],
    *,
    company_id: CompanyId,
    clock: datetime | None = None,
) -> CompanyFinancialPackage | None:
    """Deterministically parse an explicitly fixture-labelled India results dict.

    Expected shape (fixture only)::

        {
          "data_origin": "fixture",
          "authority": "nse",
          "currency": "INR",
          "fiscal_year": 2024,
          "period_start": "2023-04-01",
          "period_end": "2024-03-31",
          "form_type": "in_annual_results",
          "accession_or_reference": "...",
          "rows": [{"label": "Revenue from Operations", "value": "100", "scale": 1000000}]
        }
    """

    if payload.get("data_origin") != DataOrigin.FIXTURE.value:
        # Refuse to treat unlabelled / live-looking payloads as fixture success.
        return None
    authority_raw = str(payload.get("authority", "")).strip().lower()
    try:
        authority = IndiaFilingAuthority(authority_raw)
    except ValueError:
        return None
    currency_raw = payload.get("currency")
    if not isinstance(currency_raw, str) or len(currency_raw) != 3:
        return None
    try:
        fiscal_year = int(payload["fiscal_year"])
        period_start = date.fromisoformat(str(payload["period_start"]))
        period_end = date.fromisoformat(str(payload["period_end"]))
    except (KeyError, TypeError, ValueError):
        return None

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None

    retrieved_at = clock or datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)
    period = ReportingPeriod(
        basis=PeriodBasis.FISCAL_YEAR,
        fiscal_year=fiscal_year,
        period_start=period_start,
        period_end=period_end,
        label=f"FY{fiscal_year}",
    )
    currency = CurrencyCode(currency_raw.upper())
    source_id = SourceId.new()
    filing_id = FilingId.new()
    facts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("label")
        if not isinstance(label, str):
            continue
        concept = map_india_results_label(label)
        if concept is None:
            continue
        try:
            raw = Decimal(str(row.get("value")))
            scale_int = int(row.get("scale", 1))
            scale = FinancialScale(scale_int)
        except (InvalidOperation, ValueError, KeyError, TypeError):
            continue
        if raw.is_nan() or raw.is_infinite():
            continue
        facts.append(
            build_fact(
                company_id=company_id,
                concept=concept,
                period=period,
                raw_value=raw,
                unit=FinancialUnit.CURRENCY,
                scale=scale,
                source_id=source_id,
                authority_tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
                retrieved_at=retrieved_at,
                currency=currency,
                filing_id=filing_id,
                provider_concept=label,
            )
        )

    if not facts:
        return None

    income_concepts = {
        FinancialConcept.REVENUE,
        FinancialConcept.NET_INCOME,
        FinancialConcept.GROSS_PROFIT,
        FinancialConcept.OPERATING_INCOME,
        FinancialConcept.COST_OF_REVENUE,
        FinancialConcept.EPS_BASIC,
        FinancialConcept.EPS_DILUTED,
    }
    income_facts = tuple(f for f in facts if f.concept in income_concepts)
    if not income_facts:
        return None

    form_raw = str(payload.get("form_type", FilingFormType.IN_ANNUAL_RESULTS.value))
    try:
        form_type = FilingFormType(form_raw)
    except ValueError:
        form_type = FilingFormType.IN_ANNUAL_RESULTS

    filing = FilingMetadata(
        filing_id=filing_id,
        company_id=company_id,
        form_type=form_type,
        reporting_period=period,
        source_id=source_id,
        authority_tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
        # The fixture payload shape (see module docstring) carries no authoritative
        # disclosure/filing date field — filed_at/published_at must stay None rather
        # than borrow period_end as a stand-in filing date.
        filed_at=None,
        published_at=None,
        retrieved_at=retrieved_at,
        accession_or_reference=(
            str(payload["accession_or_reference"])
            if payload.get("accession_or_reference")
            else None
        ),
        provider_name=f"india_fixture:{authority.value}",
    )
    return CompanyFinancialPackage(
        company_id=company_id,
        reporting_period=period,
        currency=currency,
        retrieved_at=retrieved_at,
        income_statement=IncomeStatement(
            company_id=company_id,
            period=period,
            currency=currency,
            facts=income_facts,
        ),
        filing=filing,
        provider_name=f"india_fixture:{authority.value}",
        availability=FinancialDataAvailability.PARTIAL,
        data_origin=DataOrigin.FIXTURE,
    )


# ---------------------------------------------------------------------------
# Live NSE / BSE corporate announcements (optional, default off)
# ---------------------------------------------------------------------------

NSE_FILINGS_HOST = "www.nseindia.com"
BSE_FILINGS_HOST = "api.bseindia.com"
INDIA_FILINGS_ALLOWED_HOSTS = frozenset({NSE_FILINGS_HOST, BSE_FILINGS_HOST})
_NSE_ANNOUNCEMENTS = f"https://{NSE_FILINGS_HOST}/api/corporate-announcements"
_BSE_ANNOUNCEMENTS = f"https://{BSE_FILINGS_HOST}/BseIndiaAPI/api/AnnSubCategoryGetData/w"
_BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
_IST = timezone(timedelta(hours=5, minutes=30))
_MAX_ANNOUNCEMENTS = 50


def _listing_tickers(company: CompanyIdentity) -> dict[str, str]:
    tickers: dict[str, str] = {}
    for security in company.securities:
        for listing in security.listings:
            tickers.setdefault(listing.exchange.as_text(), listing.ticker.as_text())
    return tickers


def _parse_india_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_IST).astimezone(UTC)
        except ValueError:
            continue
    return None


def _safe_text(value: object, limit: int) -> str:
    return strip_html(str(value), limit=limit) if value not in (None, "") else ""


class IndiaCorporateAnnouncementsSource:
    """CompanyEventSource for NSE (primary) and BSE corporate announcements."""

    name = "india_exchange_announcements"

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        rate_limiter: HostRateLimiter,
        scrip_resolver: BseScripResolver | None = None,
        cache_ttl_seconds: float = 900,
        lookback_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= lookback_days <= 365:
            msg = "lookback_days must be between 1 and 365"
            raise ValueError(msg)
        self._http = http
        self._limiter = rate_limiter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scrips = scrip_resolver
        self._lookback = lookback_days
        self._cache: TtlCache[str, tuple[QualitativeEvent, ...]] = TtlCache(
            cache_ttl_seconds, clock=self._clock
        )
        self._cookies: dict[str, str] = {}
        self._cookies_at: datetime | None = None

    def fetch(self, company: CompanyIdentity) -> tuple[QualitativeEvent, ...]:
        tickers = _listing_tickers(company)
        if not ({"NSE", "BSE"} & tickers.keys()):
            return ()
        key = company.company_id.as_text()
        hit, cached = self._cache.get(key)
        if hit and cached is not None:
            return cached
        events: list[QualitativeEvent] = []
        symbol = tickers.get("NSE") or tickers.get("BSE") or ""
        events.extend(self._fetch_nse(company.company_id, symbol))
        bse_code = self._bse_code(tickers)
        if bse_code is not None:
            events.extend(self._fetch_bse(company.company_id, bse_code))
        result = tuple(events)
        self._cache.put(key, result)
        return result

    # -- NSE --------------------------------------------------------------
    def _prime(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and self._cookies_at and (now - self._cookies_at) < timedelta(minutes=5):
            return
        home = f"https://{NSE_FILINGS_HOST}/"
        assert_allowlisted(home, INDIA_FILINGS_ALLOWED_HOSTS)
        self._limiter.acquire(NSE_FILINGS_HOST)
        response = self._http.get_response(home)
        jar: dict[str, str] = {}
        for line in response.headers.get("set-cookie", "").splitlines():
            cookie: SimpleCookie = SimpleCookie()
            try:
                cookie.load(line)
            except CookieError:
                continue
            jar.update({name: morsel.value for name, morsel in cookie.items()})
        self._cookies, self._cookies_at = jar, now

    def _nse_get(self, url: str) -> object:
        headers = {
            "Referer": f"https://{NSE_FILINGS_HOST}/companies-listing/corporate-filings-announcements",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(self._cookies.items()))
        self._limiter.acquire(NSE_FILINGS_HOST)
        response = self._http.get_response(url, accept="application/json", extra_headers=headers)
        return json.loads(response.body.decode("utf-8"))

    def _fetch_nse(self, company_id: CompanyId, symbol: str) -> list[QualitativeEvent]:
        if not symbol:
            return []
        url = f"{_NSE_ANNOUNCEMENTS}?{urlencode({'index': 'equities', 'symbol': symbol})}"
        try:
            assert_allowlisted(url, INDIA_FILINGS_ALLOWED_HOSTS)
            self._prime()
            try:
                rows = self._nse_get(url)
            except HttpTransportError as exc:
                if exc.kind is not HttpFailureKind.UNAUTHORIZED:
                    raise
                self._prime(force=True)
                rows = self._nse_get(url)
        except HttpTransportError as exc:
            _filings_logger.info("nse_announcements_unavailable", extra={"kind": exc.kind.value})
            return []
        except (UnicodeDecodeError, ValueError):
            _filings_logger.info("nse_announcements_invalid_payload")
            return []
        if isinstance(rows, dict):
            rows = rows.get("data", [])
        if not isinstance(rows, list):
            return []
        events: list[QualitativeEvent] = []
        retrieved = self._clock()
        floor = retrieved - timedelta(days=self._lookback)
        for row in rows[:_MAX_ANNOUNCEMENTS]:
            if not isinstance(row, dict):
                continue
            title = _safe_text(row.get("desc"), 256)
            detail = _safe_text(row.get("attchmntText"), 2000)
            published = _parse_india_datetime(row.get("an_dt") or row.get("sort_date"))
            if not title or (published is not None and published < floor):
                continue
            attachment = row.get("attchmntFile")
            url_value = (
                attachment
                if isinstance(attachment, str) and attachment.startswith("https://")
                else None
            )
            event = build_event(
                company_id=company_id,
                title=title,
                summary=detail or title,
                published=published,
                retrieved_at=retrieved,
                url=url_value,
                tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
                provider_name="nse_corporate_announcements",
                locator=f"nse:announcement:{row.get('seq_id') or symbol}",
                jurisdiction="IN",
            )
            if event is not None:
                events.append(event)
        return events

    # -- BSE --------------------------------------------------------------
    def _bse_code(self, tickers: dict[str, str]) -> str | None:
        ticker = tickers.get("BSE") or tickers.get("NSE")
        if not ticker:
            return None
        if self._scrips is not None:
            return self._scrips.resolve(ticker)
        return BSE_SCRIP_CODES.get(ticker.upper())

    def _fetch_bse(self, company_id: CompanyId, scrip: str) -> list[QualitativeEvent]:
        retrieved = self._clock()
        start = (retrieved - timedelta(days=self._lookback)).astimezone(_IST)
        query = {
            "pageno": "1",
            "strCat": "-1",
            "strPrevDate": start.strftime("%Y%m%d"),
            "strScrip": scrip,
            "strSearch": "P",
            "strToDate": retrieved.astimezone(_IST).strftime("%Y%m%d"),
            "strType": "C",
            "subcategory": "-1",
        }
        url = f"{_BSE_ANNOUNCEMENTS}?{urlencode(query)}"
        try:
            assert_allowlisted(url, INDIA_FILINGS_ALLOWED_HOSTS)
            self._limiter.acquire(BSE_FILINGS_HOST)
            payload = self._http.get_json(
                url,
                extra_headers={
                    "Referer": "https://www.bseindia.com/",
                    "Origin": "https://www.bseindia.com",
                },
            )
        except HttpTransportError as exc:
            _filings_logger.info("bse_announcements_unavailable", extra={"kind": exc.kind.value})
            return []
        rows = payload.get("Table")
        if not isinstance(rows, list):
            return []
        events: list[QualitativeEvent] = []
        for row in rows[:_MAX_ANNOUNCEMENTS]:
            if not isinstance(row, dict):
                continue
            title = _safe_text(row.get("NEWSSUB") or row.get("HEADLINE"), 256)
            if not title:
                continue
            attachment = row.get("ATTACHMENTNAME")
            url_value = (
                f"{_BSE_ATTACHMENT_BASE}{quote(attachment)}"
                if isinstance(attachment, str) and attachment.strip()
                else None
            )
            event = build_event(
                company_id=company_id,
                title=title,
                summary=_safe_text(row.get("HEADLINE"), 2000) or title,
                published=_parse_india_datetime(row.get("NEWS_DT") or row.get("DT_TM")),
                retrieved_at=retrieved,
                url=url_value,
                tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
                provider_name="bse_corporate_announcements",
                locator=f"bse:announcement:{row.get('NEWSID') or scrip}",
                jurisdiction="IN",
            )
            if event is not None:
                events.append(event)
        return events
