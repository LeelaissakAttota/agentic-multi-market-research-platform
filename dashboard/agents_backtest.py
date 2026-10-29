"""Backtest page: point-in-time agent ratings scored against forward returns."""

from __future__ import annotations

import streamlit as st

from amrp.application.point_in_time import backtest
from amrp.dashboard.market_live import fetch_daily


def backtest_page() -> None:
    st.markdown("#### Agent Backtest")
    st.caption(
        "Replays the agent rating on past dates using only prices known on that date "
        "(point-in-time, no look-ahead), then scores each call against the close "
        "`horizon` sessions later. Market evidence only - fundamentals and news are not "
        "replayed. Research only; not investment advice."
    )
    c1, c2, c3 = st.columns([2, 1, 1])
    ticker = c1.text_input("Ticker", value="AAPL", help="Yahoo symbol, e.g. AAPL or TCS.NS")
    horizon = c2.selectbox("Horizon (sessions)", [5, 10, 21, 42], index=2)
    rng = c3.selectbox("History", ["1y", "2y", "5y"], index=1)
    if not st.button("Run backtest", type="primary"):
        return
    with st.spinner(f"Fetching {rng} of {ticker} closes..."):
        daily = fetch_daily(ticker.strip().upper(), range_=rng)
    if daily is None or len(daily.bars) < 80:
        st.error("Not enough price history for this ticker.")
        return
    bars = [(b.date, b.close) for b in daily.bars]
    res = backtest(
        bars, ticker=daily.symbol, horizon_days=int(horizon), currency=daily.currency or "USD"
    )
    if not res.trades:
        st.warning("No complete trades in this window.")
        return
    m = st.columns(4)
    m[0].metric("Calls", len(res.trades))
    m[1].metric("Hit rate", f"{(res.hit_rate or 0):.0%}")
    m[2].metric("Agent strategy", f"{res.strategy_return_pct:+.1f}%")
    m[3].metric("Buy & hold", f"{res.buy_and_hold_pct:+.1f}%")
    growth, curve = 1.0, []
    for t in res.trades:
        growth *= 1 + t.position_return_pct / 100
        curve.append({"date": t.exit_date, "Agent strategy (x)": round(growth, 4)})
    st.line_chart(curve, x="date", y="Agent strategy (x)")
    st.dataframe(
        [
            {
                "Date": t.date,
                "Rating": t.rating,
                "Score": round(t.score, 2),
                "Price": round(t.price, 2),
                "Exit": t.exit_date,
                "Fwd return": f"{t.return_pct:+.2f}%",
                "Hit": "yes" if t.hit else "no",
            }
            for t in res.trades
        ],
        hide_index=True,
        width="stretch",
    )
