"""Dashboard preferences (live-data toggles) saved next to the control config.

The start script turns these into environment variables, so a change applies
after restarting the API (the backend reads settings once at startup).
"""

from __future__ import annotations

import json
from pathlib import Path

from amrp.control.secrets_store import default_config_dir

#: toggle name -> (env var, label shown on the Settings page)
LIVE_TOGGLES: dict[str, tuple[str, str]] = {
    "market": ("MARKET_DATA_LIVE_ENABLED", "Live market prices (Yahoo, NSE/BSE)"),
    "financials": ("FINANCIAL_DATA_LIVE_ENABLED", "Live financials (SEC filings, Yahoo India)"),
    "india_fundamentals": ("INDIA_FUNDAMENTALS_LIVE_ENABLED", "Live India fundamentals"),
    "news": ("LIVE_NEWS_ENABLED", "Live news (GDELT, RSS)"),
    "regulatory": ("LIVE_REGULATORY_ENABLED", "Live regulatory (SEC, SEBI)"),
    "llm": ("OPENROUTER_LIVE_ENABLED", "AI analyst summary (OpenRouter, free first)"),
}
#: toggle name -> one-line plain description of the real data it brings
LIVE_DESCRIPTIONS: dict[str, str] = {
    "market": "Real share prices and 1-year price history from Yahoo Finance and NSE/BSE.",
    "financials": "Real revenue, profit and balance-sheet numbers from SEC filings "
    "(US) and Yahoo Finance (India).",
    "india_fundamentals": "Real Indian company fundamentals (P/E, margins, debt) "
    "from public Yahoo Finance India pages.",
    "news": "Real recent headlines about the company from GDELT and public RSS feeds.",
    "regulatory": "Real filings and regulatory actions from SEC EDGAR (US) and SEBI (India).",
    "llm": "Real AI-written analyst summary and research plan (OpenRouter; free models "
    "unless you pick a paid one).",
}
_TEXT_FIELDS = {
    "sec_user_agent": "SEC_USER_AGENT",
    "llm_model": "OPENROUTER_SELECTED_MODEL",
    "llm_base_url": "OPENROUTER_BASE_URL",
}
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def is_paid_model(model_id: str) -> bool:
    """Fail closed: anything without the ``:free`` suffix counts as paid."""

    model_id = model_id.strip()
    return bool(model_id) and not model_id.endswith(":free")


def prefs_path() -> Path:
    return default_config_dir("amrp") / "dashboard_settings.json"


def load_prefs(path: Path | None = None) -> dict[str, object]:
    path = path or prefs_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_prefs(values: dict[str, object], path: Path | None = None) -> None:
    """Replace the live toggles / SEC contact; model choice keys are preserved."""

    path = path or prefs_path()
    old = load_prefs(path)
    merged = {**old, **values}
    clean: dict[str, object] = {k: bool(merged.get(k)) for k in LIVE_TOGGLES}
    clean["paid_opt_in"] = bool(merged.get("paid_opt_in"))
    values = merged
    for key in _TEXT_FIELDS:
        text = str(values.get(key) or "").strip()[:256]
        if text:
            clean[key] = text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def env_from_prefs(values: dict[str, object]) -> dict[str, str]:
    """Environment for the API processes. Live sources that need SEC_USER_AGENT get it."""

    env = {var: "true" if values.get(k) else "false" for k, (var, _) in LIVE_TOGGLES.items()}
    for key, var in _TEXT_FIELDS.items():
        if values.get(key):
            env[var] = str(values[key])
    model = str(values.get("llm_model") or "")
    if is_paid_model(model):
        if values.get("paid_opt_in"):
            env["ALLOW_PAID_MODELS"] = "true"  # explicit opt-in recorded by the user
        else:
            env.pop("OPENROUTER_SELECTED_MODEL", None)  # fail closed: back to free auto
    return env


def uses_custom_router(values: dict[str, object]) -> bool:
    base = str(values.get("llm_base_url") or OPENROUTER_BASE).rstrip("/")
    return base != OPENROUTER_BASE


def save_model_choice(
    model: str, base_url: str, *, paid_opt_in: bool, path: Path | None = None
) -> dict[str, object]:
    """Persist the Settings > Model choice. Empty model = automatic free-model choice.

    A paid model is only stored with ``paid_opt_in=True`` (the user ticked the cost
    warning); otherwise ``ValueError`` is raised and nothing is written.
    """

    model, base_url = model.strip()[:200], (base_url.strip().rstrip("/") or OPENROUTER_BASE)
    if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ValueError("base URL must start with https://")
    if is_paid_model(model) and not paid_opt_in:
        raise ValueError("paid model needs explicit opt-in")
    prefs = load_prefs(path)
    prefs["llm_model"] = model
    prefs["llm_base_url"] = base_url[:512]
    prefs["paid_opt_in"] = bool(paid_opt_in and is_paid_model(model))
    save_prefs(prefs, path)
    return load_prefs(path)


def settings_update(values: dict[str, object]) -> dict[str, object]:
    """Settings field updates (non-secret) implied by saved prefs, for per-run settings."""

    env = env_from_prefs(values)
    update: dict[str, object] = {}
    for var, raw in env.items():
        field = var.lower()
        update[field] = raw == "true" if raw in ("true", "false") else raw
    if "openrouter_selected_model" not in update:
        update["openrouter_selected_model"] = ""
        update["allow_paid_models"] = False
    return update
