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

import streamlit as st

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

CSS = """
<style>
:root { --navy:#0A1428; --panel:#0F1E3A; --line:#1E3460; --blue:#2F7BFF; --muted:#8FA3C7; }
[data-testid="stAppViewContainer"] { background: radial-gradient(1200px 600px at 70% -10%,
  #12306b 0%, var(--navy) 55%); }
[data-testid="stSidebar"] { background: #08111F; border-right: 1px solid var(--line); }
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.4rem; }
.aerp-logo { display:flex; align-items:center; gap:.6rem; margin-bottom:.4rem; }
.aerp-logo .mark { width:38px; height:38px; border-radius:9px; background:
  linear-gradient(135deg,#2F7BFF,#16C1FF); display:flex; align-items:end; justify-content:center;
  gap:3px; padding:7px; }
.aerp-logo .mark i { display:block; width:5px; background:#fff; border-radius:2px; }
.aerp-logo b { font-size:1.35rem; letter-spacing:.06em; color:#fff; }
.aerp-logo small { display:block; color:var(--muted); font-size:.72rem; margin-top:-4px; }
.card { background: linear-gradient(180deg,#112449,#0E1C38); border:1px solid var(--line);
  border-radius:14px; padding:14px 16px; height:100%; }
.card h4 { margin:0 0 .2rem 0; font-size:.82rem; color:var(--muted); font-weight:500; }
.card .v { font-size:1.7rem; font-weight:700; color:#fff; line-height:1.2; }
.card .s { font-size:.75rem; color:var(--muted); }
.stat-blue { border-left:4px solid #2F7BFF; } .stat-green { border-left:4px solid #22C55E; }
.stat-amber { border-left:4px solid #F59E0B; } .stat-red { border-left:4px solid #EF4444; }
.hero { background: linear-gradient(120deg,#123A8C 0%, #0E2350 60%, #0B1A3A 100%);
  border:1px solid var(--line); border-radius:16px; padding:22px 24px; margin-bottom:12px; }
.hero h1 { font-size:1.9rem; margin:0; color:#fff; }
.hero p { color:#C9D6F2; margin:.3rem 0 0 0; }
.pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:.72rem;
  font-weight:600; }
.p-green { background:#123d2a; color:#4ADE80; } .p-amber { background:#3d2e10; color:#FBBF24; }
.p-red { background:#3d1414; color:#F87171; } .p-blue { background:#132f5e; color:#7FB0FF; }
.p-gray { background:#1f2a40; color:#A9B6CF; }
.steps { display:flex; justify-content:space-between; margin:8px 0 4px 0; }
.step { text-align:center; flex:1; color:var(--muted); font-size:.8rem; }
.step .dot { width:30px; height:30px; border-radius:50%; margin:0 auto 4px auto;
  display:flex; align-items:center; justify-content:center; background:#1a2a4a;
  border:2px solid #2a3f6a; color:#fff; font-weight:700; }
.step.done .dot { background:#1d4ed8; border-color:#3b82f6; }
.step.current .dot { background:#F59E0B; border-color:#FBBF24; }
.step.failed .dot { background:#b91c1c; border-color:#ef4444; }
.side-foot { background:#0F1E3A; border:1px solid var(--line); border-radius:12px; padding:10px;
  font-size:.75rem; color:var(--muted); }
div[data-testid="stVerticalBlockBorderWrapper"] { background: rgba(15,30,58,.65);
  border-color: var(--line) !important; border-radius: 14px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- data access
def safe(fn: Any, *args: Any, default: Any = None, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except ApiError:
        return default


def all_workflows() -> list[dict[str, Any]]:
    api = safe(API.get, "/research/workflows", limit=100, default={}) or {}
    return merge_workflows(api.get("workflows", []), history_as_workflows(load_history()))


def pill(text: str, tone: str) -> str:
    return f'<span class="pill p-{tone}">{text}</span>'


def status_pill(label: str) -> str:
    tone = {
        "Completed": "green",
        "Partial": "blue",
        "Pending approval": "amber",
        "Failed": "red",
        "Rejected": "red",
        "Cancelled": "red",
    }.get(label, "gray")
    return pill(label, tone)


def origin_pill(origin: str | None, status: str | None = None) -> str:
    label, colour = origin_badge(origin, status)
    tone = {"green": "green", "blue": "blue", "orange": "amber"}.get(colour, "gray")
    return pill(label, tone)


def card(title: str, value: str, sub: str = "", tone: str = "blue") -> str:
    return (
        f'<div class="card stat-{tone}"><h4>{title}</h4><div class="v">{value}</div>'
        f'<div class="s">{sub}</div></div>'
    )


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


def search_box(key: str, button: str = "Start Research", objective: str = "") -> None:
    with st.form(f"ask_{key}", clear_on_submit=False, border=False):
        c1, c2 = st.columns([5, 1])
        q = c1.text_input(
            "Company or question",
            value=ss.get(f"prefill_{key}", ""),
            placeholder="Enter company name or ticker… e.g. Reliance, TCS, Apple (AAPL)",
            label_visibility="collapsed",
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


# ---------------------------------------------------------------- pages
def home_page() -> None:
    st.markdown(
        '<div class="hero"><h1>Welcome to AERP</h1>'
        "<p>Your Agentic Equity Research Platform — evidence-backed research reports for "
        "Indian &amp; US equities. Research only, not investment advice.</p></div>",
        unsafe_allow_html=True,
    )
    search_box("home")
    st.caption("Popular searches")
    chips(list(EXAMPLE_PROMPTS), "homechip")
    flash()
    wfs = all_workflows()
    stats = workflow_stats(wfs)
    c = st.columns(4)
    c[0].markdown(
        card("Total Research", str(stats.total), "this dashboard's run history"),
        unsafe_allow_html=True,
    )
    c[1].markdown(
        card("Completed", str(stats.completed), "completed or partial", "green"),
        unsafe_allow_html=True,
    )
    c[2].markdown(
        card("Pending Approval", str(stats.pending), "waiting for you", "amber"),
        unsafe_allow_html=True,
    )
    c[3].markdown(
        card("Failed / Rejected", str(stats.failed), "needs attention", "red"),
        unsafe_allow_html=True,
    )
    st.write("")
    left, mid, right = st.columns([1.1, 1.2, 1.2])
    with left.container(border=True):
        st.markdown("**Market Overview**")
        st.caption(
            "Index levels (NIFTY 50, SENSEX, S&P 500) are not available: no index "
            "provider is configured in the backend."
        )
        snap = safe(API.get, "/market/snapshot", q="Apple", default=None)
        price, cur, origin = latest_close(snap)
        if price is not None:
            st.markdown(
                f"Apple (AAPL) last close: **{cur} {price:,.2f}** " + origin_pill(origin),
                unsafe_allow_html=True,
            )
            series = close_series(snap)
            if series:
                st.line_chart({"close": [v for _, v in series]}, height=120)
        else:
            st.caption(f"Sample price: {NA}")
    with mid.container(border=True):
        st.markdown("**Recent Research**")
        rows = workflow_rows(wfs)[:6]
        if not rows:
            st.caption("No research yet. Search a company above.")
        for r in rows:
            st.markdown(
                f"**{r['Company']}** · {r['Objective']}  {status_pill(r['Status'])}  "
                f"<span class='s' style='color:#8FA3C7;font-size:.75rem'>{r['Created']}</span>",
                unsafe_allow_html=True,
            )
    with right.container(border=True):
        st.markdown("**Top News**")
        name = (wfs and workflow_rows(wfs)[0]["Company"]) or "Apple"
        news = safe(API.get, "/news/events/snapshot", q=name, default=None) or {}
        events = (news.get("package") or {}).get("events") or []
        if not events:
            st.caption(f"News for {name}: {NA}")
        else:
            st.markdown(
                f"For **{name}** " + origin_pill(news.get("data_origin")), unsafe_allow_html=True
            )
            for ev in events[:5]:
                url = (ev.get("evidence") or {}).get("source_url")
                link = f" [↗]({url})" if url else ""
                st.markdown(f"- {ev.get('event_date', '')} — {ev.get('title')}{link}")


def research_page() -> None:
    st.markdown("### Start New Research")
    main, side = st.columns([2.2, 1])
    with main:
        obj = ss.get("objective_pick", "")
        search_box("research", "Search", objective=obj)
        st.markdown("**Popular Companies**")
        chips([(n, n) for n in POPULAR_COMPANIES], "popco", objective=obj)
        st.markdown(
            "**Research Objective**  ·  _leave unselected to auto-detect from your question_"
        )
        keys = list(OBJECTIVE_LABELS)
        for row in (keys[:4], keys[4:]):
            cols = st.columns(4)
            for col, k in zip(cols, row, strict=False):
                picked = ss.get("objective_pick") == k
                with col.container(border=True):
                    st.markdown(f"**{OBJECTIVE_LABELS[k]}**" + (" ✅" if picked else ""))
                    st.caption(OBJECTIVE_HINTS[k])
                    if st.button(
                        "Selected" if picked else "Select",
                        key=f"obj_{k}",
                        type="primary" if picked else "secondary",
                    ):
                        ss.objective_pick = "" if picked else k
                        st.rerun()
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
                    f"⏸️ **Approval needed** — comprehensive research on **{p['company']}** "
                    "calls several data sources. Approve to run it, or Reject to cancel."
                )
                a, r, _ = st.columns([1, 1, 4])
                if a.button("✅ Approve", type="primary", key="ap_inline"):
                    decide(p["workflow_id"], p["company"], "approved")
                    go("Research Report")
                    st.rerun()
                if r.button("❌ Reject", key="rj_inline"):
                    decide(p["workflow_id"], p["company"], "rejected")
                    st.rerun()
        st.markdown("**Recent Searches**")
        rows = workflow_rows(all_workflows())[:10]
        if rows:
            st.dataframe(
                [{k: v for k, v in r.items() if k != "workflow_id"} for r in rows],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("No searches yet.")
    with side.container(border=True):
        st.markdown("**Suggested Companies**")
        for n in POPULAR_COMPANIES[:4]:
            res = safe(API.get, "/companies/resolve", q=n, default={}) or {}
            comp = res.get("company") or {}
            if res.get("status") != "RESOLVED":
                continue
            listings = res.get("listings") or comp.get("listings") or []
            tick = next(
                (
                    f"{x.get('exchange')}:{x.get('ticker')}"
                    for x in listings
                    if isinstance(x, dict) and x.get("ticker")
                ),
                "",
            )
            c1, c2 = st.columns([3, 1])
            c1.markdown(
                f"**{comp.get('display_name') or n}**  \n"
                f"<span style='color:#8FA3C7;font-size:.75rem'>{tick}</span>",
                unsafe_allow_html=True,
            )
            if c2.button("View", key=f"sug_{n}"):
                start_research(n, objective=obj)
                go("Research Report" if "result" in ss else "Research")
                st.rerun()


def tracker(workflow: dict[str, Any] | None) -> None:
    icons = {"done": "✓", "current": "…", "todo": "", "failed": "✕"}
    html = "".join(
        f'<div class="step {state}"><div class="dot">{icons[state] or i + 1}</div>{name}</div>'
        for i, (name, state) in enumerate(step_states(workflow))
    )
    st.markdown(f'<div class="steps">{html}</div>', unsafe_allow_html=True)


def approvals_page() -> None:
    st.markdown("### Pending Approvals")
    st.caption("Review and approve research workflows before execution.")
    flash()
    wfs = all_workflows()
    groups = split_approvals(wfs)
    c = st.columns(4)
    c[0].markdown(card("Pending", str(len(groups["pending"])), "", "amber"), unsafe_allow_html=True)
    c[1].markdown(
        card("Approved", str(len(groups["approved"])), "", "green"), unsafe_allow_html=True
    )
    c[2].markdown(card("Rejected", str(len(groups["rejected"])), "", "red"), unsafe_allow_html=True)
    c[3].markdown(card("Total", str(len(wfs)), "all workflows"), unsafe_allow_html=True)
    st.write("")
    with st.container(border=True):
        st.markdown(f"**Pending Approval ({len(groups['pending'])})**")
        if not groups["pending"]:
            st.caption("Nothing is waiting. Comprehensive research requests appear here.")
        for w in groups["pending"]:
            wid = str(w["workflow_id"])
            row = workflow_rows([w])[0]
            a, b, c3, d, e = st.columns([2, 2, 2, 1, 1])
            a.write(row["Company"])
            b.write(row["Objective"])
            c3.write(row["Created"])
            if d.button("Approve", key=f"apv_{wid}", type="primary"):
                try:
                    decide(wid, row["Company"], "approved")
                    go("Research Report")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
            if e.button("Reject", key=f"rej_{wid}"):
                try:
                    decide(wid, row["Company"], "rejected")
                except ApiError as exc:
                    ss.error = exc.message
                st.rerun()
    left, right = st.columns([1.4, 1])
    with left.container(border=True):
        st.markdown("**Workflow Steps**")
        latest = (
            groups["pending"][:1]
            or sorted(wfs, key=lambda w: str(w.get("created_at") or ""), reverse=True)[:1]
        )
        if latest:
            st.caption(
                f"{workflow_rows(latest)[0]['Company']} · {workflow_rows(latest)[0]['Objective']}"
            )
        tracker(latest[0] if latest else None)
    with right.container(border=True):
        st.markdown("**Recent Activity**")
        audit = safe(CONTROL.get, "/control/audit", default=None) if CONTROL.token else None
        items = (audit or {}).get("events") or (audit or {}).get("audit") or []
        if items:
            for ev in items[-6:][::-1]:
                st.markdown(
                    f"- {ev.get('action') or ev.get('event')} · "
                    f"{str(ev.get('at') or ev.get('timestamp') or '')[:16]}"
                )
        else:
            for r in workflow_rows(wfs)[:6]:
                st.markdown(f"- {r['Company']} — {r['Status']} · {r['Created']}")
            if not wfs:
                st.caption("No activity yet.")
    st.divider()
    steps_mode()


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


def _rows_table(rows: list[dict[str, Any]]) -> None:
    st.dataframe(
        [
            {
                "Ref": r.get("tag"),
                "Metric": r.get("label"),
                "Value": r.get("value"),
                "Detail": r.get("detail"),
            }
            for r in rows
        ],
        hide_index=True,
        use_container_width=True,
    )


def _items(rows: list[dict[str, Any]]) -> None:
    for r in rows[:10]:
        link = f" [link]({r['url']})" if r.get("url") else ""
        st.markdown(
            f"- **[{r.get('tag')}]** {r.get('date') or ''} — {r.get('title')} "
            f"{badge_markdown(r.get('data_origin'))}{link}"
        )


def report_page() -> None:
    reports: dict[str, Any] = ss.get("reports", {})
    if not reports:
        st.markdown("### Research Report")
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
        f"### {view.company}  "
        + status_pill(str(wf.get("status", "?")).capitalize())
        + " "
        + " ".join(origin_pill(o) for o in sorted(origins)),
        unsafe_allow_html=True,
    )
    head.caption(
        f"{objective_label(wf.get('objective'))} · workflow {wid[:8]} · "
        f"{wf.get('evidence_count', '-')} evidence items"
    )
    dl.download_button(
        "⬇️ Download Report",
        res["markdown"],
        file_name=f"AERP-{view.company.replace(' ', '_')}.md",
        mime="text/markdown",
        use_container_width=True,
    )
    tabs = st.tabs(list(REPORT_TABS))
    with tabs[0]:
        c1, c2, c3 = st.columns([1, 1.4, 1])
        mrows = findings.get("market_figures") or []
        frows = findings.get("financial_figures") or []
        with c1.container(border=True):
            st.markdown("**Key Figures**")
            for label in (
                "Last close",
                "Simple return",
                "SMA (simple moving average)",
                "Revenue growth",
                "Net margin",
                "Debt to equity",
                "Free cash flow",
            ):
                v = figure_value(mrows, label) or figure_value(frows, label)
                st.markdown(f"{label}: **{v or NA}**")
        with c2.container(border=True):
            st.markdown("**Price (recent sessions)**")
            snap = safe(API.get, "/market/snapshot", q=view.company.split(" (")[0], default=None)
            series = close_series(snap)
            if series:
                st.markdown(origin_pill((snap or {}).get("data_origin")), unsafe_allow_html=True)
                st.bar_chart({"close": {d: v for d, v in series}}, height=220)
            else:
                st.caption(f"Price history: {NA}")
        with c3.container(border=True):
            st.markdown("**Top News**")
            if findings.get("news_items"):
                _items(findings["news_items"][:4])
            else:
                st.caption(NA)
        with st.container(border=True):
            st.markdown(f"**🧠 Analyst Summary** · _by {view.summary_by}_")
            st.write(view.summary)
            if view.summary_note:
                st.caption(view.summary_note + " Not investment advice.")
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
            st.dataframe(refs, hide_index=True, use_container_width=True)
        else:
            st.caption(f"Evidence references: {NA}.")


def reports_page() -> None:
    st.markdown("### Research Reports")
    st.caption("Search, filter and export your research reports.")
    flash()
    rows = workflow_rows(all_workflows())
    c1, c2, c3 = st.columns([3, 2, 2])
    text = c1.text_input("Search", placeholder="Search reports…", label_visibility="collapsed")
    company = c2.selectbox(
        "Company", ["All", *sorted({r["Company"] for r in rows})], label_visibility="collapsed"
    )
    objective = c3.selectbox(
        "Objective", ["All", *sorted({r["Objective"] for r in rows})], label_visibility="collapsed"
    )
    shown = filter_reports(rows, text, company, objective)
    st.download_button(
        "⬇️ Export CSV",
        to_csv(shown) or "no rows\n",
        file_name="aerp-reports.csv",
        mime="text/csv",
        disabled=not shown,
    )
    reports = ss.get("reports", {})
    with st.container(border=True):
        if not shown:
            st.caption("No reports match.")
        for r in shown:
            a, b, c, d, e = st.columns([2, 2, 1.4, 2, 1])
            a.write(r["Company"])
            b.write(r["Objective"])
            c.markdown(status_pill(r["Status"]), unsafe_allow_html=True)
            d.write(r["Created"])
            if r["workflow_id"] in reports:
                if e.button("View", key=f"view_{r['workflow_id']}"):
                    ss.selected_report = r["workflow_id"]
                    go("Research Report")
                    st.rerun()
            elif r["Status"] in ("Completed", "Partial"):
                if e.button("Open", key=f"open_{r['workflow_id']}"):
                    try:
                        open_report(r["workflow_id"], r["Company"])
                        go("Research Report")
                    except ApiError as exc:
                        ss.error = f"Report not available ({exc.message})"
                    st.rerun()
            elif r["Status"] == "Pending approval":
                if e.button("Review", key=f"rev_{r['workflow_id']}"):
                    go("Workflow Approvals")
                    st.rerun()
            else:
                e.caption("-")


def watchlist_page() -> None:
    st.markdown("### My Watchlist")
    st.caption(
        "Stored locally on this computer. Prices come from /market/snapshot; the badge "
        "shows whether each price is LIVE or SAMPLE."
    )
    with st.form("wl", border=False):
        c1, c2 = st.columns([4, 1])
        name = c1.text_input(
            "Add", placeholder="Company name or ticker", label_visibility="collapsed"
        )
        if c2.form_submit_button("+ Add", use_container_width=True) and name.strip():
            add_to_watchlist(name)
            st.rerun()
    items = load_watchlist()
    if not items:
        st.info("Your watchlist is empty. Add Reliance, TCS or Apple to start.")
        return
    with st.container(border=True):
        h = st.columns([2, 2, 2, 2, 1, 1])
        for col, t in zip(h, ["Company", "Ticker", "Last price", "Source", "", ""], strict=True):
            if t:
                col.markdown(f"**{t}**")
        for it in items:
            snap = safe(API.get, "/market/snapshot", q=it, default=None)
            price, cur, origin = latest_close(snap)
            listing = (snap or {}).get("listing") or {}
            c = st.columns([2, 2, 2, 2, 1, 1])
            c[0].write(it)
            c[1].write(
                f"{listing.get('exchange', '')}:{listing.get('ticker', '')}" if listing else NA
            )
            c[2].write(f"{cur} {price:,.2f}" if price is not None else NA)
            c[3].markdown(
                origin_pill(origin) if price is not None else pill("NOT AVAILABLE", "gray"),
                unsafe_allow_html=True,
            )
            if c[4].button("Research", key=f"wr_{it}"):
                start_research(it)
                go("Research Report" if "result" in ss else "Research")
                st.rerun()
            if c[5].button("Remove", key=f"wd_{it}"):
                remove_from_watchlist(it)
                st.rerun()


def health_page() -> None:
    st.markdown("### Provider Health")
    st.caption(
        "Live status from /health/providers. Latency below is measured by the "
        "dashboard for the local services; provider latency is not reported by the "
        "backend."
    )
    c = st.columns(4)
    for col, (label, client, path) in zip(
        c,
        [
            ("REST API", API, "/health"),
            ("Readiness", API, "/ready"),
            ("Control API", CONTROL, "/control/health"),
            ("Version", API, "/version"),
        ],
        strict=True,
    ):
        t0 = time.perf_counter()
        ok = safe(client.get, path, default=None) is not None
        ms = (time.perf_counter() - t0) * 1000
        col.markdown(
            card(
                label,
                "Healthy" if ok else "Down",
                f"{ms:.0f} ms" if ok else "",
                "green" if ok else "red",
            ),
            unsafe_allow_html=True,
        )
    st.write("")
    left, right = st.columns([1.4, 1])
    with left.container(border=True):
        st.markdown("**Provider Status**")
        cards = provider_cards(safe(API.get, "/health/providers", default={}))
        if not cards:
            st.caption(
                "No live provider called yet (offline sample mode or nothing run). "
                "Turn on live data in Settings."
            )
        else:
            st.dataframe(
                [
                    {
                        "Provider": x["name"],
                        "State": x["state"],
                        "Successes": int(x["successes"] or 0),
                        "Failures": int(x["failures"] or 0),
                        "Last success": x["last_success"],
                        "Last error": x["last_error"] or "-",
                        "Latency": NA if x["latency_ms"] is None else f"{x['latency_ms']:.0f} ms",
                    }
                    for x in cards
                ],
                hide_index=True,
                use_container_width=True,
            )
    with right.container(border=True):
        st.markdown("**Recent Logs**")
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
        st.code("\n".join(logs) or "(no step-by-step run logs yet)")
        ver = safe(API.get, "/version", default=None)
        if ver:
            st.json(ver, expanded=False)


def settings_page() -> None:
    st.markdown("### Settings")
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
        st.dataframe(rows, hide_index=True, use_container_width=True)
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
    "Home": ("🏠", home_page),
    "Research": ("🔍", research_page),
    "Workflow Approvals": ("✅", approvals_page),
    "Research Report": ("📄", report_page),
    "Reports": ("🗂️", reports_page),
    "Watchlist": ("⭐", watchlist_page),
    "Provider Health": ("🩺", health_page),
    "Settings": ("⚙️", settings_page),
}
with st.sidebar:
    st.markdown(
        '<div class="aerp-logo"><div class="mark"><i style="height:10px"></i>'
        '<i style="height:16px"></i><i style="height:22px"></i></div>'
        "<div><b>AERP</b><small>Agentic Equity Research</small></div></div>",
        unsafe_allow_html=True,
    )
    pending_n = workflow_stats(all_workflows()).pending
    ss.setdefault("nav", "Home")
    for name, (icon, _) in PAGES.items():
        label = f"{icon}  {name}" + (
            f"  🔴 {pending_n}" if name == "Workflow Approvals" and pending_n else ""
        )
        if st.button(
            label,
            key=f"nav_{name}",
            use_container_width=True,
            type="primary" if ss.nav == name else "secondary",
        ):
            ss.nav = name
            st.rerun()
    st.write("")
    st.markdown(
        '<div class="side-foot"><b>Evidence-First Research</b><br>Real data. Clear '
        "sources. Every number shows where it came from.<br><br>by Leela Issak</div>",
        unsafe_allow_html=True,
    )

PAGES[ss.nav][1]()
