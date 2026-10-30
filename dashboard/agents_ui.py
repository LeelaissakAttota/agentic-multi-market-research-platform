"""Dashboard UI for the multi-agent features: LLM tiers, debate/risk/rating, memory, backtest.

Kept out of ``dashboard/app.py`` so the page code stays small; every function here only
renders Streamlit widgets and calls the application layer.
"""

from __future__ import annotations

import streamlit as st

from amrp.infrastructure.llm.providers import (
    LLM_PROVIDERS,
    DeliberationConfig,
    TierChoice,
    env_lookup,
    load_config,
    provider_ready,
    save_config,
)


# ----------------------------------------------------------------- settings: AI agents
def agent_llm_settings() -> None:
    """Settings tab: which provider/model each agent tier uses, debate rounds, LLM on/off."""

    cfg = load_config()
    lookup = env_lookup()
    st.markdown("**AI agents - providers and models**")
    st.caption(
        "Analysts (bull, bear, risk views) use the **quick** tier; the judge that settles "
        "the debate uses the **deep** tier. Keys are read from your environment, the key "
        "store (Providers tab) or .env - never shown here. A tier without a key falls back "
        "to the deterministic, rule-based agents."
    )
    ready = {k: provider_ready(k, lookup) for k in LLM_PROVIDERS}
    st.markdown(
        " ".join(
            f"`{LLM_PROVIDERS[k].label}` {'✅' if ok else '⚪ no key'}" for k, ok in ready.items()
        )
    )
    keys = list(LLM_PROVIDERS)
    enabled = st.toggle("Use LLM agents (off = deterministic only)", value=cfg.llm_enabled)
    cols = st.columns(2)
    choices: dict[str, TierChoice] = {}
    for col, tier, current in zip(cols, ("quick", "deep"), (cfg.quick, cfg.deep), strict=True):
        with col.container(border=True):
            st.markdown(
                f"**{tier.capitalize()} tier** - " + ("analysts" if tier == "quick" else "judge")
            )
            prov = st.selectbox(
                "Provider",
                keys,
                index=keys.index(current.provider),
                format_func=lambda k: LLM_PROVIDERS[k].label,
                key=f"tier_{tier}_prov",
            )
            info = LLM_PROVIDERS[prov]
            default = info.quick_model if tier == "quick" else info.deep_model
            model = st.text_input(
                "Model (blank = default)",
                value=current.model if current.provider == prov else "",
                placeholder=default,
                key=f"tier_{tier}_model",
            )
            choices[tier] = TierChoice(prov, model.strip())
    rounds = st.slider("Bull vs Bear debate rounds", 1, 3, cfg.debate_rounds)
    if st.button("Save AI agent settings", type="primary"):
        save_config(
            DeliberationConfig(
                quick=choices["quick"],
                deep=choices["deep"],
                llm_enabled=enabled,
                debate_rounds=rounds,
            )
        )
        st.success("Saved. Used from the next research run.")
