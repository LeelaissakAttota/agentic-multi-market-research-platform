"""AERP dashboard: one question box, approvals, step-by-step mode, settings.

Run:  streamlit run dashboard/app.py   (or .\\scripts\\start-dashboard.ps1 on Windows)
Talks only to the local REST API (AERP_API_URL) and control API (AERP_CONTROL_URL).
"""

from __future__ import annotations

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
    provider_strip,
)

API = JsonClient(os.environ.get("AERP_API_URL", "http://127.0.0.1:8000"))
CONTROL = JsonClient(
    os.environ.get("AERP_CONTROL_URL", "http://127.0.0.1:8712"),
    token=os.environ.get("EQUITY_RESEARCH_CONTROL_TOKEN", ""),
    timeout=30,
)
OBJECTIVE_LABELS = {
    "company_overview": "Company overview",
    "market_analysis": "Market",
    "financial_analysis": "Financials",
    "news_and_events": "News & events",
    "industry_analysis": "Industry",
    "regulatory_analysis": "Regulatory",
    "comprehensive_equity_research": "Comprehensive research (needs approval)",
}

st.set_page_config(page_title="AERP - Equity Research", page_icon="📈", layout="wide")
ss = st.session_state


def health_strip() -> None:
    try:
        API.get("/health")
        providers = provider_strip(API.get("/health/providers"))
    except ApiError as exc:
        st.error(f"API not reachable - start it with scripts/start-dashboard.ps1 ({exc.message})")
        return
    if not providers:
        st.caption("🟢 API online · data providers: offline sample mode (no live calls yet)")
        return
    st.caption("🟢 API online · providers: " + " · ".join(f"{i} {n}" for n, _, i in providers))


# ---------------------------------------------------------------- research
def start_research(question: str, pick: dict[str, str] | None = None) -> None:
    ss.pop("result", None)
    ss.pop("pending", None)
    ss.pop("choices", None)
    intent = parse_intent(question)
    ss.question, ss.intent = question, intent
    body: dict[str, Any] = {"objective": intent.objective, "objective_text": question[:512]}
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
        return
    run_workflow(created["workflow_id"], company)


def run_workflow(workflow_id: str, company: str) -> None:
    with st.spinner(f"Researching {company}… (live sources can take up to a minute)"):
        executed = API.post(f"/research/workflows/{workflow_id}/execute")
        rep = API.post(f"/research/workflows/{workflow_id}/report", format="json")
        md = API.post(f"/research/workflows/{workflow_id}/report", format="markdown")
    ss.result = {
        "view": build_report_view(rep["stored_report"]["content"], company),
        "markdown": md["stored_report"]["content"],
        "workflow": executed.get("workflow") or {},
        "workflow_id": workflow_id,
    }


def decide(decision: str) -> None:
    pending = ss.pop("pending")
    note = "approved in dashboard" if decision == "approved" else "rejected in dashboard"
    API.post(
        f"/research/workflows/{pending['workflow_id']}/approval",
        {"decision": decision, "note": note},
    )
    if decision == "approved":
        run_workflow(pending["workflow_id"], pending["company"])
    else:
        ss.info = f"Rejected. Nothing was run for {pending['company']}."


