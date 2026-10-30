"""As-of analysis must not leak future data; backtest mechanics."""

from __future__ import annotations

import math
from datetime import date, timedelta

from amrp.application.deliberation import deliberate
from amrp.application.point_in_time import backtest, filter_bars, market_findings
from tests.unit.deliberation_fixtures import FINDINGS


def _bars(n: int = 200, drift: float = 0.004) -> list[tuple[str, float]]:
    d0 = date(2025, 1, 1)
    return [
        ((d0 + timedelta(days=i)).isoformat(), 100 * math.exp(drift * i + 0.03 * math.sin(i / 3)))
        for i in range(n)
    ]


def test_filter_bars_and_findings_stop_at_as_of() -> None:
    bars = _bars()
    cut = bars[80][0]
    assert filter_bars(bars, cut)[-1][0] == cut
    f = market_findings(bars, cut)
    assert all(cut in r["detail"] for r in f["market_figures"])
    assert {r["label"] for r in f["market_figures"]} == {
        "Last close",
        "Simple return",
        "SMA (simple moving average)",
    }
    assert market_findings(bars, "2000-01-01") == {"market_figures": []}


def test_future_bars_cannot_change_past_rating() -> None:
    bars = _bars()
    cut = bars[120][0]
    crash = bars[:121] + [(d, c * 0.2) for d, c in bars[121:]]
    a = deliberate(market_findings(bars, cut), "T", as_of=cut)
    b = deliberate(market_findings(crash, cut), "T", as_of=cut)
    assert a.rating == b.rating


def test_backtest_decisions_are_leak_free() -> None:
    bars = _bars()
    crash = bars[:150] + [(d, c * 0.5) for d, c in bars[150:]]
    ra, rb = backtest(bars, ticker="T"), backtest(crash, ticker="T")
    for ta, tb in zip(ra.trades, rb.trades, strict=True):
        if ta.date < bars[150][0]:
            assert (ta.rating, ta.score) == (tb.rating, tb.score)


def test_backtest_metrics() -> None:
    res = backtest(_bars(), ticker="UP", horizon_days=10)
    assert res.trades and res.trades[0].date == _bars()[50][0]
    assert all(t.exit_date > t.date for t in res.trades)
    gaps = {
        (date.fromisoformat(b.date) - date.fromisoformat(a.date)).days
        for a, b in zip(res.trades, res.trades[1:], strict=False)
    }
    assert gaps == {10}
    assert res.hit_rate is not None and 0 <= res.hit_rate <= 1
    assert res.buy_and_hold_pct > 0  # steady uptrend
    assert backtest(_bars(30)).trades == ()


def test_backtest_window() -> None:
    bars = _bars()
    res = backtest(bars, start=bars[100][0], end=bars[140][0], horizon_days=5)
    assert res.trades[0].date >= bars[100][0] and res.trades[-1].date <= bars[140][0]


def test_report_as_of_filter_is_respected() -> None:
    d = deliberate(FINDINGS, "Apple", as_of="2025-12-31")
    cited = {t for a in d.debate.arguments for p in a.points for t in p.tags}
    assert not cited & {"F1", "F2", "N1", "N3"}
