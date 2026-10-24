"""Optional Finnhub (free tier) source for US company news and analyst data.

Used only when ``FINNHUB_API_KEY`` is configured. Three free endpoints:

* ``/company-news``           -> recent headlines (Tier 3/4 by publisher);
* ``/stock/recommendation``   -> latest analyst buy/hold/sell counts;
* ``/calendar/earnings``      -> next scheduled earnings date.

Analyst consensus and the earnings date are emitted as qualitative events
(``provider_name`` ``finnhub:recommendation`` / ``finnhub:earnings``) so they
flow through the normal evidence pipeline and carry evidence tags in reports.
They are third-party data, never classified as FACT. The key is sent as a
header (``X-Finnhub-Token``) and never logged or embedded in locators/URLs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from pydantic import SecretStr

from financial_intelligence.domain.identity import CompanyIdentity
from financial_intelligence.domain.news import EventType, QualitativeEvent
from financial_intelligence.domain.sources import SourceAuthorityTier
from financial_intelligence.infrastructure.http import BoundedHttpClient
from financial_intelligence.infrastructure.http.feeds import HostRateLimiter, assert_allowlisted
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from financial_intelligence.infrastructure.news.live_events import build_event
from financial_intelligence.infrastructure.news.live_feeds import _CachedSource, _domain_tier

FINNHUB_HOST = "finnhub.io"
_BASE = f"https://{FINNHUB_HOST}/api/v1"
_MAX_NEWS = 15
RECOMMENDATION_PROVIDER = "finnhub:recommendation"
EARNINGS_PROVIDER = "finnhub:earnings"


@dataclass(frozen=True, slots=True)
class AnalystRecommendation:
    period: str
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int

    @property
    def total(self) -> int:
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def consensus(self) -> str:
        if not self.total:
            return "No rating"
        bull, bear = self.strong_buy + self.buy, self.sell + self.strong_sell
        if bull > max(self.hold, bear):
            return "Buy"
        if bear > max(self.hold, bull):
            return "Sell"
        return "Hold"

    def headline(self) -> str:
        return (
            f"Analyst consensus: {self.consensus} - {self.strong_buy} strong buy, {self.buy} buy, "
            f"{self.hold} hold, {self.sell} sell, {self.strong_sell} strong sell "
            f"({self.total} analysts, {self.period})"
        )


def us_ticker(company: CompanyIdentity) -> str | None:
    """Primary US-listed ticker, if any (Finnhub free tier covers US symbols)."""

    if company.country.as_text() != "US":
        return None
    listings = [lst for sec in company.securities for lst in sec.listings]
    listings.sort(key=lambda lst: not lst.is_primary)
    for listing in listings:
        return listing.ticker.as_text()
    return None


def parse_recommendation(payload: object) -> AnalystRecommendation | None:
    if not isinstance(payload, list) or not payload:
        return None
    rows = [r for r in payload if isinstance(r, dict) and isinstance(r.get("period"), str)]
    if not rows:
        return None
    row = max(rows, key=lambda r: str(r["period"]))

    def n(key: str) -> int:
        try:
            return max(0, int(row.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    rec = AnalystRecommendation(
        str(row["period"]), n("strongBuy"), n("buy"), n("hold"), n("sell"), n("strongSell")
    )
    return rec if rec.total else None


def parse_next_earnings(payload: object, today: str) -> dict[str, object] | None:
    rows = payload.get("earningsCalendar") if isinstance(payload, dict) else None
    candidates = rows if isinstance(rows, list) else []
    upcoming = [
        r
        for r in candidates
        if isinstance(r, dict) and isinstance(r.get("date"), str) and r["date"] >= today
    ]
    return min(upcoming, key=lambda r: str(r["date"])) if upcoming else None


class FinnhubSource(_CachedSource):
    """CompanyEventSource for US companies: news + analyst consensus + next earnings."""

    name = "finnhub"

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        api_key: SecretStr,
        rate_limiter: HostRateLimiter,
        cache_ttl_seconds: float,
        lookback_days: int,
        clock: Callable[[], datetime] | None,
    ) -> None:
        super().__init__(
            http,
            rate_limiter=rate_limiter,
            cache_ttl_seconds=cache_ttl_seconds,
            lookback_days=lookback_days,
            clock=clock,
        )
        self._key = api_key
        if api_key.get_secret_value():
            PROVIDER_HEALTH.register(self.name)

    def _get(self, path: str, params: dict[str, str]) -> object:
        url = f"{_BASE}{path}?{urlencode(params)}"
        assert_allowlisted(url, frozenset({FINNHUB_HOST}))
        self._limiter.acquire(FINNHUB_HOST)
        try:
            response = self._http.get_response(
                url,
                accept="application/json",
                extra_headers={"X-Finnhub-Token": self._key.get_secret_value()},
            )
            payload = json.loads(response.body.decode("utf-8") or "null")
        except Exception as exc:
            kind = getattr(getattr(exc, "kind", None), "value", None) or type(exc).__name__
            PROVIDER_HEALTH.record_failure(self.name, str(kind))
            raise
        PROVIDER_HEALTH.record_success(self.name)
        return payload

    def _fetch(self, company: CompanyIdentity) -> list[QualitativeEvent]:
        symbol = us_ticker(company)
        if symbol is None or not self._key.get_secret_value():
            return []
        now = self._clock()
        today = now.date().isoformat()
        start = (now - timedelta(days=self._lookback)).date().isoformat()
        events: list[QualitativeEvent] = []

        def add(event: QualitativeEvent | None) -> None:
            if event is not None:
                events.append(event)

        rec = parse_recommendation(self._get("/stock/recommendation", {"symbol": symbol}))
        if rec is not None:
            add(
                build_event(
                    company_id=company.company_id,
                    title=rec.headline(),
                    summary=rec.headline(),
                    published=now,
                    retrieved_at=now,
                    url=f"https://finnhub.io/docs/api/recommendation-trends#{symbol}",
                    tier=SourceAuthorityTier.TIER_3_REPUTABLE_NEWS,
                    provider_name=RECOMMENDATION_PROVIDER,
                    locator=f"finnhub:recommendation:{symbol}:{rec.period}",
                    event_type=EventType.OTHER,
                    jurisdiction="US",
                )
            )
        horizon = (now + timedelta(days=120)).date().isoformat()
        nxt = parse_next_earnings(
            self._get("/calendar/earnings", {"symbol": symbol, "from": today, "to": horizon}),
            today,
        )
        if nxt is not None:
            eps = nxt.get("epsEstimate")
            title = f"Next earnings report scheduled {nxt['date']}" + (
                f" (EPS estimate {eps})" if isinstance(eps, (int, float)) else ""
            )
            add(
                build_event(
                    company_id=company.company_id,
                    title=title,
                    summary=title,
                    published=now,
                    retrieved_at=now,
                    url=f"https://finnhub.io/docs/api/earnings-calendar#{symbol}",
                    tier=SourceAuthorityTier.TIER_3_REPUTABLE_NEWS,
                    provider_name=EARNINGS_PROVIDER,
                    locator=f"finnhub:earnings:{symbol}:{nxt['date']}",
                    event_type=EventType.EARNINGS,
                    jurisdiction="US",
                )
            )
        news = self._get("/company-news", {"symbol": symbol, "from": start, "to": today})
        for item in (news if isinstance(news, list) else [])[:_MAX_NEWS]:
            if not isinstance(item, dict):
                continue
            title, link = str(item.get("headline") or "").strip(), item.get("url")
            if not title or not isinstance(link, str) or not link.startswith("http"):
                continue
            try:
                published = datetime.fromtimestamp(int(item.get("datetime") or 0), tz=UTC)
            except (TypeError, ValueError, OSError):
                published = None
            if not self._within_window(published):
                continue
            source = str(item.get("source") or "").strip()
            add(
                build_event(
                    company_id=company.company_id,
                    title=title,
                    summary=str(item.get("summary") or title).strip() or title,
                    published=published,
                    retrieved_at=now,
                    url=link,
                    tier=_domain_tier(None, source),
                    provider_name=f"finnhub:{source}" if source else "finnhub",
                    locator=f"finnhub:news:{item.get('id') or link[:200]}",
                    jurisdiction="US",
                )
            )
        return events
