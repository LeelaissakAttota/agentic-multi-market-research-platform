from financial_intelligence.config.settings import Settings


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, APP_ENV="test", **kw)  # type: ignore[arg-type]


def test_india_quotes_follow_live_market_flag_by_default() -> None:
    assert _s().india_live_quotes_active() is False
    assert _s(MARKET_DATA_LIVE_ENABLED=True).india_live_quotes_active() is True


def test_explicit_override_wins() -> None:
    live = _s(MARKET_DATA_LIVE_ENABLED=True, INDIA_LIVE_QUOTES_ENABLED=False)
    assert live.india_live_quotes_active() is False
    assert _s(INDIA_LIVE_QUOTES_ENABLED=True).india_live_quotes_active() is True
