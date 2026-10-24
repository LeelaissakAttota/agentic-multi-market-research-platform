"""Market-data infrastructure adapters for Phase 3."""

from amrp.infrastructure.market.cache import CachingMarketDataAdapter
from amrp.infrastructure.market.fallback import FallbackMarketDataAdapter
from amrp.infrastructure.market.in_memory import InMemoryMarketDataAdapter
from amrp.infrastructure.market.reference_dataset import (
    build_reference_market_series,
)
from amrp.infrastructure.market.yahoo_chart import YahooChartMarketDataAdapter

__all__ = [
    "CachingMarketDataAdapter",
    "FallbackMarketDataAdapter",
    "InMemoryMarketDataAdapter",
    "YahooChartMarketDataAdapter",
    "build_reference_market_series",
]
