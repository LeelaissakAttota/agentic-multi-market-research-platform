"""AERP dashboard v2: dark navy theme, sidebar navigation, real data only.

Run:  streamlit run dashboard/app.py   (or .\\scripts\\start-dashboard.ps1 on Windows)
Talks only to the local REST API (AERP_API_URL) and control API (AERP_CONTROL_URL).
Every number on screen comes from those APIs or the local run history; anything the
backend does not provide is shown as "not available" or with a SAMPLE badge.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from theme import (  # type: ignore[import-not-found]  # dashboard/theme.py
    CSS,
    NAV_ICON_CSS,
    SIDE_FOOT,
    TOPBAR,
    asset_uri,
    esc,
    initials,
    logo_html,
    pill,
    stat_card,
    status_pill,
    table,
)

from financial_intelligence.dashboard.client import (
    ApiError,
    JsonClient,
    candidate_choices,
    probe,
    resolve_company,
)
from financial_intelligence.dashboard.intent import EXAMPLE_PROMPTS, parse_intent
from financial_intelligence.dashboard.prefs import LIVE_TOGGLES, load_prefs, save_prefs
from financial_intelligence.dashboard.providers import (
    PROVIDERS,
    choose_model,
    connection_test_request,
)
from financial_intelligence.dashboard.report_view import (
    badge_markdown,
    build_report_view,
    origin_badge,
)
from financial_intelligence.dashboard.views import (
    OBJECTIVE_HINTS,
    OBJECTIVE_LABELS,
    POPULAR_COMPANIES,
    REPORT_TABS,
    add_to_watchlist,
    close_series,
    evidence_refs,
    figure_value,
    filter_reports,
    history_as_workflows,
    latest_close,
    load_history,
    load_watchlist,
    merge_workflows,
    objective_label,
    provider_cards,
    record_history,
    remove_from_watchlist,
    revenue_profit_series,
    split_approvals,
    step_states,
    to_csv,
    workflow_rows,
    workflow_stats,
)

API = JsonClient(os.environ.get("AERP_API_URL", "http://127.0.0.1:8000"))
CONTROL = JsonClient(
    os.environ.get("AERP_CONTROL_URL", "http://127.0.0.1:8712"),
    token=os.environ.get("EQUITY_RESEARCH_CONTROL_TOKEN", ""),
    timeout=30,
)
NA = "not available"

st.set_page_config(page_title="AERP - Agentic Equity Research", page_icon="📈", layout="wide")
ss = st.session_state
st.markdown(CSS, unsafe_allow_html=True)


def safe(fn: Any, *args: Any, default: Any = None, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except ApiError:
        return default


def all_workflows() -> list[dict[str, Any]]:
    api = safe(API.get, "/research/workflows", limit=100, default={}) or {}
    return merge_workflows(api.get("workflows", []), history_as_workflows(load_history()))


def origin_pill(origin: str | None, status: str | None = None) -> str:
    label, colour = origin_badge(origin, status)
    tone = {"green": "green", "blue": "blue", "orange": "amber"}.get(colour, "gray")
    return pill(label, tone)


def go(page: str) -> None:
    ss.nav = page


# ---------------------------------------------------------------- research flow
def start_research(question: str, pick: dict[str, str] | None = None, objective: str = "") -> None:
    for key in ("result", "pending", "choices"):
        ss.pop(key, None)
    intent = parse_intent(question)
    ss.question, ss.intent = question, intent
    chosen = objective or intent.objective
    ss.objective_used = chosen
    body: dict[str, Any] = {"objective": chosen, "objective_text": question[:512]}
    if pick:
        body.update(q=pick["q"], ticker=pick["ticker"] or None, exchange=pick["exchange"] or None)
        company = pick["label"]
    else:
        with st.spinner("Finding the company…"):
            res = resolve_company(API, intent.candidates)
        if res is None:
            ss.error = (
                "I couldn't find a listed company in that question. Try e.g. 'Apple' or "
                "'TCS', or turn on live data in Settings for companies outside the offline "
                "sample set."
            )
            return
        if res["status"] == "AMBIGUOUS":
            ss.choices = candidate_choices(res)
            return
        comp = res.get("company") or {}
        company = str(comp.get("display_name") or res["matched_phrase"])
        body["q"] = res["matched_phrase"]
    ss.company = company
    created = API.post("/research/workflows", body)
    if created.get("approval_status") == "pending":
        ss.pending = {"workflow_id": created["workflow_id"], "company": company}
        record_history(
            {
                "workflow_id": created["workflow_id"],
                "company": company,
                "objective": chosen,
                "status": "created",
                "approval_status": "pending",
                "created_at": created.get("evaluated_at"),
            }
        )
        return
    run_workflow(created["workflow_id"], company)


def run_workflow(workflow_id: str, company: str) -> None:
    with st.spinner(f"Researching {company}… (live sources can take up to a minute)"):
        executed = API.post(f"/research/workflows/{workflow_id}/execute")
        rep = API.post(f"/research/workflows/{workflow_id}/report", format="json")
        md = API.post(f"/research/workflows/{workflow_id}/report", format="markdown")
    wf = executed.get("workflow") or {}
    content = rep["stored_report"]["content"]
    ss.result = {
        "view": build_report_view(content, company),
        "raw": content,
        "markdown": md["stored_report"]["content"],
        "workflow": wf,
        "workflow_id": workflow_id,
    }
    ss.reports = {**ss.get("reports", {}), workflow_id: ss.result}
    record_history(
        {
            "workflow_id": workflow_id,
            "company": company,
            "objective": wf.get("objective"),
            "status": wf.get("status"),
            "approval_status": wf.get("approval_status"),
            "created_at": wf.get("created_at"),
        }
    )


def open_report(workflow_id: str, company: str) -> None:
    """Render the stored workflow's report again via the API (no new research run)."""

    wf = API.get(f"/research/workflows/{workflow_id}")
    rep = API.post(f"/research/workflows/{workflow_id}/report", format="json")
    md = API.post(f"/research/workflows/{workflow_id}/report", format="markdown")
    content = rep["stored_report"]["content"]
    result = {
        "view": build_report_view(content, company),
        "raw": content,
        "markdown": md["stored_report"]["content"],
        "workflow": wf.get("workflow") or wf,
        "workflow_id": workflow_id,
    }
    ss.reports = {**ss.get("reports", {}), workflow_id: result}
    ss.selected_report = workflow_id


