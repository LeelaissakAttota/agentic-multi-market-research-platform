"""AMRP dashboard v2: dark navy theme, sidebar navigation, real data only.

Run:  streamlit run dashboard/app.py   (or .\\scripts\\start-dashboard.ps1 on Windows)
Talks only to the local REST API (AMRP_API_URL) and control API (AMRP_CONTROL_URL).
Every number on screen comes from those APIs or the local run history; anything the
backend does not provide is shown as "not available" or with a SAMPLE badge.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from agents_backtest import backtest_page  # type: ignore[import-not-found]
from agents_debate import (  # type: ignore[import-not-found]
    deliberation_tab,
    ensure_deliberation,
    rating_badge,
)
from agents_memory import memory_page  # type: ignore[import-not-found]
from agents_ui import agent_llm_settings  # type: ignore[import-not-found]
from theme import (  # type: ignore[import-not-found]  # dashboard/theme.py
    CSS,
    NAV_ICON_CSS,
    agent_grid,
    asset_uri,
    esc,
    initials,
    logo_html,
    pill,
    stat_card,
    status_pill,
    table,
)

from amrp.application.deliberation import append_to_report
from amrp.dashboard import commodity as cm
from amrp.dashboard import market_live as ml
from amrp.dashboard import markets as mk
from amrp.dashboard.client import (
    ApiError,
    JsonClient,
    candidate_choices,
    probe,
    probe_json,
    resolve_company,
)
from amrp.dashboard.intent import parse_intent
from amrp.dashboard.prefs import (
    LIVE_DESCRIPTIONS,
    LIVE_TOGGLES,
    OPENROUTER_BASE,
    is_paid_model,
    load_prefs,
    save_model_choice,
    save_prefs,
    uses_custom_router,
)
from amrp.dashboard.providers import (
    PROVIDERS,
    choose_model,
    connection_test_request,
    detect_provider,
    models_request,
    parse_model_ids,
)
from amrp.dashboard.report_export import (
    markdown_to_html,
    markdown_to_pdf,
    pdf_available,
    report_filename,
)
from amrp.dashboard.report_view import (
    badge_markdown,
    build_report_view,
    origin_badge,
)
from amrp.dashboard.views import (
    OBJECTIVE_HINTS,
    OBJECTIVE_LABELS,
    REPORT_TABS,
    RESEARCH_AGENTS,
    STEP_MODE_HELP,
    add_to_watchlist,
    agent_states,
    analyst_rows,
    close_series,
    evidence_refs,
    figure_value,
    filter_reports,
    history_as_workflows,
    latest_close,
    latest_run_logs,
    load_history,
    load_saved_report,
    load_watchlist,
    merge_workflows,
    model_options,
    objective_label,
    pending_steps,
    provider_cards,
    record_history,
    remove_from_watchlist,
    revenue_profit_series,
    save_report,
    split_approvals,
    step_mode_company,
    step_states,
    to_csv,
    workflow_rows,
    workflow_stats,
)
from amrp.envcompat import env

NEWS_HEIGHT = 470  # px: news column = price box + AI box beside it (scrolls inside)
API = JsonClient(env("AMRP_API_URL", "http://127.0.0.1:8000"))
CONTROL = JsonClient(
    env("AMRP_CONTROL_URL", "http://127.0.0.1:8712"),
    token=env("AMRP_CONTROL_TOKEN", ""),
    timeout=30,
)
RUN_TIMEOUT = float(env("AMRP_RUN_TIMEOUT", "120"))
RUNNER = JsonClient(API.base_url, timeout=RUN_TIMEOUT)
NA = "not available"

st.set_page_config(
    page_title="AMRP - Agentic Multi-Market Research Platform", page_icon="📈", layout="wide"
)
ss = st.session_state
IST = timezone(timedelta(hours=5, minutes=30))
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
def start_research(
    question: str,
    pick: dict[str, str] | None = None,
    objective: str = "",
    auto_approve: bool = False,
) -> None:
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
    if created.get("approval_status") == "pending" and auto_approve:
        API.post(
            f"/research/workflows/{created['workflow_id']}/approval",
            {"decision": "approved", "note": "approved via step-by-step run in dashboard"},
        )
    elif created.get("approval_status") == "pending":
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
        try:
            executed = RUNNER.post(f"/research/workflows/{workflow_id}/execute")
        except ApiError as exc:
            record_history(
                {
                    "workflow_id": workflow_id,
                    "company": company,
                    "objective": ss.get("objective_used"),
                    "status": "failed",
                    "approval_status": "approved",
                }
            )
            raise ApiError(
                exc.status,
                f"Research for {company} did not finish ({exc.message}). Live sources may be "
                "offline — check your connection or turn off live data in Settings, then retry.",
            ) from None
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
    ss.selected_report = workflow_id
    save_report(workflow_id, {**ss.result, "company": company})
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
    """Render a stored report: the API first, else the local copy saved when it finished."""

    try:
        result = _open_report_api(workflow_id, company)
    except ApiError:
        saved = load_saved_report(workflow_id)
        if saved is None:
            raise
        result = {**saved, "view": build_report_view(saved["raw"], company)}
    ss.reports = {**ss.get("reports", {}), workflow_id: result}
    ss.selected_report = workflow_id


def _open_report_api(workflow_id: str, company: str) -> dict[str, Any]:
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
    save_report(workflow_id, {**result, "company": company})
    return result


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
        sent = c2.form_submit_button(button, type="primary", width="stretch")
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
        if col.button(label, key=f"{key}_{label}", width="stretch"):
            try:
                start_research(prompt, objective=objective)
                go("Research Report" if "result" in ss else "Research")
            except ApiError as exc:
                ss.error = exc.message
            st.rerun()


# ---------------------------------------------------------------- layout pieces
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


# ---------------------------------------------------------------- live market (Yahoo public)
@st.cache_data(ttl=900, show_spinner=False)
def live_indices() -> dict[str, list[ml.Daily]]:
    return {
        "US": [d for s, n in ml.US_INDICES if (d := ml.fetch_daily(s, n))],
        "IN": [d for s, n in ml.IN_INDICES if (d := ml.fetch_daily(s, n))],
    }


@st.cache_data(ttl=900, show_spinner=False)
def live_trending() -> dict[str, list[ml.Daily]]:
    return {"US": ml.trending_us(5), "IN": ml.most_active_india(5)}


@st.cache_data(ttl=900, show_spinner=False)
def live_news(symbols: tuple[str, ...]) -> list[ml.NewsItem]:
    return ml.news_one_per_company(list(symbols), 5)


def _ist(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso).astimezone(IST)
    except ValueError:
        return iso or "?"
    return when.strftime("%d %b %H:%M IST")


def _axis_fmt(hi: float) -> str:
    return ",.2f" if hi < 100 else (",.0f" if hi < 100_000 else "~s")


def area_chart(d: ml.Daily, height: int = 150, month: bool = False) -> Any:
    """Smooth line + gradient fill; green when the period change is up, red when down."""

    up = (d.change or 0) >= 0
    color = "#22D3A0" if up else "#FF4D6A"
    df = pd.DataFrame([{"Day": b.date[5:], "Close": b.close} for b in d.bars])
    lo, hi = float(df.Close.min()), float(df.Close.max())
    pad = (hi - lo) * 0.25 or hi * 0.01
    x = alt.X("Day:O", title=None, axis=alt.Axis(labelAngle=0))
    if month:
        df["Day"] = pd.to_datetime([b.date for b in d.bars])
        x = alt.X("Day:T", title=None, axis=alt.Axis(format="%d %b", labelAngle=0, tickCount=6))
    y = alt.Y(
        "Close:Q",
        title=None,
        scale=alt.Scale(domain=[lo - pad, hi + pad], nice=False),
        axis=alt.Axis(format=_axis_fmt(hi) if month else "~s", tickCount=4),
    )
    grad = alt.Gradient(
        gradient="linear",
        stops=[
            alt.GradientStop(color=color, offset=0),
            alt.GradientStop(color="rgba(0,0,0,0)", offset=1),
        ],
        x1=1,
        x2=1,
        y1=0,
        y2=1,
    )
    base = alt.Chart(df).encode(
        x=x, y=y, tooltip=[alt.Tooltip("Day", title="Day"), alt.Tooltip("Close:Q", format=",.2f")]
    )
    chart = alt.layer(
        base.encode(y2=alt.datum(lo - pad)).mark_area(
            interpolate="monotone",
            line={"color": color, "strokeWidth": 2},
            color=grad,
            opacity=0.55,
        ),
        base.mark_point(filled=True, size=28 if not month else 14, color=color),
    )
    return _style(chart).properties(
        height=height, padding={"left": 12, "right": 6, "top": 4, "bottom": 2}
    )


def _index_card(d: ml.Daily) -> None:
    chg = d.change
    tone = "s-green" if (chg or 0) >= 0 else "s-red"
    pct = f'<span class="{tone}">{chg:+.2%}</span>' if chg is not None else ""
    with st.container(border=True, key=f"idx_{d.symbol.strip('^')}"):
        st.markdown(
            f'<div class="idx-h"><b>{esc(d.name)}</b><span>{d.bars[-1].close:,.2f} {pct} '
            f"<small>5d</small></span></div>",
            unsafe_allow_html=True,
        )
        st.altair_chart(area_chart(d), width="stretch")


def market_overview() -> None:
    data = live_indices()
    stamp = max((d.as_of for v in data.values() for d in v), default="")
    st.markdown(
        '<div class="panel-t">Market Overview '
        + (
            pill("LIVE", "green") + f' <small style="color:#8FA3C7">as of {_ist(stamp)}</small>'
            if stamp
            else pill("NOT AVAILABLE", "gray")
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(2)
    for col, (region, label) in zip(
        cols, (("US", "US market"), ("IN", "Indian market")), strict=True
    ):
        items = data.get(region) or []
        with col.container(border=True, key=f"mkt_{region}"):
            st.markdown(f'<div class="sub-t">{label}</div>', unsafe_allow_html=True)
            if not items:
                st.markdown(
                    f'<div class="na">{label} data: {NA} (Yahoo Finance unreachable)</div>',
                    unsafe_allow_html=True,
                )
                continue
            inner = st.columns(len(items))
            for ic, d in zip(inner, items, strict=True):
                with ic:
                    _index_card(d)
    st.caption("Daily closes, last 5 trading days · source: Yahoo Finance (cached 15 min)")


@st.cache_data(ttl=3600, show_spinner=False)
def watch_ticker(name: str) -> str:
    """Ticker for a watchlist name via the backend resolver ('' when unresolved)."""

    res = safe(API.get, "/companies/resolve", q=name, default={}) or {}
    comp = res.get("company") or {}
    for x in res.get("listings") or comp.get("listings") or []:
        if isinstance(x, dict) and x.get("ticker"):
            return str(x["ticker"])
    return ""


def top_news() -> None:
    watch = [watch_ticker(n) for n in load_watchlist()]
    trend = live_trending()
    symbols = tuple(
        s for s in [*watch, *(d.symbol for d in trend.get("US", [])), *ml.NEWS_FALLBACK] if s
    )
    items = live_news(symbols)
    body = (
        "".join(
            f'<div class="rr"><div class="n"><b><a href="{esc(n.link)}" target="_blank" '
            f'style="color:#fff;text-decoration:none">{esc(n.title)}</a></b>'
            f"<small>{esc(n.symbol)} · {esc(n.source)} · {_ist(n.published)}</small></div></div>"
            for n in items
        )
        or f'<div class="na">News: {NA} (news feed unreachable)</div>'
    )
    st.markdown(
        '<div class="panel-t">Top News '
        + (pill("LIVE", "green") if items else "")
        + "</div>"
        + body,
        unsafe_allow_html=True,
    )


SUGGESTED_REGIONS = (
    ("US", "US &ndash; Top trending", "Yahoo Finance trending tickers"),
    ("IN", "India &ndash; Top trending", "most active by traded value among NIFTY large caps"),
)


def _suggested_list(region: str, items: list[ml.Daily], obj: str) -> None:
    if not items:
        st.markdown(f'<div class="na">{NA}</div>', unsafe_allow_html=True)
    for d in items:
        chg = d.change
        tone = "s-green" if (chg or 0) >= 0 else "s-red"
        pct = f'<span class="{tone}">{chg:+.2%}</span>' if chg is not None else ""
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.markdown(
            f'<div class="co" style="border:none"><div class="lg">{initials(d.name)}</div>'
            f'<div class="n"><b>{esc(d.name)}</b><small>{esc(d.symbol)} · {d.currency} '
            f"{d.bars[-1].close:,.2f} {pct}</small></div></div>",
            unsafe_allow_html=True,
        )
        if c2.button("View", key=f"sug_{region}_{d.symbol}"):
            try:
                start_research(d.symbol, objective=obj)
                go("Research Report" if "result" in ss else "Research")
            except ApiError as exc:
                ss.error = exc.message
            st.rerun()


def suggested_companies(obj: str) -> None:
    """Full-width 'Suggested Companies': US and India top-5 in two side-by-side boxes."""

    trend = live_trending()
    st.markdown('<div class="panel-t">Suggested Companies</div>', unsafe_allow_html=True)
    cols = st.columns(2)
    for col, (region, title, note) in zip(cols, SUGGESTED_REGIONS, strict=True):
        with col.container(border=True, key=f"sugbox_{region}"):
            st.markdown(
                f'<div class="sub-t">{title} {pill("LIVE", "green") if trend.get(region) else ""}'
                '</div><div style="font-size:.72rem;color:#8FA3C7;margin-bottom:.2rem">'
                f"{note}</div>",
                unsafe_allow_html=True,
            )
            _suggested_list(region, trend.get(region) or [], obj)


# ---------------------------------------------------------------- pages
def home_page() -> None:
    globe = asset_uri("hero-globe.png")
    img = f'<img src="{globe}" alt="">' if globe else ""
    st.markdown(
        f'<div class="hero">{img}<h1>Welcome to AMRP</h1><h3>Agentic Multi-Market Research '
        "Platform</h3><p>Evidence-backed research for Indian &amp; US equities, commodities, "
        "forex and crypto."
        "</p></div>",
        unsafe_allow_html=True,
    )
    with st.container(key="herorow"):
        search_box("home")
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
    with st.container(border=True):
        market_overview()
    mid, right = st.columns([1, 1.25])
    with mid.container(border=True):
        st.markdown(
            '<div class="panel-t">Recent Research</div>' + rr_list(workflow_rows(wfs)),
            unsafe_allow_html=True,
        )
    with right.container(border=True):
        top_news()


def objective_rows(keys: list[str]) -> list[list[str]]:
    """Objective cards: four on the first row, the rest on a centred second row."""

    return [keys[:4], keys[4:]] if len(keys) > 4 else [keys]


def research_page() -> None:
    obj = ss.get("objective_pick", "")
    st.markdown("#### Start New Research")
    search_box("research", "Search", objective=obj)
    st.write("")
    suggested_companies(obj)
    st.write("")
    with st.container():
        st.markdown("#### Research Objective")
        keys = list(OBJECTIVE_LABELS)
        icons = ["🏢", "📈", "📊", "📰", "🏭", "⚖️", "🧠"]
        for row in objective_rows(keys):
            # row 2 (3 cards) is centred under row 1 with the same card width
            cols = st.columns(4) if len(row) == 4 else st.columns([1, 2, 2, 2, 1])[1:4]
            for col, k in zip(cols, row, strict=False):
                picked = ss.get("objective_pick") == k
                ckey = f"objc_{k}__sel" if picked else f"objc_{k}"
                with col.container(border=True, key=ckey):
                    st.markdown(
                        f'<div class="objc"><div class="tile t-blue">{icons[keys.index(k)]}</div>'
                        f"<b>{OBJECTIVE_LABELS[k]}</b><small>{OBJECTIVE_HINTS[k]}</small></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button(
                        "Selected" if picked else "Select",
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
    st.markdown("#### Pending Approvals")
    st.caption("Review and approve research workflows before execution.")
    flash()
    wfs = all_workflows()
    groups = split_approvals(wfs)
    c = st.columns(4)
    c[0].markdown(
        stat_card("Pending", str(len(groups["pending"])), "awaiting you", "amber", "⏳"),
        unsafe_allow_html=True,
    )
    c[1].markdown(
        stat_card("Approved", str(len(groups["approved"])), "approved", "green", "✓"),
        unsafe_allow_html=True,
    )
    c[2].markdown(
        stat_card("Rejected", str(len(groups["rejected"])), "rejected", "red", "✕"),
        unsafe_allow_html=True,
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
    left, right = st.columns([1.35, 1])
    with left.container(border=True, key="wf_steps"):
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
    with right.container(border=True, key="wf_activity"):
        rows = workflow_rows(wfs)[:4]
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
        detail = f" · _{r['detail']}_" if r.get("detail") else ""
        st.markdown(
            f"- **[{r.get('tag')}]** {r.get('date') or ''} — {r.get('title')}{detail} "
            f"{badge_markdown(r.get('data_origin'))}{link}"
        )


def report_downloads(res: dict[str, Any], company: str, key: str) -> None:
    """Download buttons for one report: formatted PDF, printable HTML and raw Markdown."""

    md = str(res.get("markdown") or "")
    if not md.strip():
        st.warning("This report has no document content yet. Re-open it from Reports.")
        return
    title = f"{company} research report"
    if pdf_available():
        try:
            pdf = markdown_to_pdf(md, title)
        except Exception as exc:  # never break the page because of the export
            st.caption(f"PDF export failed ({type(exc).__name__}); use HTML or Markdown.")
        else:
            st.download_button(
                "\u2b07 Download PDF",
                pdf,
                file_name=report_filename(company, "pdf"),
                mime="application/pdf",
                type="primary",
                width="stretch",
                key=f"{key}_pdf",
            )
    else:
        st.caption("Install fpdf2 for PDF export.")
    st.download_button(
        "HTML (print-ready)",
        markdown_to_html(md, title).encode("utf-8"),
        file_name=report_filename(company, "html"),
        mime="text/html",
        width="stretch",
        key=f"{key}_html",
    )
    st.download_button(
        "Markdown (.md)",
        md.encode("utf-8"),
        file_name=report_filename(company, "md"),
        mime="text/markdown",
        width="stretch",
        key=f"{key}_md",
    )


def report_page() -> None:
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
    if "ticker" not in res:
        cq = wf.get("company_query") or {}
        res["ticker"] = str(cq.get("ticker") or "") or watch_ticker(view.company)
    agents = ensure_deliberation(res, view.company, res["ticker"])
    res["markdown"] = append_to_report(res["markdown"], agents)
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
        + rating_badge(agents)
        + " "
        + " ".join(origin_pill(o) for o in sorted(origins))
        + f'</div><div style="font-size:.75rem;color:#8FA3C7">'
        f"{esc(objective_label(wf.get('objective')))} · workflow {wid[:8]} · "
        f"{esc(wf.get('evidence_count', '-'))} evidence items</div>",
        unsafe_allow_html=True,
    )
    with dl:
        report_downloads(res, view.company, key=f"rp_{wid}")
    tabs = st.tabs([*REPORT_TABS, "Debate & Rating"])
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
                st.altair_chart(revenue_chart(pairs), width="stretch")
            elif series:
                st.altair_chart(price_chart(series), width="stretch")
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
        recs = analyst_rows(sec.rows) if sec else []
        if recs:
            with st.container(border=True, key="analyst_rec"):
                st.markdown(
                    '<div class="panel-t">Analyst recommendation '
                    + pill("Finnhub", "blue")
                    + "</div>",
                    unsafe_allow_html=True,
                )
                for r in recs:
                    st.markdown(
                        f"**[{r.get('tag')}]** {r.get('title')} "
                        f"{badge_markdown(r.get('data_origin'))}"
                    )
                st.caption("Third-party analyst data (Finnhub), not a fact or advice.")
        elif sec and sec.rows:
            st.caption(
                f"Analyst recommendation: {NA} (add a Finnhub key in Settings; US companies only)."
            )
        if sec and sec.rows:
            _items([r for r in sec.rows if r not in recs])
        else:
            st.caption(f"News: {NA}.")
    with tabs[3]:
        sec = by.get("Industry")
        if sec and sec.rows:
            st.markdown(badge_markdown(sec.origin, sec.status) + f" {sec.message or ''}")
            figs = [r for r in sec.rows if "label" in r]
            peers = [r for r in sec.rows if "title" in r]
            if figs:
                _rows_table(figs)
            if peers:
                st.markdown("**Peer comparison (1-year return)**")
                _items(peers)
        else:
            msg = f" ({sec.message})" if sec and sec.message else ""
            st.caption(f"Industry: {NA}{msg}.")
    with tabs[4]:
        sec = by.get("Regulatory")
        if sec and sec.rows:
            st.markdown(badge_markdown(sec.origin, sec.status) + f" {sec.message or ''}")
            _items(sec.rows)
        else:
            msg = f" ({sec.message})" if sec and sec.message else ""
            st.caption(f"Regulatory: {NA}{msg}.")
    with tabs[5]:
        st.markdown(res["markdown"])
    with tabs[-1]:
        deliberation_tab(res, view.company, key=f"dl_{wid}")
    with tabs[6]:
        refs = evidence_refs(data)
        if refs:
            st.markdown(table(refs, list(refs[0])), unsafe_allow_html=True)
        else:
            st.caption(f"Evidence references: {NA}.")


def reports_page() -> None:
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
        file_name="amrp-reports.csv",
        mime="text/csv",
        disabled=not shown,
        width="stretch",
    )
    reports = ss.get("reports", {})
    done = [r for r in shown if r["Status"] in ("Completed", "Partial")]
    if done:
        with st.expander(
            "\u2b07 Download a report document (PDF / HTML / Markdown)", expanded=True
        ):
            labels = {f"{r['Company']} \u00b7 {r['Created'][:16]}": r for r in done}
            pick = st.selectbox("Report", list(labels), key="dl_pick")
            r = labels[pick]
            wid = r["workflow_id"]
            if wid not in reports and st.button("Prepare document", key="dl_prepare"):
                try:
                    open_report(wid, r["Company"])
                    reports = ss.get("reports", {})
                except ApiError as exc:
                    st.error(f"Report not available ({exc.message})")
            if wid in reports:
                cols3 = st.columns(3)
                res = reports[wid]
                company = getattr(res.get("view"), "company", None) or r["Company"]
                with cols3[0]:
                    report_downloads(res, company, key=f"rl_{wid}")
            else:
                st.caption("Click Prepare document to build the files for this report.")
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
            e.write("PDF / HTML / MD")
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
                    if safe(API.get, f"/research/workflows/{wid}") is None:
                        ss.error = (
                            "This approval request is no longer held by the API (it was "
                            "restarted). Run the research again to create a new request."
                        )
                    else:
                        go("Workflow Approvals")
                    st.rerun()
            else:
                f.write("-")


def watchlist_page() -> None:
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
            logs = latest_run_logs(
                safe(CONTROL.get, "/control/runs", default={}) or {},
                lambda rid: safe(CONTROL.get, f"/control/runs/{rid}/logs", default={}),
            )
        st.markdown('<div class="panel-t">Recent Logs</div>', unsafe_allow_html=True)
        st.code("\n".join(logs) or "(no step-by-step run logs yet)")
        ver = safe(API.get, "/version", default=None)
        if ver:
            st.json(ver, expanded=False)


def steps_mode() -> None:
    st.markdown("#### 🪜 Step-by-step mode")
    st.markdown(STEP_MODE_HELP)
    if not CONTROL.token:
        st.error("Control API token missing - start with scripts/start-dashboard.ps1.")
        return
    try:
        CONTROL.get("/control/health")
    except ApiError as exc:
        st.error(f"Control API not reachable ({exc.message}).")
        return
    with st.form("steprun"):
        q = st.text_input(
            "Company",
            value=step_mode_company(ss.get("company")),
            placeholder="Company name or ticker, e.g. RELIANCE.NS or AAPL",
            help="Type any company name, or a ticker such as RELIANCE.NS, TCS.NS or AAPL.",
        )
        objective = st.selectbox(
            "What to research", list(OBJECTIVE_LABELS), index=6, format_func=objective_label
        )
        start = st.form_submit_button("▶️ Start step-by-step run", type="primary")
    try:
        if start and not q.strip():
            st.warning("Enter a company name or ticker first.")
            start = False
        if start:
            q = q.strip()
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
    st.markdown("#### Settings")
    st.caption("Configure providers, models, security and preferences.")
    t1, t2, t3, t4, t5, t6 = st.tabs(
        ["Providers", "Model", "AI agents", "Data keys", "Live data", "Security"]
    )
    t3, t4, t5, t6 = t4, t5, t6, t3
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
        with st.container(border=True, key="prov_table"):
            st.markdown(
                table(rows, ["Provider", "StatusH", "Used by backend"], ["StatusH"]).replace(
                    "<th>StatusH</th>", "<th>Status</th>"
                ),
                unsafe_allow_html=True,
            )
        st.markdown('<div class="sec-gap"></div>', unsafe_allow_html=True)
        st.markdown("**Add an API key** - paste any key; the provider is detected from its format.")
        key = st.text_input(
            "API key",
            type="password",
            key="k_any",
            placeholder="sk-or-… / sk-ant-… / AIza… / gsk_… / sk-… or any custom key",
        ).strip()
        detected = detect_provider(key) if key else None
        names = [p.key for p in PROVIDERS]
        spec = st.selectbox(
            "Provider (auto-detected, change if wrong)",
            PROVIDERS,
            index=names.index(detected.key) if detected else 0,
            format_func=lambda p: p.label,
            key=f"prov_{detected.key if detected else 'none'}",
        )
        if detected:
            st.markdown(
                pill(f"Detected: {detected.label}", "blue")
                + ("" if detected.key != "custom" else " " + pill("Enter base URL", "amber")),
                unsafe_allow_html=True,
            )
        if spec.signup_url:
            st.caption(f"Get a key at {spec.signup_url}")
        if not spec.used_by_backend:
            st.info(
                "Saved for later. Research summaries use OpenRouter (or a Custom "
                "OpenAI-compatible router chosen under Model)."
            )
        base_url = st.text_input(
            "Base URL",
            value=spec.base_url,
            key=f"bu_{spec.key}",
            placeholder="https://your-server/v1 (models are loaded from <base>/models)",
            disabled=not spec.needs_base_url,
        ).strip()
        mkey = f"models_{spec.key}"
        c1, c2 = st.columns(2)
        if c1.button("🔌 Test & load models", disabled=not key):
            try:
                ok, msg = probe(*connection_test_request(spec, key, base_url))
                ids: list[str] = []
                if ok:
                    _, _, body = probe_json(*models_request(spec, key, base_url))
                    ids = parse_model_ids(body)
                ss[mkey] = ids
                (st.success if ok else st.error)(f"{msg} · {len(ids)} models found" if ok else msg)
            except ValueError as exc:
                st.error(str(exc))
        if ss.get(mkey):
            st.selectbox("Models available with this key", ss[mkey], key=f"pick_{spec.key}")
        if c2.button("💾 Save", type="primary", disabled=not key):
            try:
                saved = CONTROL.put(f"/control/keys/{spec.key_name}", {"value": key})
                st.success(f"Saved {saved['masked']} as {spec.label}.")
            except ApiError as exc:
                st.error(exc.message)
    with t2:
        model_settings()
    with t3:
        data_keys(masked)
    with t4:
        live_settings()
    with t5:
        st.markdown("**Security & keys**")
        st.markdown(
            "- Keys are write-only: only the last 4 characters are ever shown.\n"
            "- The control API listens on 127.0.0.1 only, with a random per-session "
            "token.\n- Comprehensive research always needs your approval."
        )
        cfg = safe(CONTROL.get, "/control/settings", default=None) if CONTROL.token else None
        if cfg:
            st.json(cfg, expanded=False)
    with t6:
        agent_llm_settings()


RESTART_NOTE = (
    "Step-by-step and Comprehensive Research runs use this from the next run. The Research "
    "page (REST API) picks it up after restarting scripts/start-dashboard.ps1."
)


def model_settings() -> None:
    prefs = load_prefs()
    current = str(prefs.get("llm_model") or "")
    st.markdown("**Model selection**")
    st.markdown(
        "Default: **automatic** - the fastest working **free** model is chosen for every "
        "run. Pick a specific model below for deeper research; paid models are used only "
        "when you select one here."
    )
    st.markdown(
        "Current: "
        + (
            pill(current, "amber" if is_paid_model(current) else "green")
            + (" " + pill("PAID", "red") if is_paid_model(current) else "")
            if current
            else pill("Automatic (free first)", "green")
        ),
        unsafe_allow_html=True,
    )
    custom = uses_custom_router(prefs)
    router = st.radio(
        "Router",
        ["OpenRouter", "Custom OpenAI-compatible router"],
        index=1 if custom else 0,
        horizontal=True,
        key="m_router",
    )
    if router == "OpenRouter":
        base = OPENROUTER_BASE
    else:
        base = st.text_input(
            "Base URL",
            value=str(prefs.get("llm_base_url") or "") if custom else "",
            placeholder="https://your-router.example/v1 (needs /models and /chat/completions)",
            key="m_base",
        ).strip()
        st.caption(
            "Uses the Custom key saved under Providers (CUSTOM_LLM_API_KEY). Only model ids "
            "ending in ':free' count as free; every other model is treated as paid."
        )
    list_key = st.text_input(
        "API key to load the model list (optional, not saved)",
        type="password",
        key="m_listkey",
        help="OpenRouter lists models without a key; custom routers usually need one.",
    ).strip()
    if st.button("🔄 Load models from provider", disabled=not base):
        headers = {"Authorization": f"Bearer {list_key}"} if list_key else {}
        ok, msg, body = probe_json(base.rstrip("/") + "/models", headers)
        ss.m_models = model_options(body) if ok else []
        (st.success if ok else st.error)(
            f"{msg} · {len(ss.m_models)} models" if ok else f"Could not load models: {msg}"
        )
    models: list[dict[str, Any]] = ss.get("m_models") or []
    ids = ["", *[m["id"] for m in models]]
    labels = {"": "Automatic - best free model (default)", **{m["id"]: m["label"] for m in models}}
    if current and current not in labels:
        ids.append(current)
        labels[current] = current + (" · PAID" if is_paid_model(current) else " · FREE")
    choice = st.selectbox(
        f"Model ({sum(1 for m in models if m['free'])} free / "
        f"{sum(1 for m in models if not m['free'])} paid loaded)",
        ids,
        index=ids.index(current) if current in ids else 0,
        format_func=lambda m: labels.get(m, m),
        key="m_choice",
    )
    paid = is_paid_model(choice)
    optin = False
    if paid:
        st.markdown(
            '<div class="banner-red">💲 <b>Paid model - costs real money.</b> The provider '
            "bills your account per token for every research summary and plan (a "
            "comprehensive run makes several calls). It is used only because you select "
            "it; switch back to Automatic to return to free models.</div>",
            unsafe_allow_html=True,
        )
        optin = st.checkbox("I understand this model is paid and want to use it", key="m_optin")
    if st.button("💾 Save model choice", type="primary", disabled=(paid and not optin) or not base):
        try:
            save_model_choice(choice, base, paid_opt_in=optin)
            st.success("Saved. " + RESTART_NOTE)
        except ValueError as exc:
            st.error(str(exc))
    with st.expander("Which free model would Automatic use right now?"):
        task = st.radio("Task", ["summary", "planning", "report"], horizontal=True)
        if st.button("Show"):
            try:
                listed = JsonClient(OPENROUTER_BASE, timeout=20).get("/models")
                pick = choose_model(listed.get("data", []), task)
                (st.success if pick.model_id else st.warning)(
                    f"**{pick.model_id}** - {pick.reason}" if pick.model_id else pick.reason
                )
            except ApiError as exc:
                st.error(f"Could not load the model list: {exc.message}")


DATA_KEYS = (
    (
        "ALPHA_VANTAGE_API_KEY",
        "Alpha Vantage",
        "Backup source for US/India share prices and daily history when Yahoo is unavailable.",
        "https://www.alphavantage.co/support/#api-key",
        "Free plan: only a limited number of requests per day (check alphavantage.co for "
        "current limits).",
    ),
    (
        "FINNHUB_API_KEY",
        "Finnhub",
        "US company news, analyst recommendations (buy/hold/sell) and the earnings "
        "calendar. Shown on the report News tab with evidence tags.",
        "https://finnhub.io/register",
        "Free plan: rate-limited per minute; US-listed companies only.",
    ),
)


def data_keys(masked: dict[str, Any]) -> None:
    st.markdown("**Market data keys** (optional - real data works without them)")
    cols = st.columns(len(DATA_KEYS))
    for col, (name, label, what, url, note) in zip(cols, DATA_KEYS, strict=True):
        info = masked.get(name) or {}
        with col.container(border=True, key=f"dk_{name.lower()}"):
            status = (
                pill("Configured " + str(info.get("masked", "")), "green")
                if info.get("configured")
                else pill("Not configured", "amber")
            )
            st.markdown(f"**{label}** {status}", unsafe_allow_html=True)
            st.caption(what)
            st.markdown(f"[Get a free key →]({url})")
            st.caption(note)
            value = st.text_input(f"{label} key", type="password", key=f"dkv_{name}").strip()
            if st.button(f"💾 Save {label} key", key=f"dks_{name}", disabled=not value):
                try:
                    saved = CONTROL.put(f"/control/keys/{name}", {"value": value})
                    st.success(f"Saved {saved['masked']}. " + RESTART_NOTE)
                except ApiError as exc:
                    st.error(exc.message)


def live_settings() -> None:
    st.markdown("**Live data switches**")
    st.caption("Off = labelled SAMPLE DATA (works offline). On = real data from public sources.")
    prefs = load_prefs()
    with st.form("live"):
        values: dict[str, object] = {}
        for k, (var, label) in LIVE_TOGGLES.items():
            values[k] = st.toggle(f"{label}  ·  `{var}`", value=bool(prefs.get(k)), key=f"lt_{k}")
            st.caption(LIVE_DESCRIPTIONS[k])
        values["sec_user_agent"] = st.text_input(
            "SEC_USER_AGENT (your name + real email)",
            value=str(prefs.get("sec_user_agent", "")),
            placeholder="Jane Doe jane@example.com",
            help="SEC EDGAR requires every request to identify a contact.",
        ).strip()
        st.caption(
            "SEC EDGAR blocks anonymous requests: US filings and financials need a contact "
            "name and a real email in the User-Agent. It is only sent to sec.gov."
        )
        if st.form_submit_button("💾 Save live-data settings", type="primary"):
            ua = str(values["sec_user_agent"])
            if ua and ("@" not in ua or " " not in ua):
                st.error("SEC_USER_AGENT needs a name and an email, e.g. 'Jane Doe jane@x.com'.")
            else:
                save_prefs(values)
                st.success("Saved. " + RESTART_NOTE)


# ---------------------------------------------------------------- comprehensive research
_STATE_PILL = {
    "waiting": ("Waiting for Start", "gray"),
    "queued": ("Queued", "gray"),
    "pending_approval": ("Needs approval", "amber"),
    "approved": ("Approved", "blue"),
    "running": ("Running", "blue"),
    "succeeded": ("Done", "green"),
    "failed": ("Failed", "red"),
    "rejected": ("Rejected", "red"),
    "skipped": ("Skipped", "gray"),
}


def _step_label(state: str) -> str:
    text, tone = _STATE_PILL.get(state, (state.replace("_", " ").title(), "gray"))
    return pill(text, tone)


def _control_ready() -> bool:
    if not CONTROL.token:
        st.error("Control API token missing - start with scripts/start-dashboard.ps1.")
        return False
    try:
        CONTROL.get("/control/health")
    except ApiError as exc:
        st.error(f"Control API not reachable ({exc.message}).")
        return False
    return True


def comprehensive_page() -> None:
    st.markdown("#### Comprehensive Research")
    st.caption(
        "Five research agents analyse one company. Every agent step waits for your "
        "approval; you see live progress and logs, and can retry a failed step."
    )
    run: dict[str, Any] | None = None
    if _control_ready() and ss.get("comp_run"):
        run = safe(CONTROL.get, f"/control/runs/{ss.comp_run}", default=None)
    steps: list[dict[str, Any]] = (run or {}).get("steps", [])
    states = agent_states(steps)
    st.markdown(
        agent_grid([(n, _step_label(states[k]), w) for k, n, w in RESEARCH_AGENTS]),
        unsafe_allow_html=True,
    )
    if not CONTROL.token:
        return
    st.markdown('<div class="comp-gap"></div>', unsafe_allow_html=True)
    st.markdown('<div class="panel-t">Start a run</div>', unsafe_allow_html=True)
    with st.form("comp_start"):
        c1, c2 = st.columns([4, 1])
        q = c1.text_input(
            "Company",
            value="",
            placeholder="Company name or ticker, e.g. RELIANCE.NS or AAPL",
            label_visibility="collapsed",
        )
        start = c2.form_submit_button(
            "Start Comprehensive Research", type="primary", width="stretch"
        )
    try:
        if start and not q.strip():
            st.warning("Enter a company name or ticker first.")
        elif start:
            CONTROL.put(
                "/control/settings", {"approval_mode": True, "auto_approve_low_risk": False}
            )
            created = CONTROL.post(
                "/control/runs",
                {"params": {"q": q.strip(), "objective": "comprehensive_equity_research"}},
            )
            ss.comp_run, ss.comp_company = created["run_id"], q.strip()
            st.rerun()
        if run is None:
            st.info("Start a run to see the approval gate, per-step progress and live logs.")
            return
        status = str(run.get("status"))
        done = sum(1 for s in steps if s.get("state") in ("succeeded", "failed", "rejected"))
        total = max(len(steps), len(RESEARCH_AGENTS))
        st.markdown(
            f"**{esc(ss.get('comp_company', ''))}** · run `{ss.comp_run[:8]}` · "
            f"status **{status}** · {done}/{total} steps finished"
        )
        st.progress(min(1.0, done / total) if total else 0.0)
        waiting = pending_steps(steps)
        if waiting:
            with st.container(key="comp_pending"):
                st.markdown(
                    f'<div class="banner-amber">⏸ <b>Approval needed</b> - {len(waiting)} '
                    "step(s) are waiting for you. Nothing runs until you approve.</div>",
                    unsafe_allow_html=True,
                )
        for step in steps:
            sid, state = step["step_id"], str(step["state"])
            with st.container(border=True):
                head, b1, b2, b3 = st.columns([6, 1, 1, 1])
                head.markdown(
                    f"{_step_label(state)} **{esc(str(step.get('kind') or ''))}** - "
                    f"{esc(str(step.get('name') or ''))[:140]}",
                    unsafe_allow_html=True,
                )
                out = step.get("output") or {}
                if out.get("summary"):
                    head.caption(str(out["summary"])[:400])
                if step.get("error"):
                    head.caption(f"error: {step['error']}")
                if state == "pending_approval":
                    if b1.button("Approve", key=f"ca{sid}", type="primary"):
                        CONTROL.post(f"/control/runs/{ss.comp_run}/steps/{sid}/approve", {})
                        st.rerun()
                    if b2.button("Reject", key=f"cr{sid}"):
                        CONTROL.post(f"/control/runs/{ss.comp_run}/steps/{sid}/reject", {})
                        st.rerun()
                if state in ("failed", "rejected") and b3.button("Retry", key=f"ct{sid}"):
                    CONTROL.post(f"/control/runs/{ss.comp_run}/steps/{sid}/retry", {})
                    st.rerun()
        logs = CONTROL.get(f"/control/runs/{ss.comp_run}/logs").get("logs", [])
        st.markdown("**Live log**")
        st.code(
            "\n".join(f"{x.get('level', 'info'):5} {x.get('message')}" for x in logs[-40:])
            or "(no logs yet)"
        )
        if status in ("succeeded", "completed", "failed", "partial", "stopped"):
            res = run.get("result") or {}
            st.success(
                f"Run finished: {res.get('status', status)} · "
                f"{res.get('completed_count', '?')} agents completed, "
                f"{len(res.get('evidence_refs') or [])} evidence references."
            )
            if st.button("📄 Open the finished report", type="primary", key="comp_report"):
                try:
                    start_research(
                        str(ss.get("comp_company", "")),
                        objective="comprehensive_equity_research",
                        auto_approve=True,
                    )
                except ApiError as exc:
                    ss.error = exc.message
                if ss.get("result"):
                    ss.nav = "Research Report"
                    st.rerun()
            st.caption(
                "The formatted report (tabs, charts, evidence tags) is built by the REST API "
                "from a comprehensive workflow for the same company."
            )
        elif status in ("running", "queued") and not waiting:
            time.sleep(1.5)
            st.rerun()
    except ApiError as exc:
        st.error(exc.message)


# ---------------------------------------------------------------- commodity market
class _NoData(Exception):
    """Raised inside cached fetchers so failed/empty results are never cached."""


@st.cache_data(ttl=900, show_spinner=False)
def _commodity_series_cached(name: str) -> ml.Daily:
    d = cm.price_series(cm.COMMODITIES[name])
    if d is None or not d.bars:
        raise _NoData(name)
    return d


@st.cache_data(ttl=900, show_spinner=False)
def _commodity_news_cached(name: str) -> list[ml.NewsItem]:
    items = cm.related_news(cm.COMMODITIES[name])
    if not items:
        raise _NoData(name)
    return items


def commodity_series(name: str, retries: int = 2) -> ml.Daily | None:
    """US futures series; failures are not cached and are retried."""
    for _ in range(retries):
        try:
            return _commodity_series_cached(name)
        except _NoData:
            continue
    return None


def commodity_news(name: str, retries: int = 2) -> list[ml.NewsItem]:
    for _ in range(retries):
        try:
            return _commodity_news_cached(name)
        except _NoData:
            continue
    return []


def commodity_page() -> None:
    st.markdown("#### Commodity Market")
    st.caption("Live US futures prices (COMEX/NYMEX, USD), a 1-month graph and related news.")
    # Keyed container -> CSS class ``st-key-cm_selectors``; pills wrap on narrow windows.
    with st.container(key="cm_selectors"):
        name = st.radio("Commodity", list(cm.COMMODITIES), horizontal=True, key="cm_name")
    com = cm.COMMODITIES[name]
    with st.spinner("Loading live prices…"):
        d = commodity_series(name)
    left, right = st.columns([2.2, 1])
    with left.container(border=True, key="cm_price"):
        if d is None:
            st.markdown(
                f'<div class="na">{esc(name)} price: {NA} (Yahoo Finance unreachable)</div>',
                unsafe_allow_html=True,
            )
        else:
            last = d.bars[-1].close
            day = (last / d.bars[-2].close - 1) if len(d.bars) > 1 and d.bars[-2].close else None
            unit = f"US$ per {com.us_unit}"
            cur = "$"
            tone = "s-green" if (day or 0) >= 0 else "s-red"
            mtone = "s-green" if (d.change or 0) >= 0 else "s-red"
            st.markdown(
                f'<div class="cm-h"><div><b>{esc(name)}</b> '
                f"{pill('LIVE', 'green')} <small>{esc(com.symbol)} · {esc(com.exchange)}"
                f" · as of {_ist(d.as_of)}</small></div>"
                f'<div class="cm-v">{cur}{last:,.2f} <small>{esc(unit)}</small></div>'
                f'<div><span class="{tone}">{day:+.2%} 1d</span> · '
                f'<span class="{mtone}">{(d.change or 0):+.2%} 1m</span></div></div>'
                if day is not None
                else f"<b>{esc(name)}</b> {cur}{last:,.2f} {esc(unit)}",
                unsafe_allow_html=True,
            )
            st.altair_chart(area_chart(d, height=260, month=True), width="stretch")
            st.caption("Daily closes, last 1 month · source: Yahoo Finance (cached 15 min)")
    with right.container(border=True, key="cm_news"):
        items = commodity_news(name)
        body = (
            "".join(
                f'<div class="rr"><div class="n"><b><a href="{esc(n.link)}" target="_blank">'
                f"{esc(n.title)}</a></b><small>{esc(n.source)} · {_ist(n.published)}</small>"
                "</div></div>"
                for n in items
            )
            or f'<div class="na">Related news: {NA}</div>'
        )
        st.markdown(
            f'<div class="panel-t">Latest {esc(name)} news</div>{body}', unsafe_allow_html=True
        )
        st.caption("Yahoo Finance + Google News RSS")
    dbars, ex = session_charts(com.symbol, "cmx", 1.0, 2)
    ai_panel(
        "commodity",
        f"{name} (US)",
        mk.build_evidence(name, f"USD per {com.us_unit}", dbars, ex, items, 2),
        "cmx",
    )


# ---------------------------------------------------------------- sessions + AI (v7)
SESSION_COLORS = {"Asia/Tokyo": "#A78BFA", "London": "#38BDF8", "New York": "#F59E0B"}


@st.cache_data(ttl=600, show_spinner=False)
def intraday(symbol: str, range_: str, interval: str) -> list[mk.IBar]:
    return mk.fetch_intraday(symbol, range_, interval)


@st.cache_data(ttl=900, show_spinner=False)
def instrument_news(kind: str, key: str) -> list[ml.NewsItem]:
    return mk.news_for((mk.FOREX if kind == "fx" else mk.CRYPTO)[key])


def session_chart(bars: list[mk.IBar], height: int, fmt: str, week: bool) -> Any:
    """Area chart with shaded Asia/London/New York bands and high/low markers per day."""

    df = pd.DataFrame([{"t": b.ts.replace(tzinfo=None), "Close": b.close} for b in bars])
    lo = min(b.low for b in bars)
    hi = max(b.high for b in bars)
    pad = (hi - lo) * 0.15 or hi * 0.002
    t0, t1 = df.t.min(), df.t.max()
    bands = []
    for day in sorted({b.ts.date() for b in bars}):
        base = datetime(day.year, day.month, day.day)
        for name, a, z in mk.SESSIONS:
            s0, s1 = max(base + timedelta(hours=a), t0), min(base + timedelta(hours=z), t1)
            if s0 < s1:
                bands.append({"s0": s0, "s1": s1, "Session": name})
    marks = []
    groups = [bars] if not week else list(mk.by_day(bars).values())
    for g in groups:
        e = mk.extremes(g)
        if e:
            marks.append(
                {
                    "t": e.high_ts.replace(tzinfo=None),
                    "v": e.high,
                    "k": "High",
                    "lab": f"H {format(e.high, fmt)} · {e.high_session}",
                }
            )
            marks.append(
                {
                    "t": e.low_ts.replace(tzinfo=None),
                    "v": e.low,
                    "k": "Low",
                    "lab": f"L {format(e.low, fmt)} · {e.low_session}",
                }
            )
    up = bars[-1].close >= bars[0].open
    color = "#22D3A0" if up else "#FF4D6A"
    x = alt.X(
        "t:T",
        title=None,
        scale=alt.Scale(domain=[t0, t1]),
        axis=alt.Axis(format="%d %b" if week else "%H:%M", labelAngle=0, tickCount=7),
    )
    y = alt.Y(
        "Close:Q",
        title=None,
        scale=alt.Scale(domain=[lo - pad, hi + pad], nice=False),
        axis=alt.Axis(format=fmt if hi < 100 else _axis_fmt(hi), tickCount=4),
    )
    band = (
        alt.Chart(pd.DataFrame(bands))
        .mark_rect(opacity=0.10)
        .encode(
            x="s0:T",
            x2="s1:T",
            color=alt.Color(
                "Session:N",
                scale=alt.Scale(domain=list(SESSION_COLORS), range=list(SESSION_COLORS.values())),
                legend=alt.Legend(orient="top", title=None),
            ),
        )
    )
    grad = alt.Gradient(
        gradient="linear",
        stops=[
            alt.GradientStop(color=color, offset=0),
            alt.GradientStop(color="rgba(0,0,0,0)", offset=1),
        ],
        x1=1,
        x2=1,
        y1=0,
        y2=1,
    )
    area = (
        alt.Chart(df)
        .encode(
            x=x,
            y=y,
            tooltip=[
                alt.Tooltip("t:T", format="%d %b %H:%M UTC"),
                alt.Tooltip("Close:Q", format=fmt),
            ],
        )
        .encode(y2=alt.datum(lo - pad))
        .mark_area(
            interpolate="monotone",
            line={"color": color, "strokeWidth": 1.6},
            color=grad,
            opacity=0.5,
        )
    )
    md = pd.DataFrame(marks)
    pts = (
        alt.Chart(md)
        .mark_point(filled=True, size=46, opacity=1)
        .encode(
            x="t:T",
            y="v:Q",
            shape=alt.Shape(
                "k:N",
                scale=alt.Scale(domain=["High", "Low"], range=["triangle-up", "triangle-down"]),
                legend=None,
            ),
            color=alt.Color(
                "k:N",
                scale=alt.Scale(domain=["High", "Low"], range=["#22D3A0", "#FF4D6A"]),
                legend=None,
            ),
            tooltip=["lab:N"],
        )
    )
    layers = [band, area, pts]
    if not week:
        layers.append(
            alt.Chart(md)
            .mark_text(dy=-10, fontSize=10, color="#E2E8F0")
            .encode(x="t:T", y="v:Q", text="lab:N")
        )
    return _style(alt.layer(*layers).resolve_scale(color="independent")).properties(
        height=height, padding={"left": 12, "right": 6, "top": 4, "bottom": 2}
    )


def extremes_table(ex: list[mk.Extreme], fmt: str) -> None:
    rows = "".join(
        f"<tr><td>{esc(e.day)}</td><td class='s-green'>{format(e.high, fmt)}</td>"
        f"<td>{esc(e.high_session)}, {mk.ist(e.high_ts)}</td><td class='s-red'>"
        f"{format(e.low, fmt)}</td><td>{esc(e.low_session)}, {mk.ist(e.low_ts)}</td></tr>"
        for e in reversed(ex)
    )
    st.markdown(
        "<table class='hl'><tr><th>Day (UTC)</th><th>High</th><th>Session, IST time</th>"
        f"<th>Low</th><th>Session, IST time</th></tr>{rows}</table>",
        unsafe_allow_html=True,
    )


def session_charts(
    symbol: str, key: str, k: float = 1.0, decimals: int = 2
) -> tuple[list[mk.IBar], list[mk.Extreme]]:
    """Day-wise (15m) and week-wise (1h) charts + high/low session table. Returns evidence parts."""

    fmt = f",.{decimals}f"
    day = mk.scale(mk.last_day(intraday(symbol, "5d", "15m")), k)
    week_bars = mk.scale(intraday(symbol, "1mo", "1h"), k)
    days = mk.by_day(week_bars)
    week_bars = [b for d in list(days)[-7:] for b in days[d]]
    ex = mk.daily_extremes(week_bars, 7)
    a, b = st.columns(2)
    with a.container(border=True, key=f"{key}_day"):
        st.markdown(
            '<div class="panel-t">Day-wise · 15-min bars, last trading day (UTC)</div>',
            unsafe_allow_html=True,
        )
        if len(day) > 2:
            st.altair_chart(session_chart(day, 250, fmt, week=False), width="stretch")
        else:
            st.markdown(f'<div class="na">Intraday data: {NA}</div>', unsafe_allow_html=True)
    with b.container(border=True, key=f"{key}_week"):
        st.markdown(
            '<div class="panel-t">Week-wise · hourly bars, last 7 days (UTC)</div>',
            unsafe_allow_html=True,
        )
        if len(week_bars) > 2:
            st.altair_chart(session_chart(week_bars, 250, fmt, week=True), width="stretch")
        else:
            st.markdown(f'<div class="na">Hourly data: {NA}</div>', unsafe_allow_html=True)
    with st.container(border=True, key=f"{key}_hl"):
        st.markdown('<div class="panel-t">Session high / low by day</div>', unsafe_allow_html=True)
        if ex:
            extremes_table(ex, fmt)
        else:
            st.markdown(f'<div class="na">Session high/low: {NA}</div>', unsafe_allow_html=True)
        st.caption(
            "Sessions (UTC): Asia/Tokyo 00-09 · London 07-16 · New York 12-21; overlaps "
            "shown as 'London + New York'. 21-24 UTC = Off-session. Source: Yahoo Finance."
        )
    return day, ex


def news_panel(items: list[ml.NewsItem], title: str) -> None:
    body = (
        "".join(
            f'<div class="rr"><div class="n"><b><a href="{esc(n.link)}" target="_blank">'
            f"{esc(n.title)}</a></b><small>{esc(n.source)} · {_ist(n.published)}</small>"
            "</div></div>"
            for n in items
        )
        or f'<div class="na">Related news: {NA}</div>'
    )
    st.markdown(f'<div class="panel-t">{esc(title)}</div>{body}', unsafe_allow_html=True)


def ai_panel(market: str, name: str, evidence: list[dict[str, str]], key: str) -> None:
    with st.container(border=True, key=f"{key}_ai"):
        st.markdown('<div class="panel-t">AI analysis</div>', unsafe_allow_html=True)
        st.caption(
            f"Sends {len(evidence)} evidence items (prices + headlines) to your selected "
            "model (Settings > Model; free model by default, paid only if you opted in)."
        )
        if st.button("✨ Analyze with AI", key=f"{key}_ai_btn", disabled=not evidence):
            with st.spinner("Asking the model…"):
                try:
                    ss[f"{key}_ai_res"] = CONTROL.post(
                        "/control/analyze",
                        {"market": market, "instrument": name, "evidence": evidence},
                    )
                except Exception as exc:
                    ss[f"{key}_ai_res"] = {"status": "not_available", "reason": str(exc)[:200]}
        res = ss.get(f"{key}_ai_res")
        if res and res.get("status") == "ok":
            st.markdown(
                f"{pill('AI', 'blue')} <small>model {esc(str(res.get('model')))} · cited "
                f"{esc(', '.join(res.get('cited') or []) or 'none')}</small>",
                unsafe_allow_html=True,
            )
            st.markdown(res.get("text", ""))
            if res.get("unknown_tags"):
                st.warning("Unverified tags in answer: " + ", ".join(res["unknown_tags"]))
            with st.expander("Evidence sent"):
                for e in evidence:
                    link = f" [link]({e['url']})" if e.get("url") else ""
                    st.markdown(f"**[{e['tag']}]** {e['text']} _({e['source']})_{link}")
        elif res:
            st.markdown(
                f'<div class="na">AI analysis: {NA} ({esc(str(res.get("reason")))})</div>',
                unsafe_allow_html=True,
            )


def instrument_header(
    name: str, symbol: str, bars: list[mk.IBar], cur: str, unit: str, decimals: int
) -> None:
    if not bars:
        st.markdown(
            f'<div class="na">{esc(name)} price: {NA} (Yahoo Finance unreachable)</div>',
            unsafe_allow_html=True,
        )
        return
    last, first = bars[-1], bars[0]
    chg = last.close / first.open - 1 if first.open else 0.0
    tone = "s-green" if chg >= 0 else "s-red"
    st.markdown(
        f'<div class="cm-h"><div><b>{esc(name)}</b> {pill("LIVE", "green")} <small>'
        f"{mk.ist(last.ts)}</small></div>"
        f'<div class="cm-v">{cur}{last.close:,.{decimals}f} <small>{esc(unit)}</small></div>'
        f'<div><span class="{tone}">{chg:+.2%} today</span> <small>(vs day open '
        f"{first.open:,.{decimals}f})</small></div></div>",
        unsafe_allow_html=True,
    )


def forex_page() -> None:
    st.markdown("#### Forex Market")
    st.caption("Live FX rates, session high/low charts, related news and AI analysis.")
    pair = st.radio("Pair", list(mk.FOREX), horizontal=True, key="fx_pair")
    inst = mk.FOREX[pair]
    with st.spinner("Loading live rates…"):
        day = mk.last_day(intraday(inst.symbol, "5d", "15m"))
    left, right = st.columns([2.2, 1])
    left_box = left.container()
    with left_box.container(border=True, key="fx_price"):
        instrument_header(
            pair,
            inst.symbol,
            day,
            "",
            pair.split("/")[1] + " per " + pair.split("/")[0],
            inst.decimals,
        )
    with right.container(border=True, key="fx_news", height=NEWS_HEIGHT):
        news = instrument_news("fx", pair)
        news_panel(news, f"Latest {pair} news")
        st.caption("Yahoo Finance + Google News RSS")
    dbars, ex = session_charts(inst.symbol, "fx", 1.0, inst.decimals)
    with left_box:  # AI panel sits beside the news column (no empty space)
        ai_panel(
            "forex",
            pair,
            mk.build_evidence(pair, pair.split("/")[1], dbars, ex, news, inst.decimals),
            "fx",
        )


def crypto_page() -> None:
    st.markdown("#### Crypto Market")
    st.caption("Live crypto prices (24/7), session high/low charts, news and AI analysis.")
    coin = st.radio("Coin", list(mk.CRYPTO), horizontal=True, key="cr_coin")
    # Prices are shown in USD only (the INR conversion helper was removed).
    cur, sym, k = "USD", "$", 1.0
    inst = mk.CRYPTO[coin]
    with st.spinner("Loading live prices…"):
        day = mk.scale(mk.last_day(intraday(inst.symbol, "5d", "15m")), k)
    left, right = st.columns([2.2, 1])
    left_box = left.container()
    with left_box.container(border=True, key="cr_price"):
        instrument_header(f"{inst.name} ({coin})", inst.symbol, day, sym, cur, inst.decimals)
    with right.container(border=True, key="cr_news", height=NEWS_HEIGHT):
        news = instrument_news("crypto", coin)
        news_panel(news, f"Latest {inst.name} news")
        st.caption("Yahoo Finance + Google News RSS")
    dbars, ex = session_charts(inst.symbol, "cr", k, inst.decimals)
    with left_box:  # AI panel sits beside the news column (no empty space)
        ai_panel(
            "crypto",
            f"{coin} ({cur})",
            mk.build_evidence(coin, cur, dbars, ex, news, inst.decimals),
            "cr",
        )


# ---------------------------------------------------------------- shell
PAGES = {
    "Home": ("home", home_page),
    "Research": ("research", research_page),
    "Comprehensive Research": ("comprehensive", comprehensive_page),
    "Workflow Approvals": ("approvals", approvals_page),
    "Research Report": ("report", report_page),
    "All Research Reports": ("reports", reports_page),
    "Watchlist": ("watchlist", watchlist_page),
    "Commodity Market": ("commodity", commodity_page),
    "Forex Market": ("forex", forex_page),
    "Crypto Market": ("crypto", crypto_page),
    "Provider Health": ("health", health_page),
    "Agent Memory": ("memory", memory_page),
    "Agent Backtest": ("backtest", backtest_page),
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
            width="stretch",
            type="primary" if ss.nav == name else "secondary",
        ):
            ss.nav = name
            st.rerun()

PAGES[ss.nav][1]()
