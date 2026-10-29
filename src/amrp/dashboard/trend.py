"""Live daily trend signal (Bullish / Bearish / Neutral) for market instrument cards.

Rule: today's change (last close vs the day's first open) above +0.3% is Bullish, below
-0.3% is Bearish, otherwise Hold (shown as "Neutral"). The short-term trend (5-day
momentum, last close vs the first open of the 5-day window) cross-checks it: a day move
that runs against a strong opposite 5-day trend (beyond 1%) is downgraded to Neutral.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Literal

from amrp.dashboard.markets import IBar, last_day

Label = Literal["Bullish", "Bearish", "Neutral"]

DAY_THRESHOLD = 0.003
MOMENTUM_CONFLICT = 0.01
IST = timezone(timedelta(hours=5, minutes=30))

_STYLE: dict[str, tuple[str, str, str]] = {
    # label -> (icon, text colour, background)
    "Bullish": ("\U0001f402", "#22c55e", "rgba(34,197,94,.14)"),
    "Bearish": ("\U0001f43b", "#ef4444", "rgba(239,68,68,.14)"),
    "Neutral": ("\u2696\ufe0f", "#f59e0b", "rgba(148,163,184,.16)"),
}


@dataclass(frozen=True)
class TrendSignal:
    label: Label
    day_change: float
    momentum: float | None
    reason: str


def classify(day_change: float, momentum: float | None = None) -> TrendSignal:
    """Combine today's % change with the short-term momentum into one signal."""
    if day_change > DAY_THRESHOLD:
        base: Label = "Bullish"
    elif day_change < -DAY_THRESHOLD:
        base = "Bearish"
    else:
        return TrendSignal("Neutral", day_change, momentum, "today's move within ±0.3%")
    if momentum is not None:
        if base == "Bullish" and momentum < -MOMENTUM_CONFLICT:
            return TrendSignal("Neutral", day_change, momentum, "up today but 5-day trend is down")
        if base == "Bearish" and momentum > MOMENTUM_CONFLICT:
            return TrendSignal("Neutral", day_change, momentum, "down today but 5-day trend is up")
    word = "above +0.3%" if base == "Bullish" else "below -0.3%"
    return TrendSignal(base, day_change, momentum, f"today's move {word}")


def from_bars(bars: list[IBar]) -> TrendSignal | None:
    """Signal from multi-day intraday bars (e.g. 5 days of 15-minute bars)."""
    if not bars:
        return None
    day = last_day(bars)
    if not day or not day[0].open:
        return None
    last = day[-1].close
    day_change = last / day[0].open - 1
    momentum = last / bars[0].open - 1 if bars[0].open else None
    return classify(day_change, momentum)


def badge_html(sig: TrendSignal | None, updated: datetime) -> str:
    """Large highlighted icon on top, bold coloured label below, small live line under it."""
    stamp = updated.astimezone(IST).strftime("%H:%M")
    if sig is None:
        return (
            '<div class="trend-na" style="text-align:center"><small>Trend: not available'
            f"<br>updated {stamp} IST</small></div>"
        )
    icon, fg, bg = _STYLE[sig.label]
    mom = f" · 5-day {sig.momentum:+.2%}" if sig.momentum is not None else ""
    return (
        f'<div class="trend-badge trend-{sig.label.lower()}" title="{escape(sig.reason)}" '
        'style="display:flex;flex-direction:column;align-items:center;gap:.35rem;'
        'text-align:center">'
        f'<div class="trend-icon" style="width:76px;height:76px;border-radius:20px;'
        f"display:flex;align-items:center;justify-content:center;font-size:48px;"
        f"line-height:1;background:{bg};border:2px solid {fg};"
        f'box-shadow:0 0 18px {fg}88,inset 0 0 12px {fg}33">{icon}</div>'
        f'<div class="trend-label" style="color:{fg};font-weight:800;font-size:1.15rem;'
        f'letter-spacing:.02em">{sig.label}</div>'
        f'<div class="trend-meta" style="line-height:1.25"><small>Today '
        f"{sig.day_change:+.2%}{mom}<br>updated {stamp} IST</small></div></div>"
    )