def decide(workflow_id: str, company: str, decision: str) -> None:
    note = "approved in dashboard" if decision == "approved" else "rejected in dashboard"
    API.post(f"/research/workflows/{workflow_id}/approval", {"decision": decision, "note": note})
    ss.pop("pending", None)
    if decision == "approved":
        run_workflow(workflow_id, company)
        ss.selected_report = workflow_id
    else:
        record_history(
            {
                "workflow_id": workflow_id,
                "company": company,
                "status": "cancelled",
                "approval_status": "rejected",
            }
        )
        ss.info = f"Rejected. Nothing was run for {company}."


def flash() -> None:
    for key in ("error", "info"):
        if key in ss:
            getattr(st, key)(ss.pop(key))


def _style(chart: Any) -> Any:
    return (
        chart.properties(height=215, background="transparent")
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor="#9FB2D6",
            titleColor="#9FB2D6",
            gridColor="#0D2B55",
            domainColor="#0D2B55",
            tickColor="#0D2B55",
            labelFontSize=11,
        )
        .configure_legend(labelColor="#C9D4EA", orient="top", title=None, labelFontSize=11)
    )


def revenue_chart(pairs: list[tuple[str, str, float]]) -> Any:
    data = [{"Period": p, "Series": k, "Amount": v} for p, k, v in pairs]
    return _style(
        alt.Chart(pd.DataFrame(data))
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("Period:N", title=None, axis=alt.Axis(labelAngle=0)),
            xOffset="Series:N",
            y=alt.Y("Amount:Q", title=None, axis=alt.Axis(format="~s")),
            color=alt.Color(
                "Series:N",
                scale=alt.Scale(domain=["Revenue", "Net Profit"], range=["#2F7BFF", "#22D3A0"]),
            ),
            tooltip=["Period:N", "Series:N", alt.Tooltip("Amount:Q", format=",.0f")],
        )
    )


def price_chart(series: list[tuple[str, float]]) -> Any:
    data = [{"Date": d, "Close": v} for d, v in series]
    lo = min(v for _, v in series)
    hi = max(v for _, v in series)
    pad = (hi - lo) * 0.15 or hi * 0.02
    return _style(
        alt.Chart(pd.DataFrame(data))
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, color="#2F7BFF")
        .encode(
            x=alt.X("Date:O", title=None, axis=alt.Axis(labelAngle=0, labelOverlap=True)),
            y=alt.Y(
                "Close:Q", title=None, scale=alt.Scale(domain=[lo - pad, hi + pad], nice=False)
            ),
            y2=alt.datum(lo - pad),
            tooltip=["Date:O", alt.Tooltip("Close:Q", format=",.2f")],
        )
    )


def search_box(key: str, button: str = "Start Research", objective: str = "") -> None:
    with st.form(f"ask_{key}", clear_on_submit=False, border=False):
        c1, c2 = st.columns([5, 1])
        q = c1.text_input(
            "Company or question",
            value=ss.get(f"prefill_{key}", ""),
            placeholder="Enter company name or ticker… e.g. Reliance, TCS, Apple (AAPL)",
            label_visibility="collapsed",
            key=f"askin_{key}",
        )
        sent = c2.form_submit_button(button, type="primary", use_container_width=True)
    if sent and q.strip():
        try:
            start_research(q.strip(), objective=objective)
            go("Research Report" if "result" in ss else "Research")
            st.rerun()
        except ApiError as exc:
            st.error(f"Something went wrong: {exc.message}")


def chips(names: list[tuple[str, str]], key: str, objective: str = "") -> None:
    cols = st.columns(len(names))
    for col, (label, prompt) in zip(cols, names, strict=True):
        if col.button(label, key=f"{key}_{label}", use_container_width=True):
            try:
                start_research(prompt, objective=objective)
                go("Research Report" if "result" in ss else "Research")
            except ApiError as exc:
                ss.error = exc.message
            st.rerun()


# ---------------------------------------------------------------- layout pieces
def topbar(key: str) -> None:
    """Search box (runs research), Dark badge and the user avatar, like the reference."""

    c1, c2 = st.columns([3, 1.2])
    with c1, st.form(f"top_{key}", border=False):
        q = st.text_input(
            "Search",
            placeholder="🔍  Search company (e.g. Reliance, TCS, AAPL)",
            label_visibility="collapsed",
        )
        sent = st.form_submit_button("search", type="secondary")
    c2.markdown(TOPBAR, unsafe_allow_html=True)
    if sent and q.strip():
        try:
            start_research(q.strip())
            go("Research Report" if "result" in ss else "Research")
        except ApiError as exc:
            ss.error = exc.message
        st.rerun()


def panel(title: str) -> Any:
    box = st.container(border=True)
    box.markdown(f'<div class="panel-t">{esc(title)}</div>', unsafe_allow_html=True)
    return box


def rr_list(rows: list[dict[str, str]], limit: int = 5) -> str:
    out = []
    for r in rows[:limit]:
        out.append(
            f'<div class="rr"><div class="ic">📄</div><div class="n"><b>{esc(r["Company"])}</b>'
            f'<small>{esc(r["Objective"])}</small></div><span class="tm">{esc(r["Created"][5:16])}'
            f"</span>{status_pill(r['Status'])}</div>"
        )
    return "".join(out) or '<div class="na">No research yet.</div>'


