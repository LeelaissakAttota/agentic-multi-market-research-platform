"""Point-in-time (as-of) evidence and a small, leak-free rating backtest.

``as_of`` analysis must never see information published after its date. Evidence items
are filtered by :func:`amrp.application.debate.filter_evidence`; price bars by
:func:`filter_bars`. The backtest rebuilds market evidence from bars up to each rebalance
date, rates it with the deterministic agent pipeline and scores the call against the
forward return - so a bar after the decision date can never change that decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from amrp.application.debate import filter_evidence
from amrp.application.deliberation import deliberate
from amrp.application.outcome_memory import is_correct

__all__ = [
    "BacktestResult",
    "BacktestTrade",
    "PriceBar",
    "backtest",
    "filter_bars",
    "filter_evidence",
    "market_findings",
]

PriceBar = tuple[str, float]  # (YYYY-MM-DD, close), ascending


def filter_bars(bars: Sequence[PriceBar], as_of: str) -> list[PriceBar]:
    day = as_of[:10]
    return [b for b in bars if b[0] <= day]


def market_findings(
    bars: Sequence[PriceBar],
    as_of: str,
    *,
    currency: str = "USD",
    return_window: int = 20,
    sma_window: int = 50,
) -> dict[str, Any]:
    """Market figures (same labels as a live report) from bars on or before ``as_of``."""

    past = filter_bars(bars, as_of)
    if not past:
        return {"market_figures": []}
    day, last = past[-1]
    rows: list[dict[str, str]] = [
        {
            "tag": "M1",
            "label": "Last close",
            "value": f"{currency} {last:.2f}",
            "detail": f"as of {day}",
        },
    ]
    if len(past) > return_window:
        base = past[-1 - return_window][1]
        rows.append(
            {
                "tag": "M3",
                "label": "Simple return",
                "value": f"{(last - base) / base * 100:.2f}%",
                "detail": f"{return_window} sessions; as of {day}",
            }
        )
    if len(past) >= sma_window:
        sma = sum(c for _, c in past[-sma_window:]) / sma_window
        rows.append(
            {
                "tag": "M4",
                "label": "SMA (simple moving average)",
                "value": f"{currency} {sma:.2f}",
                "detail": f"window {sma_window}; as of {day}",
            }
        )
    return {"market_figures": rows}


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    date: str
    rating: str
    score: float
    price: float
    exit_date: str
    exit_price: float
    return_pct: float
    hit: bool

    @property
    def position_return_pct(self) -> float:
        """Long on Buy, short on Sell, flat on Hold."""

        r = self.rating.lower()
        sign = 1 if "buy" in r else -1 if "sell" in r else 0
        return sign * self.return_pct


@dataclass(frozen=True, slots=True)
class BacktestResult:
    ticker: str
    horizon_days: int
    trades: tuple[BacktestTrade, ...]

    @property
    def hit_rate(self) -> float | None:
        return sum(t.hit for t in self.trades) / len(self.trades) if self.trades else None

    @property
    def strategy_return_pct(self) -> float:
        growth = 1.0
        for t in self.trades:
            growth *= 1 + t.position_return_pct / 100
        return (growth - 1) * 100

    @property
    def buy_and_hold_pct(self) -> float:
        if not self.trades:
            return 0.0
        first, last = self.trades[0], self.trades[-1]
        return (last.exit_price - first.price) / first.price * 100


def backtest(
    bars: Sequence[PriceBar],
    *,
    ticker: str = "",
    start: str | None = None,
    end: str | None = None,
    horizon_days: int = 21,
    step: int | None = None,
    currency: str = "USD",
) -> BacktestResult:
    """Rate every ``step`` sessions using only past bars; score against the forward close.

    ``horizon_days`` and ``step`` are in trading sessions (bars). Non-overlapping by
    default (``step = horizon_days``) so the compounded strategy return is meaningful.
    """

    ordered = sorted(bars)
    step = step or horizon_days
    trades: list[BacktestTrade] = []
    i = 50  # warm-up for the SMA
    while i + horizon_days < len(ordered):
        day, price = ordered[i]
        if (start and day < start) or (end and day > end):
            i += 1 if start and day < start else len(ordered)
            continue
        d = deliberate(market_findings(ordered[: i + 1], day, currency=currency), ticker, as_of=day)
        exit_day, exit_price = ordered[i + horizon_days]
        ret = (exit_price - price) / price * 100
        trades.append(
            BacktestTrade(
                day,
                d.rating.rating.value,
                d.rating.score,
                price,
                exit_day,
                exit_price,
                round(ret, 3),
                is_correct(d.rating.rating.value, ret),
            )
        )
        i += step
    return BacktestResult(ticker, horizon_days, tuple(trades))