def render_result() -> None:
    res = ss.result
    view, wf = res["view"], res["workflow"]
    st.subheader(f"📊 {view.company}")
    cols = st.columns(4)
    cols[0].metric("Status", str(wf.get("status", "?")).upper())
    cols[1].metric("Tasks", wf.get("task_count", "-"))
    cols[2].metric("Evidence items", wf.get("evidence_count", "-"))
    cols[3].metric("Approval", str(wf.get("approval_status", "-")).replace("_", " "))
    with st.container(border=True):
        st.markdown(f"**🧠 Analyst summary** · _by {view.summary_by}_")
        st.write(view.summary)
        if view.summary_note:
            st.caption(view.summary_note + " Not investment advice.")
    left, right = st.columns(2)
    for i, sec in enumerate(view.sections):
        box = (left if i % 2 == 0 else right).container(border=True)
        box.markdown(f"**{sec.title}** {badge_markdown(sec.origin, sec.status)}")
        meta = " · ".join(x for x in (sec.status, sec.as_of and f"as of {sec.as_of}") if x)
        if meta:
            box.caption(meta + (f" — {sec.message}" if sec.message else ""))
        if sec.rows and "label" in sec.rows[0]:
            box.dataframe(
                [
                    {
                        "Ref": r.get("tag"),
                        "Metric": r.get("label"),
                        "Value": r.get("value"),
                        "Detail": r.get("detail"),
                    }
                    for r in sec.rows
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            for r in sec.rows[:6]:
                link = f" [link]({r['url']})" if r.get("url") else ""
                box.markdown(
                    f"- **[{r.get('tag')}]** {r.get('date') or ''} — {r.get('title')}"
                    f" {badge_markdown(r.get('data_origin'))}{link}"
                )
    st.download_button(
        "⬇️ Download report (Markdown)",
        res["markdown"],
        file_name=f"AERP-{view.company.replace(' ', '_')}.md",
        mime="text/markdown",
    )
    with st.expander("Full report"):
        st.markdown(res["markdown"])


def research_page() -> None:
    st.title("📈 Equity Research")
    st.caption(
        "Ask about any listed company in India (NSE/BSE) or the US. Evidence-first, "
        "every number shows where it came from. Research only - not investment advice."
    )
    health_strip()
    st.write("**Try an example:**")
    chip_cols = st.columns(len(EXAMPLE_PROMPTS))
    clicked = None
    for col, (label, prompt) in zip(chip_cols, EXAMPLE_PROMPTS, strict=True):
        if col.button(label, use_container_width=True):
            clicked = prompt
    with st.form("ask", clear_on_submit=False):
        question = st.text_input(
            "Company and question",
            value=clicked or ss.get("question", ""),
            placeholder="e.g. Is Reliance's growth holding up, and what are the risks?",
        )
        submitted = st.form_submit_button("🔍 Research", type="primary")
    try:
        if clicked or (submitted and question.strip()):
            start_research(clicked or question.strip())
        if "intent" in ss and ("pending" in ss or "result" in ss or "choices" in ss):
            st.caption(
                f"Detected objective: **{OBJECTIVE_LABELS[ss.intent.objective]}** "
                f"({ss.intent.reason})"
            )
        if "choices" in ss:
            st.warning("More than one company matches. Which one did you mean?")
            for i, ch in enumerate(ss.choices):
                if st.button(ch["label"], key=f"pick{i}"):
                    start_research(ss.question, pick=ch)
                    st.rerun()
        if "pending" in ss:
            p = ss.pending
            with st.container(border=True):
                st.warning(
                    f"⏸️ **Approval needed** - comprehensive research on **{p['company']}** "
                    "calls several data sources. Approve to run it, or Reject to cancel."
                )
                a, r, _ = st.columns([1, 1, 4])
                if a.button("✅ Approve", type="primary"):
                    decide("approved")
                    st.rerun()
                if r.button("❌ Reject"):
                    decide("rejected")
                    st.rerun()
        if "result" in ss:
            render_result()
    except ApiError as exc:
        st.error(f"Something went wrong: {exc.message}")
    for key in ("error", "info"):
        if key in ss:
            getattr(st, key)(ss.pop(key))


# ---------------------------------------------------------------- step-by-step
def steps_page() -> None:
    st.title("🪜 Step-by-step mode")
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
            "What to research",
            list(OBJECTIVE_LABELS),
            index=6,
            format_func=lambda k: OBJECTIVE_LABELS[k],
        )
        go = st.form_submit_button("▶️ Start step-by-step run", type="primary")
    try:
        if go:
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


# ---------------------------------------------------------------- settings
def settings_page() -> None:
    st.title("⚙️ Settings")
    st.subheader("1. AI provider (API key)")
    st.caption(
        "Keys are write-only: saved in your local key store, only the last 4 "
        "characters are ever shown. Never paste keys into chats or screenshots."
    )
    masked: dict[str, Any] = {}
    try:
        masked = CONTROL.get("/control/keys").get("keys", {}) if CONTROL.token else {}
    except ApiError as exc:
        st.warning(f"Key store unavailable ({exc.message}).")
    spec = st.selectbox("Provider", PROVIDERS, format_func=lambda p: p.label)
    info = masked.get(spec.key_name, {})
    st.markdown(
        "Status: "
        + ("✅ saved " + str(info.get("masked", "")) if info.get("configured") else "⚪ not set")
        + (f" · get a key at {spec.signup_url}" if spec.signup_url else "")
    )
    if not spec.used_by_backend:
        st.info(
            "Saved for later. Research summaries currently use OpenRouter free models only "
            "(project cost policy)."
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

    st.subheader("2. Model choice (automatic, lowest cost)")
    task = st.radio("Task", ["summary", "planning", "report"], horizontal=True)
    if st.button("Show which model would be used"):
        try:
            models = JsonClient("https://openrouter.ai/api/v1", timeout=20).get("/models")
            choice = choose_model(models.get("data", []), task)
            if choice.model_id:
                st.success(f"**{choice.model_id}** - {choice.reason}")
            else:
                st.warning(choice.reason)
        except ApiError as exc:
            st.error(f"Could not load the model list: {exc.message}")
    st.caption(
        "Rule: free models first. Paid models are OFF by project policy "
        "(ALLOW_PAID_MODELS=false); enabling the cheapest paid fallback needs your "
        "explicit decision."
    )

    st.subheader("3. Live data sources")
    prefs = load_prefs()
    with st.form("live"):
        values: dict[str, object] = {
            k: st.toggle(label, value=bool(prefs.get(k))) for k, (_, label) in LIVE_TOGGLES.items()
        }
        values["sec_user_agent"] = st.text_input(
            "SEC contact (name + email, required by SEC for live US filings)",
            value=str(prefs.get("sec_user_agent", "")),
        )
        if st.form_submit_button("Save live-data settings"):
            save_prefs(values)
            st.success("Saved. Restart the dashboard (start-dashboard.ps1) to apply.")
    st.caption("Off = labelled SAMPLE DATA (works offline). On = real data from public sources.")


# ---------------------------------------------------------------- health
def health_page() -> None:
    st.title("🩺 Health")
    try:
        st.json(API.get("/version"))
        rows = [
            {"Provider": n, "State": f"{i} {s}"}
            for n, s, i in provider_strip(API.get("/health/providers"))
        ]
        st.dataframe(rows or [{"Provider": "none called yet", "State": "-"}], hide_index=True)
        recent = API.get("/research/workflows", limit=10).get("workflows", [])
        st.subheader("Recent research")
        st.dataframe(
            [
                {
                    "Objective": w.get("objective"),
                    "Status": w.get("status"),
                    "Approval": w.get("approval_status"),
                    "Created": w.get("created_at"),
                }
                for w in recent
            ],
            hide_index=True,
        )
    except ApiError as exc:
        st.error(exc.message)


PAGES = {
    "🔍 Research": research_page,
    "🪜 Step-by-step": steps_page,
    "⚙️ Settings": settings_page,
    "🩺 Health": health_page,
}
choice = st.sidebar.radio("Menu", list(PAGES))
st.sidebar.caption("AERP · evidence-first equity research\nby Leela Issak")
PAGES[choice]()
