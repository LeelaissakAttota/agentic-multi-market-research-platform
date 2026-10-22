from __future__ import annotations

import json
from pathlib import Path

import pytest

from financial_intelligence.control.service import EXTRA_PROVIDER_KEYS, EquityResearchControl
from financial_intelligence.dashboard.client import _error_message, candidate_choices
from financial_intelligence.dashboard.intent import (
    EXAMPLE_PROMPTS,
    company_candidates,
    detect_objective,
)
from financial_intelligence.dashboard.prefs import env_from_prefs, load_prefs, save_prefs
from financial_intelligence.dashboard.providers import (
    EXTRA_KEY_NAMES,
    PROVIDERS,
    choose_model,
    connection_test_request,
    provider,
)
from financial_intelligence.dashboard.report_view import (
    build_report_view,
    origin_badge,
    provider_strip,
)


@pytest.mark.parametrize(
    ("text", "objective"),
    [
        ("How is Apple's stock price doing?", "market_analysis"),
        (
            "Is Reliance's growth holding up, and what are the risks?",
            "comprehensive_equity_research",
        ),
        ("What are the latest news and events for TCS?", "news_and_events"),
        ("Comprehensive research on Apple", "comprehensive_equity_research"),
        ("Infosys", "company_overview"),
        ("Who are Apple's competitors?", "industry_analysis"),
    ],
)
def test_detect_objective(text: str, objective: str) -> None:
    assert detect_objective(text)[0] == objective


def test_company_candidates_longest_first_and_no_stopwords() -> None:
    cands = company_candidates("Tata Motors latest news after the demerger")
    assert cands[0] == "Tata Motors"
    assert "Tata" in cands
    assert all(c.lower() not in {"the", "news", "latest"} for c in cands)
    assert company_candidates("Is Reliance's growth holding up?")[0] == "Reliance"


def test_example_prompts_cover_requested_chips() -> None:
    labels = {label for label, _ in EXAMPLE_PROMPTS}
    assert {"Apple", "Reliance", "TCS", "Tata Motors demerger"} <= labels


def test_choose_model_prefers_free_then_cheapest() -> None:
    models = [
        {
            "id": "paid/cheap",
            "context_length": 64000,
            "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
        },
        {
            "id": "paid/dear",
            "context_length": 64000,
            "pricing": {"prompt": "0.00001", "completion": "0.00002"},
        },
        {"id": "free/small", "context_length": 4000, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "free/big", "context_length": 128000, "pricing": {"prompt": "0", "completion": "0"}},
        {
            "id": "router/auto",
            "context_length": 200000,
            "pricing": {"prompt": "-1", "completion": "-1"},
        },
    ]
    models.append(
        {
            "id": "free/music",
            "context_length": 999999,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"output_modalities": ["text", "audio"]},
        }
    )
    free = choose_model(models, "report")
    assert free.model_id == "free/big" and free.free and "free" in free.reason
    no_free = [m for m in models if not str(m["id"]).startswith("free")]
    blocked = choose_model(no_free, "summary")
    assert blocked.model_id is None and "paid/cheap" in blocked.reason
    allowed = choose_model(no_free, "summary", allow_paid=True)
    assert allowed.model_id == "paid/cheap" and not allowed.free
    assert choose_model([], "summary").model_id is None


def test_test_request_never_puts_key_in_url_and_requires_https() -> None:
    for spec in PROVIDERS:
        url, headers = connection_test_request(spec, "sk-secret-1234", "https://example.test/v1")
        assert "sk-secret" not in url
        assert any("sk-secret-1234" in v for v in headers.values())
    with pytest.raises(ValueError):
        connection_test_request(provider("custom"), "k")
    with pytest.raises(ValueError):
        connection_test_request(provider("custom"), "k", "http://evil.example/v1")


