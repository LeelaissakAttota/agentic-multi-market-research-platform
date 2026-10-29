"""Report tab: Bull vs Bear debate, judge, risk panel and final rating (dashboard only)."""

from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

from amrp.application.debate import Debate, evidence_from_findings, run_debate
from amrp.infrastructure.llm.providers import build_tier_routers, load_config

_SIDE = {"bull": ("🐂 Bull", "#1f9d55"), "bear": ("🐻 Bear", "#d64545")}


def _findings(res: dict[str, Any]) -> dict[str, Any]:
    raw = res.get("raw")
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return dict(data.get("findings") or {})


def compute_debate(res: dict[str, Any], company: str) -> Debate:
    cfg = load_config()
    routers = build_tier_routers(cfg)
    return run_debate(
        evidence_from_findings(_findings(res)),
        company,
        rounds=cfg.debate_rounds,
        quick=routers.quick,
        deep=routers.deep,
        quick_label=routers.quick_label,
        deep_label=routers.deep_label,
    )


def debate_section(debate: Debate) -> None:
    v = debate.verdict
    c = debate.citations
    st.markdown(
        f"**Judge ({html.escape(v.by)})** leans **{v.leaning.upper()}** "
        f"(score {v.score:+.2f}) - {html.escape(v.rationale)}"
    )
    st.caption(
        f"Citation check: {c.verified_points}/{c.total_points} points verified"
        + (f" - unknown tags {', '.join(c.unknown_tags)}" if c.unknown_tags else "")
        + (f" - {c.numeric_mismatches} number mismatch(es)" if c.numeric_mismatches else "")
        + ". Unverified points do not count toward the verdict."
    )
    rounds = sorted({a.round for a in debate.arguments})
    for rnd in rounds:
        cols = st.columns(2)
        for col, side in zip(cols, ("bull", "bear"), strict=True):
            arg = next((a for a in debate.arguments if a.round == rnd and a.side == side), None)
            if arg is None:
                continue
            label, color = _SIDE[side]
            with col.container(border=True):
                st.markdown(
                    f'<div style="color:{color};font-weight:700">{label} - round {rnd} '
                    f'<span style="font-weight:400;color:#8FA3C7;font-size:.75rem">'
                    f"{html.escape(arg.by)}</span></div>",
                    unsafe_allow_html=True,
                )
                for p in arg.points:
                    tags = " ".join(f"`{t}`" for t in p.tags)
                    mark = "✅" if p.verified else "⚠️"
                    note = f" _({'; '.join(p.issues)})_" if p.issues else ""
                    st.markdown(f"{mark} {p.text} {tags}{note}")


def deliberation_tab(res: dict[str, Any], company: str, key: str) -> None:
    """Render (and lazily compute, once per report) the multi-agent analysis."""

    if st.button("Re-run agents", key=f"{key}_rerun"):
        res.pop("debate", None)
    if "debate" not in res:
        with st.spinner("Bull and Bear agents are debating the evidence..."):
            res["debate"] = compute_debate(res, company)
    debate_section(res["debate"])