# ---------------------------------------------------------------- pages
def home_page() -> None:
    topbar("home")
    globe = asset_uri("hero-globe.png")
    img = f'<img src="{globe}" alt="">' if globe else ""
    st.markdown(
        f'<div class="hero">{img}<h1>Welcome to AERP</h1><h3>Your Agentic Equity Research '
        "Platform</h3><p>Get evidence-backed research reports for Indian &amp; US equities."
        "</p></div>",
        unsafe_allow_html=True,
    )
    with st.container(key="herorow"):
        search_box("home")
        c0, *cols = st.columns([1.1, 1, 1, 1, 1, 1, 3])
        c0.markdown(
            '<div style="padding-top:.35rem;font-size:.8rem;color:#C9D4EA">Popular searches:</div>',
            unsafe_allow_html=True,
        )
        for col, (label, prompt) in zip(cols, EXAMPLE_PROMPTS, strict=False):
            if col.button(label, key=f"homechip_{label}"):
                try:
                    start_research(prompt)
                    go("Research Report" if "result" in ss else "Research")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
    flash()
    wfs = all_workflows()
    stats = workflow_stats(wfs)
    rate = f"{stats.completed / stats.total:.0%} success rate" if stats.total else "no runs yet"
    c = st.columns(4)
    c[0].markdown(
        stat_card("Total Researches", str(stats.total), "run history", "blue", "▣"),
        unsafe_allow_html=True,
    )
    c[1].markdown(
        stat_card("Completed", str(stats.completed), rate, "green", "✓"), unsafe_allow_html=True
    )
    c[2].markdown(
        stat_card("Pending Approval", str(stats.pending), "Requires action", "amber", "⏳"),
        unsafe_allow_html=True,
    )
    c[3].markdown(
        stat_card("Failed / Error", str(stats.failed), "Check logs", "red", "⚠"),
        unsafe_allow_html=True,
    )
    st.write("")
    left, mid, right = st.columns([1.1, 1.2, 1.2])
    with left.container(border=True):
        st.markdown(
            '<div class="panel-t">Market Overview '
            + pill("NSE", "blue")
            + " "
            + pill("BSE", "green")
            + " "
            + pill("US", "gray")
            + "</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="na">Index levels (NIFTY 50, SENSEX, USD/INR): not available '
            "— no index provider in the backend.</div>",
            unsafe_allow_html=True,
        )
        for name in ("Apple", "Reliance"):
            snap = safe(API.get, "/market/snapshot", q=name, default=None)
            price, cur, origin = latest_close(snap)
            series = close_series(snap)
            if price is None:
                st.markdown(
                    f'<div class="kf"><span>{name}</span><b>{NA}</b></div>', unsafe_allow_html=True
                )
                continue
            first = series[0][1] if series else price
            chg = (price - first) / first if first else 0.0
            tone = "s-green" if chg >= 0 else "s-red"
            st.markdown(
                f'<div class="kf"><span>{name}</span><b>{cur} {price:,.2f} '
                f'<span class="{tone}">{chg:+.2%}</span> {origin_pill(origin)}</b></div>',
                unsafe_allow_html=True,
            )
            if series:
                st.line_chart({"close": [v for _, v in series]}, height=70, color="#22D3A0")
    with mid.container(border=True):
        st.markdown(
            '<div class="panel-t">Recent Research</div>' + rr_list(workflow_rows(wfs)),
            unsafe_allow_html=True,
        )
    with right.container(border=True):
        name = (wfs and workflow_rows(wfs)[0]["Company"]) or "Apple"
        news = safe(API.get, "/news/events/snapshot", q=name, default=None) or {}
        events = (news.get("package") or {}).get("events") or []
        body = (
            "".join(
                f'<div class="rr"><div class="n"><b>{esc(ev.get("title"))}</b><small>'
                f"{esc(ev.get('event_date'))}</small></div></div>"
                for ev in events[:5]
            )
            or f'<div class="na">News for {esc(name)}: {NA}</div>'
        )
        st.markdown(
            f'<div class="panel-t">Top News · {esc(name)} {origin_pill(news.get("data_origin"))}'
            f"</div>{body}",
            unsafe_allow_html=True,
        )


