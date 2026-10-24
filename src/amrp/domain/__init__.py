"""Provider-neutral domain primitives.

Domain code must remain free of FastAPI, providers, and infrastructure SDKs.
"""

from amrp.domain.research_run import ResearchRunId
from amrp.domain.sources import (
    SourceAuthorityTier,
    SourceId,
    SourceMetadata,
    SourceType,
)

__all__ = [
    "ResearchRunId",
    "SourceAuthorityTier",
    "SourceId",
    "SourceMetadata",
    "SourceType",
]
