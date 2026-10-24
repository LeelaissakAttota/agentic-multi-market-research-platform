from __future__ import annotations

from amrp.composition import build_container
from amrp.config.settings import Settings
from amrp.infrastructure.market import (
    FallbackMarketDataAdapter,
    YahooChartMarketDataAdapter,
)
from amrp.infrastructure.market.india_quotes import (
    IndiaLiveQuoteMarketDataAdapter,
)


def _stack(**kw: object) -> list[object]:
    settings = Settings(_env_file=None, APP_ENV="test", **kw)  # type: ignore[arg-type]
    container = build_container(settings)
    node: object = container.get_market_snapshot._market_data
    seen: list[object] = []
    stack = [node]
    while stack:
        current = stack.pop()
        seen.append(current)
        for attr in ("_inner", "_base", "_primary", "_secondary", "_adapter"):
            child = getattr(current, attr, None)
            if child is not None:
                stack.append(child)
    return seen


def test_india_only_does_not_enable_yahoo_for_all_markets() -> None:
    nodes = _stack(INDIA_LIVE_QUOTES_ENABLED=True)
    assert any(isinstance(n, IndiaLiveQuoteMarketDataAdapter) for n in nodes)
    assert not any(isinstance(n, YahooChartMarketDataAdapter) for n in nodes)


def test_live_market_enables_yahoo_and_india_overlay() -> None:
    nodes = _stack(MARKET_DATA_LIVE_ENABLED=True)
    assert any(isinstance(n, YahooChartMarketDataAdapter) for n in nodes)
    assert any(isinstance(n, FallbackMarketDataAdapter) for n in nodes)
    assert any(isinstance(n, IndiaLiveQuoteMarketDataAdapter) for n in nodes)