def test_extra_provider_keys_accepted_by_key_store(tmp_path: Path) -> None:
    assert set(EXTRA_KEY_NAMES) == set(EXTRA_PROVIDER_KEYS)
    control = EquityResearchControl(config_dir=tmp_path)
    control.secrets.set("GROQ_API_KEY", "gsk_abcdefghijkl1234")
    masked = control.secrets.masked()["GROQ_API_KEY"]
    assert masked == {"configured": True, "masked": "****1234"}
    # extra keys are stored only; they never become research settings
    assert "groq" not in json.dumps(control.settings_with_keys().model_dump(), default=str)


def test_report_view_and_badges() -> None:
    report = {
        "findings": {
            "analyst_summary": {"text": "Apple: close [M1]", "generated_by": "deterministic"},
            "market": {"data_origin": "fixture", "status": "degraded", "as_of": "2026-08-07"},
            "market_figures": [{"tag": "M1", "label": "Last close", "value": "USD 199.00"}],
            "news": {"data_origin": "live", "status": "ok"},
            "news_items": [{"tag": "N1", "title": "t", "data_origin": "live"}],
            "question": "q?",
            "errors": [],
        }
    }
    view = build_report_view(json.dumps(report), "Apple")
    assert view.summary.startswith("Apple") and view.question == "q?"
    assert [s.title for s in view.sections] == ["Market", "Top news"]
    assert origin_badge("fixture")[0] == "SAMPLE DATA"
    assert origin_badge("cached_live")[0].startswith("LIVE")
    assert origin_badge(None)[0] == "UNKNOWN SOURCE"
    assert origin_badge(None, "unavailable")[0] == "NOT AVAILABLE"


def test_provider_strip_and_candidates_and_errors() -> None:
    strip = provider_strip({"providers": [{"provider": "nse", "state": "failing"}, {"x": 1}]})
    assert strip == [("nse", "failing", "🔴")]
    res = {
        "candidates": [
            {
                "company": {"display_name": "Tata Motors Passenger Vehicles Ltd"},
                "matched_listings": [
                    {"ticker": "TMPV", "exchange": "BSE", "is_primary": False},
                    {"ticker": "TMPV", "exchange": "NSE", "is_primary": True},
                ],
            }
        ]
    }
    assert candidate_choices(res)[0]["label"] == "Tata Motors Passenger Vehicles Ltd (NSE:TMPV)"
    assert _error_message('{"error": {"message": "bad"}}') == "bad"
    assert _error_message('{"detail": "nope"}') == "nope"


def test_prefs_roundtrip_and_env(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    assert load_prefs(path) == {}
    save_prefs({"market": True, "news": 1, "sec_user_agent": " Me me@example.com ", "x": 5}, path)
    prefs = load_prefs(path)
    assert prefs["market"] is True and "x" not in prefs
    env = env_from_prefs(prefs)
    assert env["MARKET_DATA_LIVE_ENABLED"] == "true"
    assert env["LIVE_REGULATORY_ENABLED"] == "false"
    assert env["SEC_USER_AGENT"] == "Me me@example.com"


def test_launch_applies_prefs_only_when_saved_and_never_overrides_env_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from financial_intelligence.control.secrets_store import SecretStore
    from financial_intelligence.dashboard import launch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env: dict[str, str] = {}
    assert launch.apply_saved_settings(env) == []
    cfg = tmp_path / "equity-research"
    save_prefs({"news": True}, cfg / "dashboard_settings.json")
    SecretStore(cfg / "secrets.json", ("OPENROUTER_API_KEY",)).set(
        "OPENROUTER_API_KEY", "sk-or-test-abcdefgh1234"
    )
    names = launch.apply_saved_settings(env)
    assert env["LIVE_NEWS_ENABLED"] == "true" and env["MARKET_DATA_LIVE_ENABLED"] == "false"
    assert "OPENROUTER_API_KEY" in names and env["OPENROUTER_API_KEY"].endswith("1234")
    env2 = {"OPENROUTER_API_KEY": "from-env"}
    launch.apply_saved_settings(env2)
    assert env2["OPENROUTER_API_KEY"] == "from-env"
