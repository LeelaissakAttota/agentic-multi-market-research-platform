"""Optional live regulatory feeds: SEC litigation/admin RSS and SEBI RSS.

* SEC: litigation releases and administrative proceedings RSS on
  ``www.sec.gov`` (requests carry a declared User-Agent per SEC fair access).
* SEBI: the public ``sebirss.xml`` feed on ``www.sebi.gov.in``; only
  enforcement items (orders, adjudication, settlement, show-cause) are kept.

Feeds are market-wide, so they are fetched once per TTL and matched to a
company by its legal/display names and aliases (whole-phrase, corporate
suffixes removed). Matches are Tier-1 official records; nothing is inferred
beyond the headline. When no live match exists the sample package is used.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from financial_intelligence.application.ports import CompanyCatalogPort, RegulatoryEventPort
from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.identity import CompanyId, CompanyIdentity
from financial_intelligence.domain.news import EventEvidenceRef, InformationClass
from financial_intelligence.domain.regulatory import (
    CompanyRegulatoryPackage,
    RegulatorCode,
    RegulatoryActionType,
    RegulatoryAvailability,
    RegulatoryEvent,
    RegulatoryStatus,
)
from financial_intelligence.domain.sources import SourceAuthorityTier, SourceId
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpTransportError
from financial_intelligence.infrastructure.http.feeds import (
    FeedItem,
    HostRateLimiter,
    TtlCache,
    assert_allowlisted,
    parse_feed,
)
from financial_intelligence.observability.logging import get_logger

logger = get_logger("financial_intelligence.infrastructure.regulatory.live_feeds")

SEC_HOST = "www.sec.gov"
SEBI_HOST = "www.sebi.gov.in"
SEC_FEEDS = (
    f"https://{SEC_HOST}/rss/litigation/litreleases.xml",
    f"https://{SEC_HOST}/rss/litigation/admin.xml",
)
SEBI_FEED = f"https://{SEBI_HOST}/sebirss.xml"
_SEBI_ENFORCEMENT_MARKERS = (
    "/enforcement/",
    "order",
    "adjudicat",
    "settlement",
    "show cause",
    "penalty",
)
_SUFFIXES = re.compile(
    r"\b(limited|ltd|inc|incorporated|corp|corporation|co|company|plc|llc|holdings)\b\.?",
)
_NON_WORD = re.compile(r"[^0-9a-z]+")


def _normalize(text: str) -> str:
    return f" {_NON_WORD.sub(' ', text.casefold()).strip()} "


def company_name_patterns(company: CompanyIdentity) -> tuple[str, ...]:
    """Distinctive normalized name phrases (>= 2 words or >= 5 chars)."""

    names = {company.legal_name, company.display_name, *(a.value for a in company.aliases)}
    patterns: set[str] = set()
    for name in names:
        core = _normalize(_SUFFIXES.sub(" ", name.casefold())).strip()
        if len(core.split()) >= 2 or len(core) >= 5:
            patterns.add(f" {core} ")
    return tuple(sorted(patterns))


def _action_type(text: str) -> RegulatoryActionType:
    lowered = text.casefold()
    if "penalt" in lowered or re.search(r"\bfine[ds]?\b", lowered):
        return RegulatoryActionType.PENALTY
    if "settle" in lowered or "order" in lowered or "judgment" in lowered:
        return RegulatoryActionType.ORDER
    if "show cause" in lowered or "notice" in lowered:
        return RegulatoryActionType.NOTICE
    if "withdraw" in lowered:
        return RegulatoryActionType.WITHDRAWAL
    return RegulatoryActionType.OTHER


@dataclass(frozen=True, slots=True)
class RegulatoryFeedItem:
    regulator: RegulatorCode
    jurisdiction: str
    feed_url: str
    item: FeedItem


class RegulatoryFeedSource(Protocol):
    name: str

    def items(self) -> Sequence[RegulatoryFeedItem]:
        """Return current feed items (empty when unavailable). Must not raise."""


class _FeedSource:
    name = "regulatory_feed"
    regulator = RegulatorCode.OTHER
    jurisdiction = "US"
    feeds: tuple[str, ...] = ()
    hosts: frozenset[str] = frozenset()

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        rate_limiter: HostRateLimiter,
        cache_ttl_seconds: float = 1800,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._limiter = rate_limiter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: TtlCache[str, tuple[RegulatoryFeedItem, ...]] = TtlCache(
            cache_ttl_seconds, clock=self._clock
        )

    def items(self) -> tuple[RegulatoryFeedItem, ...]:
        collected: list[RegulatoryFeedItem] = []
        for url in self.feeds:
            hit, cached = self._cache.get(url)
            if not hit or cached is None:
                cached = self._load(url)
                self._cache.put(url, cached)
            collected.extend(cached)
        return tuple(collected)

    def _load(self, url: str) -> tuple[RegulatoryFeedItem, ...]:
        try:
            host = assert_allowlisted(url, self.hosts)
            self._limiter.acquire(host)
            response = self._http.get_response(url, accept="application/rss+xml, */*;q=0.5")
            parsed = parse_feed(response.body, max_items=200)
        except HttpTransportError as exc:
            logger.info("regulatory_feed_unavailable", extra={"url": url, "kind": exc.kind.value})
            return ()
        except ValueError as exc:
            logger.info("regulatory_feed_invalid", extra={"url": url, "error": str(exc)[:200]})
            return ()
        return tuple(
            RegulatoryFeedItem(self.regulator, self.jurisdiction, url, item)
            for item in parsed
            if self._keep(item)
        )

    def _keep(self, item: FeedItem) -> bool:
        return True


class SecLitigationFeedSource(_FeedSource):
    """SEC litigation releases + administrative proceedings RSS."""

    name = "sec_litigation_rss"
    regulator = RegulatorCode.SEC
    jurisdiction = "US"
    feeds = SEC_FEEDS
    hosts = frozenset({SEC_HOST})


class SebiOrdersFeedSource(_FeedSource):
    """SEBI public RSS filtered to enforcement items (orders etc.)."""

    name = "sebi_orders_rss"
    regulator = RegulatorCode.SEBI
    jurisdiction = "IN"
    feeds = (SEBI_FEED,)
    hosts = frozenset({SEBI_HOST})

    def _keep(self, item: FeedItem) -> bool:
        text = f"{item.link or ''} {item.title}".casefold()
        return any(marker in text for marker in _SEBI_ENFORCEMENT_MARKERS)


class LiveRegulatoryAdapter:
    """RegulatoryEventPort matching market-wide regulator feeds to a company."""

    def __init__(
        self,
        catalog: CompanyCatalogPort,
        sources: Sequence[RegulatoryFeedSource],
        *,
        lookback_days: int = 365,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalog = catalog
        self._sources = tuple(sources)
        self._lookback = lookback_days
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_regulatory_package(self, company_id: CompanyId) -> CompanyRegulatoryPackage | None:
        company = self._catalog.get_by_id(company_id)
        if company is None:
            return None
        patterns = company_name_patterns(company)
        if not patterns:
            return None
        retrieved = self._clock()
        floor = retrieved - timedelta(days=self._lookback)
        events: list[RegulatoryEvent] = []
        for source in self._sources:
            try:
                items = source.items()
            except Exception as exc:
                logger.warning(
                    "regulatory_source_failure",
                    extra={"source": source.name, "error_type": type(exc).__name__},
                )
                continue
            for feed_item in items:
                item = feed_item.item
                haystack = _normalize(f"{item.title} {item.summary}")
                if not any(pattern in haystack for pattern in patterns):
                    continue
                if item.published is not None and item.published < floor:
                    continue
                event = self._event(company_id, feed_item, source.name, retrieved)
                if event is not None:
                    events.append(event)
        if not events:
            return None
        return CompanyRegulatoryPackage(
            company_id=company_id,
            retrieved_at=retrieved,
            events=tuple(events),
            provider_name="+".join(s.name for s in self._sources),
            availability=RegulatoryAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )

    @staticmethod
    def _event(
        company_id: CompanyId,
        feed_item: RegulatoryFeedItem,
        provider: str,
        retrieved: datetime,
    ) -> RegulatoryEvent | None:
        item = feed_item.item
        when = min(item.published or retrieved, retrieved).date()
        identity = item.link or f"{feed_item.feed_url}#{item.title}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        case_ref = None
        match = re.search(r"\b(LR-\d+|\d{2,4}-\d{3,6})\b", item.title)
        if match:
            case_ref = match.group(1)
        try:
            return RegulatoryEvent(
                event_id=f"{feed_item.regulator.value.lower()}-{digest}",
                company_id=company_id,
                regulator=feed_item.regulator,
                jurisdiction=feed_item.jurisdiction,
                action_type=_action_type(f"{item.title} {item.summary}"),
                status=RegulatoryStatus.UNKNOWN,
                title=item.title[:256],
                summary=(item.summary or item.title)[:2000],
                event_date=when,
                information_class=InformationClass.FACT,
                evidence=EventEvidenceRef(
                    source_id=SourceId.new(),
                    authority_tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
                    locator=f"{provider}:{identity[:200]}",
                    retrieved_at=retrieved,
                    source_url=item.link,
                    published_at=when,
                    provider_name=provider,
                ),
                case_reference=case_ref or digest[:16],
                published_at=when,
            )
        except ValueError as exc:
            logger.info("regulatory_item_rejected", extra={"error": str(exc)[:200]})
            return None


class FallbackRegulatoryAdapter:
    """Live first; sample package when live has no matching items."""

    def __init__(self, primary: RegulatoryEventPort, secondary: RegulatoryEventPort) -> None:
        self._primary = primary
        self._secondary = secondary

    def get_regulatory_package(self, company_id: CompanyId) -> CompanyRegulatoryPackage | None:
        try:
            live = self._primary.get_regulatory_package(company_id)
        except Exception as exc:
            logger.warning("live_regulatory_failure", extra={"error_type": type(exc).__name__})
            live = None
        if live is not None and live.events:
            return live
        return self._secondary.get_regulatory_package(company_id)
