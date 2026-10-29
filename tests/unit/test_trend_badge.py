"""Live daily trend badge: thresholds, 5-day cross-check, badge HTML, page wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amrp.dashboard import trend as tr
from amrp.dashboard.markets import IBar


@pytest.mark.parametrize(
    ("day", "mom", "label"),
    [
        (0.004, None, "Bullish"),
        (-0.004, None, "Bearish"),
        (0.003, None, "Neutral"),
        (-0.003, None, "Neutral"),
        (0.0, 0.05, "Neutral"),
        (0.01, 0.02, "Bullish"),
        (0.01, -0.02, "Neutral"),  # up today against a falling 5-day trend
        (-0.01, 0.02, "Neutral"),  # down today against a rising 5-day trend
        (-0.01, -0.005, "Bearish"),
    ],
)
def test_classify(day: float, mom: float | None, label: str) -> None:
    assert tr.classify(day, mom).label == label


def _bars(day_open: float, last: float, first_open: float) -> list[IBar]:
    t0 = datetime(2026, 10, 26, 9, tzinfo=UTC)
    t1 = datetime(2026, 10, 29, 9, tzinfo=UTC)
    return [
        IBar(t0, first_open, first_open, first_open, first_open),
        IBar(t1, day_open, day_open, day_open, day_open),
        IBar(t1 + timedelta(minutes=15), day_open, last, day_open, last),
    ]


def test_from_bars_uses_today_and_five_day() -> None:
    sig = tr.from_bars(_bars(100.0, 101.0, 99.0))
    assert sig is not None and sig.label == "Bullish"
    assert sig.day_change == pytest.approx(0.01)
    assert sig.momentum == pytest.approx(101 / 99 - 1)
    assert tr.from_bars([]) is None


def test_badge_html_icons_and_updated_ist() -> None:
    now = datetime(2026, 10, 29, 17, 30, tzinfo=UTC)  # 23:00 IST
    bull = tr.badge_html(tr.classify(0.01), now)
    assert "\U0001f402" in bull and "Bullish" in bull and "#22c55e" in bull
    assert "updated 23:00 IST" in bull
    bear = tr.badge_html(tr.classify(-0.01), now)
    assert "\U0001f43b" in bear and "Bearish" in bear and "#ef4444" in bear
    hold = tr.badge_html(tr.classify(0.0), now)
    assert "\u2696" in hold and "Neutral" in hold
    assert "not available" in tr.badge_html(None, now)


def test_pages_render_live_badge_fragment() -> None:
    src = (Path(__file__).resolve().parents[2] / "dashboard" / "app.py").read_text("utf-8")
    assert "@st.fragment(run_every=60)\ndef trend_badge" in src
    assert "@st.cache_data(ttl=55, show_spinner=False)\ndef _live_5d" in src
    assert src.count("trend_badge(com.symbol)") == 1
    assert src.count("trend_badge(inst.symbol)") == 2
