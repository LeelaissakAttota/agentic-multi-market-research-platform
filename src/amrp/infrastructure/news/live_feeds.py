"""Optional free live news sources: GDELT DOC 2.0 API and RSS feeds.

* ``GdeltDocSource`` queries ``api.gdeltproject.org`` (article list, JSON).
* ``GoogleNewsRssSource`` queries ``news.google.com/rss/search`` with the
  India or US edition depending on the company's country.
* ``RssFeedSource`` reads operator-configured company IR RSS/Atom feeds; only
  the configured hosts are allowlisted.

News is secondary coverage: items are Tier 3 (reputable publisher) or Tier 4
(everything else) and are never classified as FACT. All requests go through
the bounded HTTP client, are spaced per host (GDELT asks for one request every
five seconds), and are cached per company.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from amrp.domain.identity import CompanyIdentity
from amrp.domain.news import QualitativeEvent
from amrp.domain.sources import SourceAuthorityTier
from amrp.infrastructure.http import BoundedHttpClient, HttpTransportError
from amrp.infrastructure.http.feeds import (
    FeedItem,
    HostRateLimiter,
    TtlCache,
    assert_allowlisted,
    parse_feed,
)
from amrp.infrastructure.news.live_events import build_event
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.news.live_feeds")

GDELT_HOST = "api.gdeltproject.org"
GOOGLE_NEWS_HOST = "news.google.com"
_GDELT_DOC = f"https://{GDELT_HOST}/api/v2/doc/doc"
_GOOGLE_RSS = f"https://{GOOGLE_NEWS_HOST}/rss/search"
_MAX_ITEMS = 25

#: Publishers treated as Tier 3 (reputable news); all others are Tier 4.
REPUTABLE_NEWS_DOMAINS = frozenset(
    {
        "reuters.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
        "cnbc.com",
        "apnews.com",
        "economictimes.indiatimes.com",
        "livemint.com",
        "business-standard.com",
        "thehindubusinessline.com",
        "moneycontrol.com",
        "financialexpress.com",
        "marketwatch.com",
        "barrons.com",
    }
)
_REPUTABLE_NAMES = (
    "reuters",
    "bloomberg",
    "wall street journal",
    "financial times",
    "cnbc",
    "associated press",
    "economic times",
    "mint",
    "business standard",
    "hindu businessline",
    "moneycontrol",
    "financial express",
    "marketwatch",
    "barron",
)


def _domain_tier(domain: str | None, publisher: str | None = None) -> SourceAuthorityTier:
    host = (domain or "").casefold().removeprefix("www.")
    if any(host == d or host.endswith(f".{d}") for d in REPUTABLE_NEWS_DOMAINS):
        return SourceAuthorityTier.TIER_3_REPUTABLE_NEWS
    name = (publisher or "").casefold()
    if name and any(marker in name for marker in _REPUTABLE_NAMES):
        return SourceAuthorityTier.TIER_3_REPUTABLE_NEWS
    return SourceAuthorityTier.TIER_4_GENERAL_WEB


def _query_name(company: CompanyIdentity) -> str:
    return company.display_name.replace('"', "").strip()


def _is_us_or_india(company: CompanyIdentity) -> bool:
    return company.country.as_text() in {"IN", "US"}


class _CachedSource:
    name = "live_news"

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        rate_limiter: HostRateLimiter,
        cache_ttl_seconds: float,
        lookback_days: int,
        clock: Callable[[], datetime] | None,
    ) -> None:
        if not 1 <= lookback_days <= 90:
            msg = "lookback_days must be between 1 and 90"
            raise ValueError(msg)
        self._http = http
        self._limiter = rate_limiter
        self._lookback = lookback_days
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: TtlCache[str, tuple[QualitativeEvent, ...]] = TtlCache(
            cache_ttl_seconds, clock=self._clock
        )

    def fetch(self, company: CompanyIdentity) -> tuple[QualitativeEvent, ...]:
        if not _is_us_or_india(company):
            return ()
        key = company.company_id.as_text()
        hit, cached = self._cache.get(key)
        if hit and cached is not None:
            return cached
        try:
            events = tuple(self._fetch(company))
        except HttpTransportError as exc:
            logger.info(
                "live_news_unavailable", extra={"source": self.name, "kind": exc.kind.value}
            )
            events = ()
        except (UnicodeDecodeError, ValueError) as exc:
            logger.info("live_news_invalid", extra={"source": self.name, "error": str(exc)[:200]})
            events = ()
        self._cache.put(key, events)
        return events

    def _fetch(self, company: CompanyIdentity) -> list[QualitativeEvent]:
        raise NotImplementedError

    def _within_window(self, published: datetime | None) -> bool:
        return published is None or published >= self._clock() - timedelta(days=self._lookback)

    def _feed_events(
        self, company: CompanyIdentity, items: Sequence[FeedItem], provider: str
    ) -> list[QualitativeEvent]:
        retrieved = self._clock()
        events: list[QualitativeEvent] = []
        for item in items[:_MAX_ITEMS]:
            if not self._within_window(item.published):
                continue
            domain = urlsplit(item.link).hostname if item.link else None
            event = build_event(
                company_id=company.company_id,
                title=item.title,
                summary=item.summary or item.title,
                published=item.published,
                retrieved_at=retrieved,
                url=item.link,
                tier=_domain_tier(domain if provider != "google_news_rss" else None, item.source),
                provider_name=f"{provider}:{item.source}" if item.source else provider,
                locator=f"{provider}:{(item.link or item.title)[:200]}",
                jurisdiction=company.country.as_text(),
            )
            if event is not None:
                events.append(event)
        return events


class GdeltDocSource(_CachedSource):
    """GDELT DOC 2.0 article list for the company name (country-scoped)."""

    name = "gdelt_doc"

    def _fetch(self, company: CompanyIdentity) -> list[QualitativeEvent]:
        country = company.country.as_text()
        query = f'"{_query_name(company)}" sourcecountry:{"IN" if country == "IN" else "US"}'
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(_MAX_ITEMS),
            "timespan": f"{self._lookback}d",
            "sort": "DateDesc",
        }
        url = f"{_GDELT_DOC}?{urlencode(params)}"
        assert_allowlisted(url, frozenset({GDELT_HOST}))
        self._limiter.acquire(GDELT_HOST)
        response = self._http.get_response(url, accept="application/json")
        text = response.body.decode("utf-8").strip()
        if not text:
            return []
        payload = json.loads(text)
        articles = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(articles, list):
            return []
        retrieved = self._clock()
        events: list[QualitativeEvent] = []
        for article in articles[:_MAX_ITEMS]:
            if not isinstance(article, dict):
                continue
            title = str(article.get("title") or "").strip()
            link = article.get("url")
            if not title or not isinstance(link, str) or not link.startswith("http"):
                continue
            published = _parse_gdelt_date(article.get("seendate"))
            if not self._within_window(published):
                continue
            domain = str(article.get("domain") or urlsplit(link).hostname or "")
            event = build_event(
                company_id=company.company_id,
                title=title,
                summary=title,
                published=published,
                retrieved_at=retrieved,
                url=link,
                tier=_domain_tier(domain),
                provider_name=f"gdelt_doc:{domain}" if domain else "gdelt_doc",
                locator=f"gdelt:{link[:240]}",
                jurisdiction=country,
            )
            if event is not None:
                events.append(event)
        return events


def _parse_gdelt_date(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class GoogleNewsRssSource(_CachedSource):
    """Google News RSS search, India or US edition by company country."""

    name = "google_news_rss"

    def _fetch(self, company: CompanyIdentity) -> list[QualitativeEvent]:
        india = company.country.as_text() == "IN"
        params = {
            "q": f'"{_query_name(company)}" when:{self._lookback}d',
            "hl": "en-IN" if india else "en-US",
            "gl": "IN" if india else "US",
            "ceid": "IN:en" if india else "US:en",
        }
        url = f"{_GOOGLE_RSS}?{urlencode(params)}"
        assert_allowlisted(url, frozenset({GOOGLE_NEWS_HOST}))
        self._limiter.acquire(GOOGLE_NEWS_HOST)
        response = self._http.get_response(url, accept="application/rss+xml, application/xml;q=0.9")
        return self._feed_events(company, parse_feed(response.body), self.name)


class RssFeedSource(_CachedSource):
    """Operator-configured company IR RSS/Atom feeds keyed by ticker."""

    name = "company_ir_rss"

    def __init__(
        self,
        http: BoundedHttpClient,
        feeds: Mapping[str, str],
        *,
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
        self._feeds = {k.upper(): v for k, v in feeds.items()}
        self._hosts = frozenset(urlsplit(v).hostname or "" for v in self._feeds.values()) - {""}

    def _fetch(self, company: CompanyIdentity) -> list[QualitativeEvent]:
        tickers = {listing.ticker.as_text().upper() for listing in company.all_listings()}
        events: list[QualitativeEvent] = []
        for ticker in sorted(tickers & self._feeds.keys()):
            url = self._feeds[ticker]
            host = assert_allowlisted(url, self._hosts)
            self._limiter.acquire(host)
            response = self._http.get_response(url, accept="application/rss+xml, */*;q=0.5")
            for event in self._feed_events(company, parse_feed(response.body), "company_ir_rss"):
                events.append(event)
        return events


def parse_ir_feed_setting(raw: str) -> dict[str, str]:
    """Parse ``TICKER=https://host/feed,TICKER2=...``; https only, invalid pairs dropped."""

    feeds: dict[str, str] = {}
    for pair in raw.split(","):
        ticker, sep, url = pair.strip().partition("=")
        url = url.strip()
        if sep and ticker.strip() and url.startswith("https://") and urlsplit(url).hostname:
            feeds[ticker.strip().upper()] = url
    return feeds
