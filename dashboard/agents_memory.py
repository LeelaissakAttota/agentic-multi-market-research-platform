"""Outcome memory in the dashboard: storage location, price feed and the Memory page."""

from __future__ import annotations

import re
from typing import Any

import streamlit as st

from amrp.application.outcome_memory import OutcomeMemory, PriceSeries, parse_price
from amrp.control.secrets_store import default_config_dir
from amrp.dashboard.market_live import fetch_daily


@st.cache_resource
def memory() -> OutcomeMemory:
    return OutcomeMemory(default_config_dir("amrp") / "outcome_memory.sqlite3")


def yahoo_prices(ticker: str) -> PriceSeries:
    daily = fetch_daily(ticker, range_="2y")
    return [(b.date, b.close) for b in daily.bars] if daily else []


def price_and_date(findings: dict[str, Any]) -> tuple[float | None, str, str]:
    """Last close, currency and its as-of date from report findings."""

    for row in findings.get("market_figures") or []:
        if str(row.get("label", "")).lower() == "last close":
            price, cur = parse_price(str(row.get("value", "")))
            m = re.search(r"\d{4}-\d{2}-\d{2}", str(row.get("detail", "")))
            return price, cur, m.group(0) if m else ""
    return None, "", ""


def memory_page() -> None:
    st.markdown("#### Outcome Memory")
    st.caption(
        "Every agent rating is remembered. After its horizon (21 days) it is settled "
        "against the real closing price; settled calls become lessons for the next research "
        "on the same ticker and adjust its confidence."
    )
    mem = memory()
    if st.button("Settle due calls now", type="primary"):
        with st.spinner("Fetching closing prices..."):
            n = mem.settle(yahoo_prices)
        st.success(f"Settled {n} call(s).")
    s = mem.stats()
    c = st.columns(4)
    c[0].metric("Calls", s["calls"])
    c[1].metric("Settled", s["settled"])
    c[2].metric("Pending", s["pending"])
    acc = s["accuracy"]
    c[3].metric("Hit rate", f"{acc:.0%}" if isinstance(acc, float) else "-")
    rows = mem.rows()
    if not rows:
        st.info("No calls yet. Open a research report to let the agents rate it.")
        return
    st.dataframe(
        [
            {
                "As of": o.as_of,
                "Ticker": o.ticker,
                "Company": o.company,
                "Rating": o.rating,
                "Confidence": f"{o.confidence:.0%}",
                "Price": f"{o.currency} {o.price_at:.2f}" if o.price_at else "-",
                "Due": o.due_date,
                "Return": f"{o.return_pct:+.2f}%" if o.return_pct is not None else "pending",
                "Result": "-" if o.correct is None else ("right" if o.correct else "wrong"),
            }
            for o in rows
        ],
        hide_index=True,
        width="stretch",
    )