def research_page() -> None:
    topbar("research")
    main, side = st.columns([2.2, 1])
    obj = ss.get("objective_pick", "")
    with main:
        st.markdown("#### Start New Research")
        search_box("research", "Search", objective=obj)
        st.markdown("#### Popular Companies")
        cols = st.columns([1, 1, 1, 1, 1.2, 2])
        for col, n in zip(cols, POPULAR_COMPANIES, strict=False):
            if col.button(n, key=f"popco_{n}"):
                try:
                    start_research(n, objective=obj)
                    go("Research Report" if "result" in ss else "Research")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
        st.markdown("#### Research Objective")
        keys = list(OBJECTIVE_LABELS)
        icons = ["🏢", "📈", "📊", "📰", "🏭", "⚖️", "🧠"]
        for row in (keys[:4], keys[4:]):
            cols = st.columns(4)
            for col, k in zip(cols, row, strict=False):
                picked = ss.get("objective_pick") == k
                with col.container(border=True, key=f"objc_{k}"):
                    st.markdown(
                        f'<div class="rr" style="border:none"><div class="tile t-blue" '
                        f'style="width:30px;height:30px;font-size:.85rem">{icons[keys.index(k)]}'
                        f'</div><div class="n"><b>{OBJECTIVE_LABELS[k]}</b><small>'
                        f"{OBJECTIVE_HINTS[k]}</small></div></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button(
                        "✓ Selected" if picked else "Select",
                        key=f"obj_{k}",
                        type="primary" if picked else "secondary",
                    ):
                        ss.objective_pick = "" if picked else k
                        st.rerun()
        st.caption("No objective selected = auto-detect from your question.")
        flash()
        if "intent" in ss and ("pending" in ss or "choices" in ss):
            used = ss.get("objective_used") or ss.intent.objective
            st.caption(f"Objective: **{objective_label(used)}** ({ss.intent.reason})")
        if "choices" in ss:
            st.warning("More than one company matches. Which one did you mean?")
            for i, ch in enumerate(ss.choices):
                if st.button(ch["label"], key=f"pick{i}"):
                    start_research(ss.question, pick=ch, objective=obj)
                    go("Research Report" if "result" in ss else "Research")
                    st.rerun()
        if "pending" in ss:
            p = ss.pending
            with st.container(border=True):
                st.warning(
                    f"⏸️ **Approval needed** — comprehensive research on "
                    f"**{p['company']}** calls several data sources."
                )
                a, r, _ = st.columns([1, 1, 4])
                if a.button("Approve", key="ap_inline"):
                    decide(p["workflow_id"], p["company"], "approved")
                    go("Research Report")
                    st.rerun()
                if r.button("Reject", key="rj_inline"):
                    decide(p["workflow_id"], p["company"], "rejected")
                    st.rerun()
        with st.container(border=True):
            st.markdown('<div class="panel-t">Recent Searches</div>', unsafe_allow_html=True)
            rows = workflow_rows(all_workflows())[:8]
            for r in rows:
                r["StatusH"] = status_pill(r["Status"])
            st.markdown(
                table(rows, ["Company", "Objective", "Created", "StatusH"], ["StatusH"]).replace(
                    "<th>StatusH</th>", "<th>Status</th>"
                )
                if rows
                else '<div class="na">No searches yet.</div>',
                unsafe_allow_html=True,
            )
    with side.container(border=True):
        st.markdown('<div class="panel-t">Suggested Companies</div>', unsafe_allow_html=True)
        for n in POPULAR_COMPANIES[:4]:
            res = safe(API.get, "/companies/resolve", q=n, default={}) or {}
            if res.get("status") != "RESOLVED":
                continue
            comp = res.get("company") or {}
            listings = res.get("listings") or comp.get("listings") or []
            tick = next(
                (
                    f"{x.get('ticker')} · {x.get('exchange')}"
                    for x in listings
                    if isinstance(x, dict) and x.get("ticker")
                ),
                "",
            )
            name = str(comp.get("display_name") or n)
            c1, c2 = st.columns([4, 1])
            c1.markdown(
                f'<div class="co" style="border:none"><div class="lg">{initials(name)}</div>'
                f'<div class="n"><b>{esc(name)}</b><small>{esc(tick)}</small></div></div>',
                unsafe_allow_html=True,
            )
            if c2.button("View", key=f"sug_{n}"):
                start_research(n, objective=obj)
                go("Research Report" if "result" in ss else "Research")
                st.rerun()


def tracker(workflow: dict[str, Any] | None) -> None:
    icons = {"done": "✓", "current": "⧗", "failed": "✕"}
    parts = []
    for i, (name, state) in enumerate(step_states(workflow)):
        sub = '<br><span class="sub">Awaiting approval</span>' if state == "current" else ""
        parts.append(
            f'<div class="step {state}"><div class="dot">{icons.get(state, i + 1)}'
            f"</div>{name}{sub}</div>"
        )
    st.markdown(f'<div class="steps">{"".join(parts)}</div>', unsafe_allow_html=True)


def approvals_page() -> None:
    topbar("approvals")
    st.markdown("#### Pending Approvals")
    st.caption("Review and approve research workflows before execution.")
    flash()
    wfs = all_workflows()
    groups = split_approvals(wfs)
    c = st.columns(4)
    c[0].markdown(
        stat_card("Pending", str(len(groups["pending"])), "", "amber", "⏳"), unsafe_allow_html=True
    )
    c[1].markdown(
        stat_card("Approved", str(len(groups["approved"])), "", "green", "✓"),
        unsafe_allow_html=True,
    )
    c[2].markdown(
        stat_card("Rejected", str(len(groups["rejected"])), "", "red", "✕"), unsafe_allow_html=True
    )
    c[3].markdown(
        stat_card("Total", str(len(wfs)), "all workflows", "blue", "▣"), unsafe_allow_html=True
    )
    st.write("")
    with st.container(border=True):
        st.markdown(
            f'<div class="panel-t">Pending Approval ({len(groups["pending"])})</div>',
            unsafe_allow_html=True,
        )
        cols = [2, 2, 1.3, 1.6, 1, 1]
        h = st.columns(cols)
        for col, t in zip(
            h, ["Company", "Objective", "Requested By", "Request Time", "", "Actions"], strict=True
        ):
            col.markdown(
                f'<div style="color:#9FB2D6;font-size:.8rem">{t}</div>', unsafe_allow_html=True
            )
        if not groups["pending"]:
            st.markdown(
                '<div class="na">Nothing is waiting. Comprehensive research requests '
                "appear here.</div>",
                unsafe_allow_html=True,
            )
        for w in groups["pending"]:
            wid = str(w["workflow_id"])
            row = workflow_rows([w])[0]
            a, b, c3, d, e, f = st.columns(cols)
            a.write(row["Company"])
            b.markdown(
                f'<span style="color:#7FB0FF">{esc(row["Objective"])}</span>',
                unsafe_allow_html=True,
            )
            c3.write("Dashboard")
            d.write(row["Created"])
            if e.button("Approve", key=f"apv_{wid}"):
                try:
                    decide(wid, row["Company"], "approved")
                    go("Research Report")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
            if f.button("Reject", key=f"rej_{wid}"):
                try:
                    decide(wid, row["Company"], "rejected")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
    left, right = st.columns([1.5, 1])
    with left.container(border=True):
        latest = (
            groups["pending"][:1]
            or sorted(wfs, key=lambda w: str(w.get("created_at") or ""), reverse=True)[:1]
        )
        sub = ""
        if latest:
            r0 = workflow_rows(latest)[0]
            sub = (
                f' <span class="s-mut" style="font-size:.75rem">· {esc(r0["Company"])} · '
                f"{esc(r0['Objective'])}</span>"
            )
        st.markdown(f'<div class="panel-t">Workflow Steps{sub}</div>', unsafe_allow_html=True)
        tracker(latest[0] if latest else None)
    with right.container(border=True):
        rows = workflow_rows(wfs)[:5]
        dots = {
            "Completed": "#22D3A0",
            "Pending approval": "#F0911F",
            "Rejected": "#FF4D6A",
            "Cancelled": "#FF4D6A",
            "Failed": "#FF4D6A",
        }
        items = (
            "".join(
                f'<div class="rr"><span style="width:9px;height:9px;border-radius:50%;background:'
                f'{dots.get(r["Status"], "#7FB0FF")};display:inline-block"></span><div class="n">'
                f'<b>{esc(r["Status"])} — {esc(r["Company"])}</b></div><span class="tm">'
                f"{esc(r['Created'][5:16])}</span></div>"
                for r in rows
            )
            or '<div class="na">No activity yet.</div>'
        )
        st.markdown(f'<div class="panel-t">Recent Activity</div>{items}', unsafe_allow_html=True)
    with st.expander("🪜 Step-by-step mode (approve every step)"):
        steps_mode()


