"""Phase 5 News & Event Intelligence domain package."""

from amrp.domain.news.conflicts import (
    EventConflict,
    EventConflictState,
    detect_soft_date_conflicts,
    process_events,
    resolve_exact_dedupe_group,
)
from amrp.domain.news.dedupe import (
    deduplicate_events,
    deduplicate_events_with_conflicts,
)
from amrp.domain.news.events import (
    CompanyEventPackage,
    EventAgeMetadata,
    EventEvidenceRef,
    EventId,
    EventType,
    InformationClass,
    NewsEventAvailability,
    QualitativeEvent,
    compute_event_age,
)

__all__ = [
    "CompanyEventPackage",
    "EventAgeMetadata",
    "EventConflict",
    "EventConflictState",
    "EventEvidenceRef",
    "EventId",
    "EventType",
    "InformationClass",
    "NewsEventAvailability",
    "QualitativeEvent",
    "compute_event_age",
    "deduplicate_events",
    "deduplicate_events_with_conflicts",
    "detect_soft_date_conflicts",
    "process_events",
    "resolve_exact_dedupe_group",
]
