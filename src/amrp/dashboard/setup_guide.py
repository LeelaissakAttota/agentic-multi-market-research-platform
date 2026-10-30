"""Setup guide for new installs: what each key is for, required or not, where to get it.

Static text only; it never reads or displays any key value.
"""

from __future__ import annotations

GUIDE: list[dict[str, str]] = [
    {
        "Key / provider": "OpenRouter (OPENROUTER_API_KEY)",
        "Used for": "AI summaries and the Bull/Bear/Judge/Risk debate; auto-picks free models",
        "Needed?": "Recommended (free account works)",
        "Get it": "https://openrouter.ai/keys",
        "Connect & test": "Settings > Providers: paste key, Save; Model tab: pick OpenRouter, Test",
    },
    {
        "Key / provider": "OpenAI (OPENAI_API_KEY)",
        "Used for": "Optional paid LLM for the AI agents (quick/deep tiers)",
        "Needed?": "Optional",
        "Get it": "https://platform.openai.com/api-keys",
        "Connect & test": "Providers: paste key; AI agents tab: choose OpenAI for a tier",
    },
    {
        "Key / provider": "Anthropic Claude (ANTHROPIC_API_KEY)",
        "Used for": "Optional paid LLM for the AI agents",
        "Needed?": "Optional",
        "Get it": "https://console.anthropic.com/settings/keys",
        "Connect & test": "Providers: paste key; AI agents tab: choose Anthropic",
    },
    {
        "Key / provider": "Google Gemini (GEMINI_API_KEY)",
        "Used for": "Optional LLM (has a free tier) for the AI agents",
        "Needed?": "Optional",
        "Get it": "https://aistudio.google.com/apikey",
        "Connect & test": "Providers: paste key; AI agents tab: choose Gemini",
    },
    {
        "Key / provider": "Groq (gsk_ key)",
        "Used for": "Fast OpenAI-compatible LLM endpoint",
        "Needed?": "Optional",
        "Get it": "https://console.groq.com/keys",
        "Connect & test": "Providers: paste key (auto-detected), Save",
    },
    {
        "Key / provider": "Ollama (local, no key)",
        "Used for": "Run models on your own computer, fully offline and free",
        "Needed?": "Optional",
        "Get it": "https://ollama.com/download",
        "Connect & test": "Install, run `ollama pull llama3.1`; AI agents tab: choose Ollama",
    },
    {
        "Key / provider": "Finnhub (FINNHUB_API_KEY)",
        "Used for": "Extra company news and fundamentals",
        "Needed?": "Optional (free tier)",
        "Get it": "https://finnhub.io/register",
        "Connect & test": "Data keys tab: paste key, Save; check Provider Health",
    },
    {
        "Key / provider": "Alpha Vantage (ALPHA_VANTAGE_API_KEY)",
        "Used for": "Backup market prices when Yahoo Finance is unreachable",
        "Needed?": "Optional (free tier)",
        "Get it": "https://www.alphavantage.co/support/#api-key",
        "Connect & test": "Data keys tab: paste key, Save; check Provider Health",
    },
    {
        "Key / provider": "SEC EDGAR (SEC_USER_AGENT)",
        "Used for": "US filings for the Regulatory section; not a key, just 'Name email'",
        "Needed?": "Recommended for US stocks",
        "Get it": "https://www.sec.gov/os/accessing-edgar-data",
        "Connect & test": "Set in .env, restart; Regulatory section shows SEC filings",
    },
    {
        "Key / provider": "Yahoo Finance, Google News RSS, GDELT, SEBI",
        "Used for": "Live prices, news, filings",
        "Needed?": "No key needed",
        "Get it": "-",
        "Connect & test": "Live data tab: turn live data on; Provider Health shows green",
    },
]

STEPS = [
    "Copy `.env.example` to `.env` (or paste keys in Settings; they are stored masked).",
    "Add at least an OpenRouter key for AI summaries; everything else is optional.",
    "Open Settings > Live data and turn live data on.",
    "Open Provider Health: green = working, 'unused' = no key / not needed yet.",
    "Run research on Home (e.g. AAPL or RELIANCE) to confirm.",
]