def _rows_table(rows: list[dict[str, Any]]) -> None:
    st.markdown(
        table(
            [
                {
                    "Ref": r.get("tag"),
                    "Metric": r.get("label"),
                    "Value": r.get("value"),
                    "Detail": r.get("detail"),
                }
                for r in rows
            ],
            ["Ref", "Metric", "Value", "Detail"],
        ),
        unsafe_allow_html=True,
    )


def _items(rows: list[dict[str, Any]]) -> None:
    for r in rows[:10]:
        link = f" [link]({r['url']})" if r.get("url") else ""
        st.markdown(
            f"- **[{r.get('tag')}]** {r.get('date') or ''} — {r.get('title')} "
            f"{badge_markdown(r.get('data_origin'))}{link}"
        )


def report_page() -> None:
    topbar("report")
    reports: dict[str, Any] = ss.get("reports", {})
    if not reports:
        st.markdown("#### Research Report")
        st.info(
            "No report in this session yet. Run research from Home or Research, or open "
            "one from the Reports page."
        )
        return
    wid = ss.get("selected_report") or ss.get("result", {}).get("workflow_id")
    if wid not in reports:
        wid = list(reports)[-1]
    res = reports[wid]
    view, wf = res["view"], res["workflow"]
    raw = res.get("raw")
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    findings = data.get("findings") or {}
    by = {s.title: s for s in view.sections}
    origins = {s.origin for s in view.sections if s.origin}
    head, dl = st.columns([4, 1])
    head.markdown(
        '<div style="font-size:.75rem;color:#8FA3C7">Research &rsaquo; Reports &rsaquo; '
        f"{esc(view.company)}"
        f'</div><div style="font-size:1.35rem;font-weight:700;color:#fff">{esc(view.company)} '
        + status_pill(str(wf.get("status", "?")).capitalize())
        + " "
        + " ".join(origin_pill(o) for o in sorted(origins))
        + f'</div><div style="font-size:.75rem;color:#8FA3C7">'
        f"{esc(objective_label(wf.get('objective')))} · workflow {wid[:8]} · "
        f"{esc(wf.get('evidence_count', '-'))} evidence items</div>",
        unsafe_allow_html=True,
    )
    dl.download_button(
        "⬇ Download Report",
        res["markdown"],
        file_name=f"AERP-{view.company.replace(' ', '_')}.md",
        mime="text/markdown",
        type="primary",
        use_container_width=True,
    )
    tabs = st.tabs(list(REPORT_TABS))
    with tabs[0]:
        c1, c2, c3 = st.columns([1, 1.5, 1])
        mrows = findings.get("market_figures") or []
        frows = findings.get("financial_figures") or []
        with c1.container(border=True):
            kf = "".join(
                f'<div class="kf"><span>{esc(label)}</span><b>'
                f"{esc(figure_value(mrows, label) or figure_value(frows, label) or NA)}</b></div>"
                for label in (
                    "Last close",
                    "Simple return",
                    "SMA (simple moving average)",
                    "Revenue growth",
                    "Net margin",
                    "Debt to equity",
                    "Free cash flow",
                )
            )
            st.markdown(f'<div class="panel-t">Key Figures</div>{kf}', unsafe_allow_html=True)
        with c2.container(border=True):
            snap = safe(API.get, "/market/snapshot", q=view.company.split(" (")[0], default=None)
            series = close_series(snap)
            st.markdown(
                '<div class="panel-t">Price Performance '
                + origin_pill((snap or {}).get("data_origin"))
                + "</div>",
                unsafe_allow_html=True,
            )
            pairs = revenue_profit_series(frows)
            if pairs:
                st.altair_chart(revenue_chart(pairs), use_container_width=True)
            elif series:
                st.altair_chart(price_chart(series), use_container_width=True)
            else:
                st.markdown(f'<div class="na">Price history: {NA}</div>', unsafe_allow_html=True)
        with c3.container(border=True):
            news = findings.get("news_items") or []
            body = (
                "".join(
                    f'<div class="rr"><div class="n"><b>{esc(n.get("title"))}</b><small>'
                    f"{esc(n.get('date'))} {origin_pill(n.get('data_origin'))}</small></div></div>"
                    for n in news[:4]
                )
                or f'<div class="na">{NA}</div>'
            )
            st.markdown(f'<div class="panel-t">Top News</div>{body}', unsafe_allow_html=True)
        a1, a2 = st.columns([2.5, 1])
        with a1.container(border=True):
            st.markdown(
                f'<div class="panel-t">Analyst Summary <span class="s-mut" '
                f'style="font-size:.72rem">by {esc(view.summary_by)}</span></div>',
                unsafe_allow_html=True,
            )
            st.write(view.summary)
            if view.summary_note:
                st.caption(view.summary_note + " Not investment advice.")
        with a2.container(border=True):
            refs = evidence_refs(data)
            body = (
                "".join(
                    f'<div class="rr"><div class="ic">📑</div><div class="n"><b>'
                    f"{esc(r['Capability']).title()}</b><small>{esc(r['Retrieved'])}</small></div>"
                    f"{origin_pill(r['Origin'])}</div>"
                    for r in refs[:4]
                )
                or f'<div class="na">{NA}</div>'
            )
            st.markdown(
                f'<div class="panel-t">Evidence Citations</div>{body}', unsafe_allow_html=True
            )
    with tabs[1]:
        sec = by.get("Financials")
        if sec and sec.rows:
            st.markdown(badge_markdown(sec.origin, sec.status))
            _rows_table(sec.rows)
        else:
            st.caption(f"Financials: {NA} for this objective.")
        msec = by.get("Market")
        if msec and msec.rows:
            st.markdown("**Market** " + badge_markdown(msec.origin, msec.status))
            _rows_table(msec.rows)
    with tabs[2]:
        sec = by.get("Top news")
        if sec and sec.rows:
            _items(sec.rows)
        else:
            st.caption(f"News: {NA}.")
    with tabs[3]:
        st.caption(
            f"Industry analysis section: {NA} in this report (run the Industry Analysis objective)."
        )
    with tabs[4]:
        sec = by.get("Regulatory")
        if sec and sec.rows:
            _items(sec.rows)
        else:
            st.caption(f"Regulatory: {NA}.")
    with tabs[5]:
        st.markdown(res["markdown"])
    with tabs[6]:
        refs = evidence_refs(data)
        if refs:
            st.markdown(table(refs, list(refs[0])), unsafe_allow_html=True)
        else:
            st.caption(f"Evidence references: {NA}.")


