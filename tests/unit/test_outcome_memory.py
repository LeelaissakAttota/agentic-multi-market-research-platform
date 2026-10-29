"""Outcome memory: record, settle, lessons."""

from __future__ import annotations

from pathlib import Path

from amrp.application.outcome_memory import (
    OutcomeMemory,
    is_correct,
    market_for,
    parse_price,
)

SERIES = [("2026-01-02", 100.0), ("2026-01-20", 104.0), ("2026-01-23", 110.0)]


def _mem(tmp_path: Path) -> OutcomeMemory:
    return OutcomeMemory(tmp_path / "m" / "outcomes.sqlite3")


def test_helpers() -> None:
    assert parse_price("USD 343.83") == (343.83, "USD")
    assert parse_price("INR 1,234.5") == (1234.5, "INR")
    assert parse_price("n/a") == (None, "")
    assert market_for("RELIANCE.NS") == "in" and market_for("AAPL") == "us"
    assert is_correct("Buy", 3.0) and not is_correct("Strong Sell", 3.0)
    assert is_correct("Hold", 1.5) and not is_correct("Hold", -2.5)


def test_record_is_unique_per_workflow(tmp_path: Path) -> None:
    m = _mem(tmp_path)
    kw = dict(
        ticker="aapl",
        company="Apple",
        rating="Buy",
        score=0.3,
        confidence=0.6,
        price_at=100.0,
        as_of="2026-01-02",
        workflow_id="w1",
    )
    assert m.record(**kw)  # type: ignore[arg-type]
    assert not m.record(**kw)  # type: ignore[arg-type]
    assert [o.ticker for o in m.rows()] == ["AAPL"]
    assert m.rows()[0].due_date == "2026-01-23"


def test_settle_only_when_due_and_lessons(tmp_path: Path) -> None:
    m = _mem(tmp_path)
    m.record(
        ticker="AAPL",
        company="Apple",
        rating="Sell",
        score=-0.3,
        confidence=0.5,
        price_at=100.0,
        as_of="2026-01-02",
        currency="USD",
        workflow_id="w1",
    )
    calls: list[str] = []

    def prices(t: str):  # type: ignore[no-untyped-def]
        calls.append(t)
        return SERIES

    assert m.settle(prices, today="2026-01-20") == 0  # not due yet
    assert m.settle(prices, today="2026-01-25") == 1
    o = m.rows()[0]
    assert o.settle_date == "2026-01-23" and o.return_pct == 10.0 and o.correct is False
    lessons, acc = m.lessons("aapl")
    assert acc == 0.0 and "wrong" in lessons[0] and "+10.0%" in lessons[0]
    assert m.settle(prices, today="2026-02-01") == 0  # already settled
    assert m.stats() == {"calls": 1, "settled": 1, "pending": 0, "accuracy": 0.0}


def test_settle_survives_feed_errors(tmp_path: Path) -> None:
    m = _mem(tmp_path)
    m.record(
        ticker="X",
        company="X",
        rating="Buy",
        score=0.2,
        confidence=0.5,
        price_at=10.0,
        as_of="2026-01-02",
    )

    def boom(t: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("feed down")

    assert m.settle(boom, today="2026-03-01") == 0
    assert m.accuracy() is None and m.lessons("X") == ((), None)


def test_persists_across_instances(tmp_path: Path) -> None:
    m = _mem(tmp_path)
    m.record(
        ticker="TCS.NS",
        company="TCS",
        rating="Hold",
        score=0,
        confidence=0.4,
        price_at=3000.0,
        as_of="2026-01-02",
    )
    m.close()
    again = _mem(tmp_path)
    assert again.rows()[0].market == "in"
