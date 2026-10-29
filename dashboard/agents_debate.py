"""Report tab: Bull vs Bear debate, judge, risk panel and final rating (dashboard only)."""

from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

from amrp.application.debate import DISCLAIMER, Debate
from amrp.application.deliberation import Deliberation, Rating, deliberate
from amrp.infrastructure.llm.providers import build_tier_routers, load_config

_SIDE = {"bull": ("🐂 Bull", "#1f9d55"), "bear": ("🐻 Bear", "#d64545")}


def _findings(res: dict[str, Any]) -> dict[str, Any]:
    raw = res.get("raw")
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return dict(data.get("findings") or {})


RATING_COLORS = {
    Rating.STRONG_BUY: "#0f7b3f",
    Rating.BUY: "#1f9d55",
    Rating.HOLD: "#b7791f",
    Rating.SELL: "#d64545",
    Rating.STRONG_SELL: "#9b1c1c",
}


def compute_deliberation(
    res: dict[str, Any],
    company: str,
    *,
    lessons: tuple[str, ...] = (),
    past_accuracy: float | None = None,
    as_of: str | None = None,
) -> Deliberation:
    cfg = load_config()
    routers = build_tier_routers(cfg) if cfg.llm_enabled else None
    return deliberate(
        _findings(res),
        company,
        rounds=cfg.debate_rounds,
        quick=routers.quick if routers else None,
        deep=routers.deep if routers else None,
        quick_label=routers.quick_label if routers else "deterministic",
        deep_label=routers.deep_label if routers else "deterministic",
        lessons=lessons,
        past_accuracy=past_accuracy,
        as_of=as_of,
    )


def rating_badge(d: Deliberation | None) -> str:
    if d is None:
        return ""
    r = d.rating
    color = RATING_COLORS[r.rating]
    return (
        f'<span style="background:{color};color:#fff;border-radius:999px;padding:2px 10px;'
        f'font-size:.8rem;font-weight:700">{html.escape(r.rating.value)} · '
        f"{r.confidence:.0%}</span>"
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


def risk_section(d: Deliberation) -> None:
    st.markdown("##### Risk panel")
    cols = st.columns(len(d.risk.views) or 1)
    for col, v in zip(cols, d.risk.views, strict=False):
        with col.container(border=True):
            st.metric(v.stance.capitalize(), f"{v.score:+.2f}")
            st.caption(f"{v.comment} ({v.by})")
    if d.risk.flags:
        st.markdown("**Risk flags:** " + " · ".join(d.risk.flags))
    st.caption(f"Execution risk: {d.risk.execution_risk}")


def rating_section(d: Deliberation) -> None:
    r = d.rating
    st.markdown(
        f'<div style="font-size:1.6rem;font-weight:800">{rating_badge(d)}</div>',
        unsafe_allow_html=True,
    )
    st.progress(r.confidence, text=f"Confidence {r.confidence:.0%} · score {r.score:+.2f}")
    st.caption(r.rationale)
    st.caption("Models: " + "; ".join(f"{k}: {v}" for k, v in d.models.items()))
    st.warning(DISCLAIMER)


def deliberation_tab(res: dict[str, Any], company: str, key: str) -> None:
    """Render the multi-agent analysis computed by :func:`ensure_deliberation`."""

    if st.button("Re-run agents", key=f"{key}_rerun"):
        res.pop("deliberation", None)
        ensure_deliberation(res, company)
    d = res.get("deliberation")
    if d is None:
        st.info("Agents have not run for this report yet.")
        return
    rating_section(d)
    st.divider()
    st.markdown("##### Bull vs Bear debate")
    debate_section(d.debate)
    st.divider()
    risk_section(d)
    if d.lessons:
        st.markdown("##### Lessons from past calls")
        for x in d.lessons:
            st.markdown(f"- {x}")


def ensure_deliberation(res: dict[str, Any], company: str) -> Deliberation:
    """Compute the deliberation once per report and cache it on the result."""

    d = res.get("deliberation")
    if d is None:
        with st.spinner("Bull, Bear, Judge and Risk agents are reviewing the evidence..."):
            d = compute_deliberation(res, company)
        res["deliberation"] = d
    return d