def reports_page() -> None:
    topbar("reports")
    st.markdown("#### Research Reports")
    st.caption("Search, filter and export your research reports.")
    flash()
    rows = workflow_rows(all_workflows())
    c1, c2, c3, c4 = st.columns([3, 2, 2, 1.2])
    text = c1.text_input("Search", placeholder="Search reports…", label_visibility="collapsed")
    company = c2.selectbox(
        "Company", ["All", *sorted({r["Company"] for r in rows})], label_visibility="collapsed"
    )
    objective = c3.selectbox(
        "Objective", ["All", *sorted({r["Objective"] for r in rows})], label_visibility="collapsed"
    )
    shown = filter_reports(rows, text, company, objective)
    c4.download_button(
        "⬇ Export All",
        to_csv(shown) or "no rows\n",
        type="primary",
        file_name="aerp-reports.csv",
        mime="text/csv",
        disabled=not shown,
        use_container_width=True,
    )
    reports = ss.get("reports", {})
    with st.container(border=True):
        cols = [1.6, 1.6, 2, 1.3, 1, 1]
        h = st.columns(cols)
        for col, t in zip(
            h, ["Date", "Company", "Objective", "Status", "Format", "Action"], strict=True
        ):
            col.markdown(
                f'<div style="color:#9FB2D6;font-size:.8rem">{t}</div>', unsafe_allow_html=True
            )
        if not shown:
            st.markdown('<div class="na">No reports match.</div>', unsafe_allow_html=True)
        for r in shown:
            a, b, c, d, e, f = st.columns(cols)
            a.write(r["Created"][:16])
            b.write(r["Company"])
            c.markdown(
                f'<span style="color:#7FB0FF">{esc(r["Objective"])}</span>', unsafe_allow_html=True
            )
            d.markdown(status_pill(r["Status"]), unsafe_allow_html=True)
            e.write("MD / JSON")
            wid = r["workflow_id"]
            if wid in reports:
                if f.button("View", key=f"view_{wid}"):
                    ss.selected_report = wid
                    go("Research Report")
                    st.rerun()
            elif r["Status"] in ("Completed", "Partial"):
                if f.button("Open", key=f"open_{wid}"):
                    try:
                        open_report(wid, r["Company"])
                        go("Research Report")
                    except ApiError as exc:
                        ss.error = f"Report not available ({exc.message})"
                    st.rerun()
            elif r["Status"] == "Pending approval":
                if f.button("Review", key=f"rev_{wid}"):
                    go("Workflow Approvals")
                    st.rerun()
            else:
                f.write("-")


def watchlist_page() -> None:
    topbar("watchlist")
    h1, h2 = st.columns([3, 2])
    h1.markdown("#### My Watchlist")
    h1.caption(
        "Stored locally on this computer. Prices from /market/snapshot with a LIVE / SAMPLE badge."
    )
    with h2, st.form("wl", border=False):
        c1, c2 = st.columns([3, 1.3])
        name = c1.text_input(
            "Add", placeholder="Company name or ticker", label_visibility="collapsed"
        )
        if c2.form_submit_button("+ Add to Watchlist", type="primary") and name.strip():
            add_to_watchlist(name)
            st.rerun()
    items = load_watchlist()
    if not items:
        st.info("Your watchlist is empty. Add Reliance, TCS or Apple to start.")
        return
    with st.container(border=True):
        cols = [2.2, 1.6, 1.6, 1.2, 1.6, 1, 1]
        h = st.columns(cols)
        for col, t in zip(
            h, ["Company", "Ticker", "Last Price", "Change", "Source", "", ""], strict=True
        ):
            if t:
                col.markdown(
                    f'<div style="color:#9FB2D6;font-size:.8rem">{t}</div>', unsafe_allow_html=True
                )
        for it in items:
            snap = safe(API.get, "/market/snapshot", q=it, default=None)
            price, cur, origin = latest_close(snap)
            series = close_series(snap)
            listing = (snap or {}).get("listing") or {}
            c = st.columns(cols)
            c[0].markdown(
                f'<div class="co" style="border:none;padding:0"><div class="lg" '
                f'style="width:28px;height:28px;font-size:.7rem">{initials(it)}</div>'
                f'<div class="n"><b>{esc(it)}</b></div></div>',
                unsafe_allow_html=True,
            )
            c[1].write(
                f"{listing.get('ticker', '')} · {listing.get('exchange', '')}" if listing else NA
            )
            c[2].write(f"{cur} {price:,.2f}" if price is not None else NA)
            if price is not None and len(series) > 1 and series[0][1]:
                chg = (price - series[0][1]) / series[0][1]
                tone = "s-green" if chg >= 0 else "s-red"
                c[3].markdown(f'<span class="{tone}">{chg:+.2%}</span>', unsafe_allow_html=True)
            else:
                c[3].write(NA)
            c[4].markdown(
                origin_pill(origin) if price is not None else pill("NOT AVAILABLE", "gray"),
                unsafe_allow_html=True,
            )
            if c[5].button("Research", key=f"wr_{it}"):
                start_research(it)
                go("Research Report" if "result" in ss else "Research")
                st.rerun()
            if c[6].button("Remove", key=f"wd_{it}"):
                remove_from_watchlist(it)
                st.rerun()


