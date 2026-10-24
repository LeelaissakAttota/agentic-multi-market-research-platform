"""Phase 5 news/event infrastructure adapters."""

from amrp.infrastructure.news.cache import CachingNewsEventAdapter
from amrp.infrastructure.news.in_memory import InMemoryNewsEventAdapter

__all__ = [
    "CachingNewsEventAdapter",
    "InMemoryNewsEventAdapter",
]
