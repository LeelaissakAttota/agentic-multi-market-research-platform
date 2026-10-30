"""Outcome memory: remember past ratings, settle them against later prices, learn lessons.

Every rating produced by the deliberation step is stored in a small SQLite file. Once the
horizon has passed, :meth:`OutcomeMemory.settle` compares the rating with the realised
return. Settled calls become short "lessons" and a past-accuracy figure that are fed back
into the next research on the same ticker. Prices come from an injected callable so the
module stays offline-testable.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PriceSeries = Sequence[tuple[str, float]]  # (YYYY-MM-DD, close), ascending
PriceLookup = Callable[[str], PriceSeries]

HOLD_BAND_PCT = 2.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT UNIQUE,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL,
    market TEXT NOT NULL,
    rating TEXT NOT NULL,
    score REAL NOT NULL,
    confidence REAL NOT NULL,
    price_at REAL,
    currency TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    settled_at TEXT,
    settle_date TEXT,
    settle_price REAL,
    return_pct REAL,
    correct INTEGER
)
"""


@dataclass(frozen=True, slots=True)
class Outcome:
    id: int
    workflow_id: str | None
    ticker: str
    company: str
    market: str
    rating: str
    score: float
    confidence: float
    price_at: float | None
    currency: str
    created_at: str
    as_of: str
    horizon_days: int
    settled_at: str | None
    settle_date: str | None
    settle_price: float | None
    return_pct: float | None
    correct: bool | None

    @property
    def settled(self) -> bool:
        return self.settled_at is not None

    @property
    def due_date(self) -> str:
        return (date.fromisoformat(self.as_of[:10]) + timedelta(days=self.horizon_days)).isoformat()


def rating_direction(rating: str) -> int:
    r = rating.lower()
    if "buy" in r:
        return 1
    if "sell" in r:
        return -1
    return 0


def is_correct(rating: str, return_pct: float) -> bool:
    """A call is right when the move has its direction (Hold: |move| < 2%)."""

    d = rating_direction(rating)
    if d == 0:
        return abs(return_pct) < HOLD_BAND_PCT
    return return_pct * d > 0


def parse_price(value: str) -> tuple[float | None, str]:
    """``"USD 343.83"`` -> ``(343.83, "USD")``."""

    m = re.match(r"^\s*([A-Z]{3})?\s*([-\d,]+(?:\.\d+)?)", value or "")
    if not m:
        return None, ""
    try:
        return float(m.group(2).replace(",", "")), m.group(1) or ""
    except ValueError:
        return None, m.group(1) or ""


def market_for(ticker: str) -> str:
    return "in" if ticker.upper().endswith((".NS", ".BO")) else "us"


class OutcomeMemory:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        *,
        ticker: str,
        company: str,
        rating: str,
        score: float,
        confidence: float,
        price_at: float | None,
        as_of: str,
        currency: str = "",
        market: str | None = None,
        workflow_id: str | None = None,
        horizon_days: int = 21,
        created_at: str | None = None,
    ) -> bool:
        """Store one call; returns False when this workflow was already recorded."""

        cur = self._db.execute(
            "INSERT OR IGNORE INTO outcome_memory (workflow_id, ticker, company, market, rating,"
            " score, confidence, price_at, currency, created_at, as_of, horizon_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                workflow_id,
                ticker.upper(),
                company,
                market or market_for(ticker),
                rating,
                float(score),
                float(confidence),
                price_at,
                currency,
                created_at or datetime.now(UTC).isoformat(timespec="seconds"),
                as_of[:10],
                int(horizon_days),
            ),
        )
        self._db.commit()
        return cur.rowcount == 1

    def rows(self, ticker: str | None = None) -> list[Outcome]:
        sql = "SELECT * FROM outcome_memory"
        args: tuple[str, ...] = ()
        if ticker:
            sql += " WHERE ticker = ?"
            args = (ticker.upper(),)
        out = []
        for r in self._db.execute(sql + " ORDER BY as_of DESC, id DESC", args):
            d = dict(r)
            d["correct"] = None if d["correct"] is None else bool(d["correct"])
            out.append(Outcome(**d))
        return out

    def settle(self, prices: PriceLookup, today: str | None = None) -> int:
        """Settle every due call whose price series reaches the due date."""

        day = (today or datetime.now(UTC).date().isoformat())[:10]
        settled = 0
        cache: dict[str, PriceSeries] = {}
        for o in self.rows():
            if o.settled or o.due_date > day or not o.price_at:
                continue
            if o.ticker not in cache:
                try:
                    cache[o.ticker] = prices(o.ticker)
                except Exception:
                    cache[o.ticker] = ()
            later = [(d, c) for d, c in cache[o.ticker] if d >= o.due_date and d <= day]
            if not later:
                continue
            sdate, sprice = later[0]
            ret = (sprice - o.price_at) / o.price_at * 100
            self._db.execute(
                "UPDATE outcome_memory SET settled_at=?, settle_date=?, settle_price=?,"
                " return_pct=?, correct=? WHERE id=?",
                (day, sdate, sprice, round(ret, 3), int(is_correct(o.rating, ret)), o.id),
            )
            settled += 1
        self._db.commit()
        return settled

    def accuracy(self, ticker: str | None = None) -> float | None:
        done = [o for o in self.rows(ticker) if o.settled]
        if not done:
            return None
        return sum(1 for o in done if o.correct) / len(done)

    def lessons(self, ticker: str, limit: int = 3) -> tuple[tuple[str, ...], float | None]:
        """Short lesson lines from the latest settled calls plus past accuracy."""

        done = [o for o in self.rows(ticker) if o.settled][:limit]
        lines = []
        for o in done:
            verdict = "right" if o.correct else "wrong"
            lines.append(
                f"{o.as_of}: rated {o.rating} at {o.currency} {o.price_at:.2f}; "
                f"{o.horizon_days}d later {o.return_pct:+.1f}% -> {verdict}"
                + ("" if o.correct else " (weigh the opposing case more)")
            )
        return tuple(lines), self.accuracy(ticker)

    def stats(self) -> dict[str, float | int | None]:
        rows = self.rows()
        done = [o for o in rows if o.settled]
        return {
            "calls": len(rows),
            "settled": len(done),
            "pending": len(rows) - len(done),
            "accuracy": (sum(1 for o in done if o.correct) / len(done)) if done else None,
        }
