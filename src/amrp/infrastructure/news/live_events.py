"""Live company-event aggregation shared by optional news/filing adapters.

Event sources (exchange filings, GDELT, RSS) return already-normalized
``QualitativeEvent`` values. The aggregator dedupes near-duplicate headlines
across sources, ranks by authority tier then source reputation then recency,
and runs the domain conflict-aware dedupe. ``FallbackNewsEventAdapter``
returns the fixture/sample package whenever live coverage is empty.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from amrp.application.ports import CompanyCatalogPort, NewsEventPort
from amrp.domain.data_origin import DataOrigin
from amrp.domain.identity import CompanyId, CompanyIdentity
from amrp.domain.news import (
    CompanyEventPackage,
    EventEvidenceRef,
    EventId,
    EventType,
    InformationClass,
    NewsEventAvailability,
    QualitativeEvent,
)
from amrp.domain.news.conflicts import process_events
from amrp.domain.sources import SourceAuthorityTier, SourceId
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.news.live_events")

_MAX_EVENTS = 100

_TYPE_KEYWORDS: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    (EventType.EARNINGS, ("result", "earnings", "quarter", "profit", "revenue", "dividend")),
    (EventType.MERGER_ACQUISITION, ("acqui", "merger", "amalgamation", "takeover", "stake")),
    (EventType.MANAGEMENT, ("appoint", "resign", "ceo", "director", "board", "cfo")),
    (EventType.LITIGATION, ("lawsuit", "litigation", "court", "tribunal", "nclt", "suit")),
    (EventType.REGULATORY, ("sebi", "sec ", "regulat", "penalty", "order", "compliance")),
    (EventType.INVESTMENT, ("invest", "capex", "fund", "raise", "allotment", "bond")),
    (EventType.PRODUCT, ("launch", "product", "unveil", "partnership", "contract")),
)


def classify_event_type(text: str) -> EventType:
    """Deterministic keyword taxonomy (no LLM); falls back to OTHER."""

    lowered = f" {text.casefold()} "
    for event_type, keywords in _TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return event_type
    return EventType.OTHER


class CompanyEventSource(Protocol):
    """One live source of company events."""

    name: str

    def fetch(self, company: CompanyIdentity) -> Sequence[QualitativeEvent]:
        """Return normalized events (empty when unavailable). Must not raise."""


def build_event(
    *,
    company_id: CompanyId,
    title: str,
    summary: str | None,
    published: datetime | None,
    retrieved_at: datetime,
    url: str | None,
    tier: SourceAuthorityTier,
    provider_name: str,
    locator: str,
    event_type: EventType | None = None,
    jurisdiction: str | None = None,
) -> QualitativeEvent | None:
    """Build a domain event, or None when the item violates domain invariants."""

    when = (published or retrieved_at).astimezone(UTC)
    if when > retrieved_at:
        when = retrieved_at
    published_on: date = when.date()
    info_class = (
        InformationClass.FACT
        if tier is SourceAuthorityTier.TIER_1_AUTHORITATIVE
        else InformationClass.RESEARCH_FINDING
    )
    try:
        return QualitativeEvent(
            event_id=EventId.new(),
            company_id=company_id,
            event_type=event_type or classify_event_type(f"{title} {summary or ''}"),
            title=title[:256],
            summary=(summary or title)[:2000],
            event_date=published_on,
            information_class=info_class,
            evidence=EventEvidenceRef(
                source_id=SourceId.new(),
                authority_tier=tier,
                locator=locator[:256],
                retrieved_at=retrieved_at,
                source_url=url,
                published_at=published_on,
                provider_name=provider_name,
            ),
            jurisdiction=jurisdiction,
        )
    except ValueError as exc:
        logger.info("live_event_rejected", extra={"provider": provider_name, "error": str(exc)})
        return None


_PUBLISHER_SUFFIX = re.compile(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{2,60}$")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def headline_key(title: str) -> str:
    """Normalize a headline for cross-source near-duplicate detection."""

    base = _PUBLISHER_SUFFIX.sub("", title.casefold())
    return _NON_ALNUM.sub(" ", base).strip()


def rank_and_dedupe(
    events: Sequence[QualitativeEvent],
    *,
    source_rank: Callable[[QualitativeEvent], int] | None = None,
) -> list[QualitativeEvent]:
    """Keep the best-ranked copy of each headline / URL; best first."""

    def score(event: QualitativeEvent) -> tuple[int, int, float]:
        published = event.evidence.published_at or event.event_date
        return (
            int(event.evidence.authority_tier),
            source_rank(event) if source_rank else 0,
            -datetime.combine(published, datetime.min.time()).timestamp(),
        )

    kept: list[QualitativeEvent] = []
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    for event in sorted(events, key=score):
        key = headline_key(event.title)
        url = event.evidence.source_url
        if key in seen_titles or (url is not None and url in seen_urls):
            continue
        seen_titles.add(key)
        if url is not None:
            seen_urls.add(url)
        kept.append(event)
    return kept


#: Lower is better. Exchange/regulator disclosures first, then wire services and
#: large financial outlets; unknown publishers last.
_SOURCE_RANKS: tuple[tuple[str, int], ...] = (
    ("nse_", 0),
    ("bse_", 0),
    ("sec_", 0),
    ("sebi", 0),
    ("company_ir", 1),
    ("reuters", 2),
    ("bloomberg", 2),
    ("wsj", 3),
    ("ft.com", 3),
    ("cnbc", 3),
    ("economictimes", 3),
    ("livemint", 3),
    ("business-standard", 3),
    ("moneycontrol", 4),
)


def rank_news_source(event: QualitativeEvent) -> int:
    """Deterministic publisher reputation rank for tie-breaking within a tier."""

    haystack = f"{event.evidence.provider_name} {event.evidence.source_url or ''}".casefold()
    for marker, rank in _SOURCE_RANKS:
        if marker in haystack:
            return rank
    return 9


class LiveCompanyEventAdapter:
    """NewsEventPort aggregating live sources for catalog companies."""

    def __init__(
        self,
        catalog: CompanyCatalogPort,
        sources: Sequence[CompanyEventSource],
        *,
        source_rank: Callable[[QualitativeEvent], int] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_events: int = 50,
    ) -> None:
        if not 1 <= max_events <= _MAX_EVENTS:
            msg = f"max_events must be between 1 and {_MAX_EVENTS}"
            raise ValueError(msg)
        self._catalog = catalog
        self._sources = tuple(sources)
        self._rank = source_rank
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max = max_events

    @property
    def provider_name(self) -> str:
        return "+".join(source.name for source in self._sources) or "live"

    def get_event_package(
        self,
        company_id: CompanyId,
        *,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> CompanyEventPackage | None:
        company = self._catalog.get_by_id(company_id)
        if company is None:
            return None
        wanted: EventType | None = None
        if event_type is not None:
            try:
                wanted = EventType(event_type)
            except ValueError:
                return None
        collected: list[QualitativeEvent] = []
        for source in self._sources:
            try:
                collected.extend(source.fetch(company))
            except Exception as exc:  # a broken source must not break the others
                logger.warning(
                    "live_event_source_failure",
                    extra={"source": source.name, "error_type": type(exc).__name__},
                )
        ranked = rank_and_dedupe(collected, source_rank=self._rank)
        if wanted is not None:
            ranked = [e for e in ranked if e.event_type is wanted]
        cap = min(self._max, limit) if limit is not None else self._max
        ranked = ranked[: max(cap, 0)]
        if not ranked:
            return None
        events, conflicts = process_events(ranked)
        return CompanyEventPackage(
            company_id=company_id,
            retrieved_at=self._clock(),
            events=events,
            conflicts=conflicts,
            provider_name=self.provider_name,
            availability=NewsEventAvailability.AVAILABLE,
            data_origin=DataOrigin.LIVE,
        )


class FallbackNewsEventAdapter:
    """Primary (live) first; fixture/sample package when live has nothing."""

    def __init__(self, primary: NewsEventPort, secondary: NewsEventPort) -> None:
        self._primary = primary
        self._secondary = secondary

    def get_event_package(
        self,
        company_id: CompanyId,
        *,
        event_type: str | None = None,
        limit: int | None = None,
    ) -> CompanyEventPackage | None:
        try:
            live = self._primary.get_event_package(company_id, event_type=event_type, limit=limit)
        except Exception as exc:
            logger.warning("live_news_failure", extra={"error_type": type(exc).__name__})
            live = None
        if live is not None and live.events:
            return live
        return self._secondary.get_event_package(company_id, event_type=event_type, limit=limit)