def health_page() -> None:
    topbar("health")
    st.markdown("#### Provider Health")
    st.caption(
        "Status from /health/providers. Latency is measured by the dashboard for local "
        "services; provider latency is not reported by the backend."
    )
    c = st.columns(4)
    checks = [
        ("REST API", API, "/health"),
        ("Readiness", API, "/ready"),
        ("Control API", CONTROL, "/control/health"),
        ("Version", API, "/version"),
    ]
    for col, (label, client, path) in zip(c, checks, strict=True):
        t0 = time.perf_counter()
        ok = safe(client.get, path, default=None) is not None
        ms = (time.perf_counter() - t0) * 1000
        tone = "s-green" if ok else "s-red"
        col.markdown(
            f'<div class="hc"><div class="l">{label}</div><div class="v {tone}">'
            f'{"Healthy" if ok else "Down"}</div><div class="s">'
            f"{f'{ms:.0f} ms' if ok else 'not reachable'}</div></div>",
            unsafe_allow_html=True,
        )
    st.write("")
    left, right = st.columns([1.5, 1])
    with left.container(border=True):
        cards = provider_cards(safe(API.get, "/health/providers", default={}))
        tone = {"ok": "green", "degraded": "amber", "failing": "red"}
        rows = [
            {
                "Provider": x["name"],
                "Status": pill(x["state"].capitalize(), tone.get(x["state"], "gray")),
                "Successes": int(x["successes"] or 0),
                "Failures": int(x["failures"] or 0),
                "Last success": x["last_success"],
                "Latency": NA if x["latency_ms"] is None else f"{x['latency_ms']:.0f} ms",
            }
            for x in cards
        ]
        body = (
            table(rows, list(rows[0]), ["Status"])
            if rows
            else '<div class="na">No live provider called yet (offline sample mode). Turn on '
            "live data in Settings.</div>"
        )
        st.markdown(f'<div class="panel-t">Provider Status</div>{body}', unsafe_allow_html=True)
    with right.container(border=True):
        logs: list[str] = []
        if CONTROL.token:
            runs = (safe(CONTROL.get, "/control/runs", default={}) or {}).get("runs", [])
            if runs:
                rid = runs[-1].get("run_id")
                lg = safe(CONTROL.get, f"/control/runs/{rid}/logs", default={}) or {}
                logs = [
                    f"{x.get('level', 'info'):5} {x.get('message')}"
                    for x in lg.get("logs", [])[-12:]
                ]
        st.markdown('<div class="panel-t">Recent Logs</div>', unsafe_allow_html=True)
        st.code("\n".join(logs) or "(no step-by-step run logs yet)")
        ver = safe(API.get, "/version", default=None)
        if ver:
            st.json(ver, expanded=False)


def steps_mode() -> None:
    st.markdown("#### 🪜 Step-by-step mode")
    st.caption("Every research step waits for you: Approve, Reject or Retry, with live logs.")
    if not CONTROL.token:
        st.error("Control API token missing - start with scripts/start-dashboard.ps1.")
        return
    try:
        CONTROL.get("/control/health")
    except ApiError as exc:
        st.error(f"Control API not reachable ({exc.message}).")
        return
    with st.form("steprun"):
        q = st.text_input("Company", value="Reliance")
        objective = st.selectbox(
            "What to research", list(OBJECTIVE_LABELS), index=6, format_func=objective_label
        )
        start = st.form_submit_button("▶️ Start step-by-step run", type="primary")
    try:
        if start:
            CONTROL.put(
                "/control/settings", {"approval_mode": True, "auto_approve_low_risk": False}
            )
            run = CONTROL.post("/control/runs", {"params": {"q": q, "objective": objective}})
            ss.run_id = run["run_id"]
        if "run_id" not in ss:
            return
        run = CONTROL.get(f"/control/runs/{ss.run_id}")
        st.markdown(f"**Run** `{ss.run_id[:8]}` · status **{run['status']}**")
        for step in run.get("steps", []):
            sid, state = step["step_id"], step["state"]
            icon = {
                "pending_approval": "⏸️",
                "succeeded": "✅",
                "failed": "❌",
                "rejected": "🚫",
                "running": "⏳",
            }.get(state, "•")
            with st.container(border=True):
                st.markdown(
                    f"{icon} **{step.get('kind')}** - {step.get('name') or ''} "
                    f"· `{state}` · risk {step.get('risk', '?')}"
                )
                out = step.get("output") or {}
                if out.get("summary"):
                    st.caption(str(out["summary"]))
                if step.get("error"):
                    st.caption(f"error: {step['error']}")
                c1, c2, c3, _ = st.columns([1, 1, 1, 4])
                if state == "pending_approval":
                    if c1.button("Approve", key=f"a{sid}", type="primary"):
                        CONTROL.post(f"/control/runs/{ss.run_id}/steps/{sid}/approve", {})
                        st.rerun()
                    if c2.button("Reject", key=f"r{sid}"):
                        CONTROL.post(f"/control/runs/{ss.run_id}/steps/{sid}/reject", {})
                        st.rerun()
                if state in ("failed", "rejected") and c3.button("Retry", key=f"t{sid}"):
                    CONTROL.post(f"/control/runs/{ss.run_id}/steps/{sid}/retry", {})
                    st.rerun()
        logs = CONTROL.get(f"/control/runs/{ss.run_id}/logs").get("logs", [])
        st.markdown("**Live log**")
        st.code(
            "\n".join(f"{x.get('level', 'info'):5} {x.get('message')}" for x in logs[-40:])
            or "(no logs yet)"
        )
        if run["status"] in ("running", "queued"):
            time.sleep(1.5)
            st.rerun()
    except ApiError as exc:
        st.error(exc.message)


