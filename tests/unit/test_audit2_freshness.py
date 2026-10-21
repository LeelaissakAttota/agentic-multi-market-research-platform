from datetime import UTC, date, datetime, timedelta

from financial_intelligence.application.market_freshness import MarketFreshnessPolicy
from financial_intelligence.domain.market import FreshnessStatus
from financial_intelligence.infrastructure.market.india_quotes import _parse_ist

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def test_bse_two_digit_year_24h_format_parses() -> None:
    parsed = _parse_ist("14 Oct 26 | 16:00", NOW)
    assert parsed == datetime(2026, 10, 14, 10, 30, tzinfo=UTC)


def test_parse_ist_never_returns_future() -> None:
    assert _parse_ist("14 Oct 26 | 23:59", NOW) == NOW
    assert _parse_ist("garbage", NOW) == NOW


def test_small_future_skew_is_fresh_large_is_unknown() -> None:
    from tests.unit.test_market_hardening import _bar, _series

    policy = MarketFreshnessPolicy(stale_after=timedelta(hours=72))
    near = _series((_bar(date(2026, 10, 14), close="10"),), as_of=NOW + timedelta(seconds=3))
    far = _series((_bar(date(2026, 10, 14), close="10"),), as_of=NOW + timedelta(hours=1))
    assert policy.classify(near, now=NOW) is FreshnessStatus.FRESH
    assert policy.classify(far, now=NOW) is FreshnessStatus.UNKNOWN
