"""Dashboard preferences (live-data toggles) saved next to the control config.

The start script turns these into environment variables, so a change applies
after restarting the API (the backend reads settings once at startup).
"""

from __future__ import annotations

import json
from pathlib import Path

from financial_intelligence.control.secrets_store import default_config_dir

#: toggle name -> (env var, label shown on the Settings page)
LIVE_TOGGLES: dict[str, tuple[str, str]] = {
    "market": ("MARKET_DATA_LIVE_ENABLED", "Live market prices (Yahoo, NSE/BSE)"),
    "financials": ("FINANCIAL_DATA_LIVE_ENABLED", "Live financials (SEC filings, Yahoo India)"),
    "india_fundamentals": ("INDIA_FUNDAMENTALS_LIVE_ENABLED", "Live India fundamentals"),
    "news": ("LIVE_NEWS_ENABLED", "Live news (GDELT, RSS)"),
    "regulatory": ("LIVE_REGULATORY_ENABLED", "Live regulatory (SEC, SEBI)"),
    "llm": ("OPENROUTER_LIVE_ENABLED", "AI analyst summary (free OpenRouter models)"),
}
_TEXT_FIELDS = {"sec_user_agent": "SEC_USER_AGENT"}


def prefs_path() -> Path:
    return default_config_dir("equity-research") / "dashboard_settings.json"


def load_prefs(path: Path | None = None) -> dict[str, object]:
    path = path or prefs_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_prefs(values: dict[str, object], path: Path | None = None) -> None:
    path = path or prefs_path()
    clean: dict[str, object] = {k: bool(values.get(k)) for k in LIVE_TOGGLES}
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
    return env