def settings_page() -> None:
    topbar("settings")
    st.markdown("#### Settings")
    st.caption("Configure providers, models, security and preferences.")
    t1, t2, t3, t4 = st.tabs(["Providers", "Model Settings", "Security", "Preferences"])
    masked: dict[str, Any] = {}
    try:
        masked = CONTROL.get("/control/keys").get("keys", {}) if CONTROL.token else {}
    except ApiError as exc:
        t1.warning(f"Key store unavailable ({exc.message}).")
    with t1:
        st.markdown("**Provider configuration** (guided setup)")
        rows = [
            {
                "Provider": p.label,
                "Status": ("✅ " + str(masked.get(p.key_name, {}).get("masked", "")))
                if masked.get(p.key_name, {}).get("configured")
                else "⚪ not configured",
                "Used by backend": "yes" if p.used_by_backend else "stored only",
            }
            for p in PROVIDERS
        ]
        for r in rows:
            r["StatusH"] = (
                pill("Configured", "green")
                if str(r["Status"]).startswith("✅")
                else pill("Not configured", "amber")
            )
        st.markdown(
            table(rows, ["Provider", "StatusH", "Used by backend"], ["StatusH"]).replace(
                "<th>StatusH</th>", "<th>Status</th>"
            ),
            unsafe_allow_html=True,
        )
        spec = st.selectbox("Provider", PROVIDERS, format_func=lambda p: p.label)
        if spec.signup_url:
            st.caption(f"1. Get a key at {spec.signup_url}  2. Paste it below  3. Test  4. Save")
        if not spec.used_by_backend:
            st.info(
                "Saved for later. Research summaries currently use OpenRouter free "
                "models only (project cost policy)."
            )
        base_url = (
            st.text_input("Base URL", placeholder="https://your-server/v1")
            if spec.needs_base_url
            else ""
        )
        key = st.text_input(f"API key ({spec.key_hint})", type="password", key=f"k_{spec.key}")
        c1, c2 = st.columns(2)
        if c1.button("🔌 Test connection", disabled=not key):
            try:
                ok, msg = probe(*connection_test_request(spec, key.strip(), base_url))
            except ValueError as exc:
                ok, msg = False, str(exc)
            (st.success if ok else st.error)(msg)
        if c2.button("💾 Save", type="primary", disabled=not key):
            try:
                saved = CONTROL.put(f"/control/keys/{spec.key_name}", {"value": key.strip()})
                st.success(f"Saved {saved['masked']} - used from the next research run.")
            except ApiError as exc:
                st.error(exc.message)
    with t2:
        st.markdown("**Model selection** (automatic, lowest cost)")
        task = st.radio("Task", ["summary", "planning", "report"], horizontal=True)
        if st.button("Show which model would be used"):
            try:
                models = JsonClient("https://openrouter.ai/api/v1", timeout=20).get("/models")
                choice = choose_model(models.get("data", []), task)
                (st.success if choice.model_id else st.warning)(
                    f"**{choice.model_id}** - {choice.reason}" if choice.model_id else choice.reason
                )
            except ApiError as exc:
                st.error(f"Could not load the model list: {exc.message}")
        st.caption(
            "Rule: free models first. Paid models are OFF by project policy "
            "(ALLOW_PAID_MODELS=false); enabling a paid fallback needs your decision."
        )
    with t3:
        st.markdown("**Security & keys**")
        st.markdown(
            "- Keys are write-only: only the last 4 characters are ever shown.\n"
            "- The control API listens on 127.0.0.1 only, with a random per-session "
            "token.\n- Comprehensive research always needs your approval."
        )
        cfg = safe(CONTROL.get, "/control/settings", default=None) if CONTROL.token else None
        if cfg:
            st.json(cfg, expanded=False)
    with t4:
        st.markdown("**Live data sources**")
        prefs = load_prefs()
        with st.form("live"):
            values: dict[str, object] = {
                k: st.toggle(label, value=bool(prefs.get(k)))
                for k, (_, label) in LIVE_TOGGLES.items()
            }
            values["sec_user_agent"] = st.text_input(
                "SEC contact (name + email, required by SEC for live US filings)",
                value=str(prefs.get("sec_user_agent", "")),
            )
            if st.form_submit_button("Save live-data settings"):
                save_prefs(values)
                st.success("Saved. Restart the dashboard (start-dashboard.ps1) to apply.")
        st.caption(
            "Off = labelled SAMPLE DATA (works offline). On = real data from public sources."
        )


# ---------------------------------------------------------------- shell
PAGES = {
    "Home": ("home", home_page),
    "Research": ("research", research_page),
    "Workflow Approvals": ("approvals", approvals_page),
    "Research Report": ("report", report_page),
    "Reports": ("reports", reports_page),
    "Watchlist": ("watchlist", watchlist_page),
    "Provider Health": ("health", health_page),
    "Settings": ("settings", settings_page),
}
ss.setdefault("nav", "Home")
with st.sidebar:
    st.markdown(logo_html() + NAV_ICON_CSS, unsafe_allow_html=True)
    pending_n = workflow_stats(all_workflows()).pending
    for name, (slug, _) in PAGES.items():
        badge = f"  :red-badge[{pending_n}]" if name == "Workflow Approvals" and pending_n else ""
        if st.button(
            f"{name}{badge}",
            key=f"nav_{slug}",
            use_container_width=True,
            type="primary" if ss.nav == name else "secondary",
        ):
            ss.nav = name
            st.rerun()
    st.markdown(SIDE_FOOT, unsafe_allow_html=True)

PAGES[ss.nav][1]()
