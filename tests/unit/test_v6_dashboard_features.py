"""v6: paid-model opt-in, model persistence, Finnhub adapter, commodity helpers (offline)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from pydantic import SecretStr, ValidationError

from amrp.composition import build_container
from amrp.config.settings import Settings
from amrp.control.service import EquityResearchControl
from amrp.dashboard import commodity as cm
from amrp.dashboard import market_live as ml
from amrp.dashboard.prefs import (
    env_from_prefs,
    is_paid_model,
    load_prefs,
    save_model_choice,
    save_prefs,
    settings_update,
    uses_custom_router,
)
from amrp.dashboard.views import (
    agent_states,
    analyst_rows,
    model_options,
    pending_steps,
)
from amrp.domain.llm import ModelCallStatus
from amrp.infrastructure.http import BoundedHttpClient, HttpResponse
from amrp.infrastructure.llm.openrouter_adapter import OpenRouterAdapter
from amrp.infrastructure.news.finnhub import (
    RECOMMENDATION_PROVIDER,
    FinnhubSource,
    parse_next_earnings,
    parse_recommendation,
    us_ticker,
)
from tests.unit.live_fakes import (
    Clock,
    FakeTransport,
    company,
    http_client,
    json_response,
    no_wait_limiter,
)

_ENV = {
    "APP_ENV": "development",
    "ALLOW_PAID_MODELS": "false",
    "OPENROUTER_API_KEY": "",
    "PRIMARY_FREE_MODEL": "",
    "FALLBACK_FREE_MODEL_1": "",
    "FALLBACK_FREE_MODEL_2": "",
    "FINNHUB_API_KEY": "",
}


def _settings(**env: str) -> Settings:
    with mock.patch.dict(os.environ, {**_ENV, **env}, clear=True):
        return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------- paid-model opt-in policy
def test_paid_flag_alone_still_fails_closed() -> None:
    with pytest.raises(ValidationError):
        _settings(ALLOW_PAID_MODELS="true")


def test_paid_selected_model_requires_opt_in() -> None:
    with pytest.raises(ValidationError):
        _settings(OPENROUTER_SELECTED_MODEL="anthropic/claude-opus-5")
    s = _settings(OPENROUTER_SELECTED_MODEL="anthropic/claude-opus-5", ALLOW_PAID_MODELS="true")
    assert s.allow_paid_models and s.selected_model_is_paid()


def test_free_selected_model_needs_no_opt_in_and_enables_live_without_primary() -> None:
    s = _settings(
        OPENROUTER_SELECTED_MODEL="meta-llama/llama-4:free", OPENROUTER_LIVE_ENABLED="true"
    )
    assert not s.allow_paid_models and not s.selected_model_is_paid()


def test_base_url_must_be_https() -> None:
    with pytest.raises(ValidationError):
        _settings(OPENROUTER_BASE_URL="http://evil.example/v1")
    assert _settings(OPENROUTER_BASE_URL="https://r.example/v1/").openrouter_base_url == (
        "https://r.example/v1"
    )


# ---------------------------------------------------------------- prefs persistence
def test_save_model_choice_requires_opt_in_for_paid(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    with pytest.raises(ValueError):
        save_model_choice("openai/gpt-5", "", paid_opt_in=False, path=path)
    assert not path.exists()
    saved = save_model_choice("openai/gpt-5", "", paid_opt_in=True, path=path)
    assert saved["llm_model"] == "openai/gpt-5" and saved["paid_opt_in"] is True
    env = env_from_prefs(saved)
    assert env["OPENROUTER_SELECTED_MODEL"] == "openai/gpt-5"
    assert env["ALLOW_PAID_MODELS"] == "true"


def test_model_choice_survives_live_toggle_save_and_back_to_auto(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    save_model_choice("x/y:free", "https://r.example/v1", paid_opt_in=False, path=path)
    save_prefs({"market": True, "sec_user_agent": "Jane Doe j@x.com"}, path)
    prefs = load_prefs(path)
    assert prefs["llm_model"] == "x/y:free" and prefs["market"] is True
    assert uses_custom_router(prefs)
    assert "ALLOW_PAID_MODELS" not in env_from_prefs(prefs)
    save_model_choice("", "", paid_opt_in=True, path=path)
    prefs = load_prefs(path)
    assert prefs["paid_opt_in"] is False and not uses_custom_router(prefs)
    update = settings_update(prefs)
    assert update["openrouter_selected_model"] == "" and update["allow_paid_models"] is False


def test_tampered_prefs_paid_without_opt_in_fall_back_to_free() -> None:
    env = env_from_prefs({"llm_model": "openai/gpt-5", "paid_opt_in": False})
    assert "OPENROUTER_SELECTED_MODEL" not in env and "ALLOW_PAID_MODELS" not in env
    assert is_paid_model("openai/gpt-5") and not is_paid_model("a/b:free")


def test_control_runs_use_saved_model_and_custom_key(tmp_path: Path) -> None:
    base = _settings()
    control = EquityResearchControl(base_settings=base, config_dir=tmp_path)
    save_model_choice(
        "vendor/big-model",
        "https://router.example/v1",
        paid_opt_in=True,
        path=tmp_path / "dashboard_settings.json",
    )
    control.secrets.set("CUSTOM_LLM_API_KEY", "custom-secret-key-123")
    s = control.settings_with_keys()
    assert s.openrouter_selected_model == "vendor/big-model"
    assert s.allow_paid_models is True
    assert s.openrouter_base_url == "https://router.example/v1"
    assert s.openrouter_api_key.get_secret_value() == "custom-secret-key-123"


def test_container_uses_selected_model_and_base_url() -> None:
    s = _settings(
        OPENROUTER_LIVE_ENABLED="true",
        OPENROUTER_API_KEY="k" * 20,
        OPENROUTER_SELECTED_MODEL="vendor/big-model",
        ALLOW_PAID_MODELS="true",
        OPENROUTER_BASE_URL="https://router.example/v1",
    )
    router = build_container(s).llm_router
    assert isinstance(router, OpenRouterAdapter)
    assert router._model == "vendor/big-model"
    assert router._chat_url == "https://router.example/v1/chat/completions"
    assert router._allow_paid is True


class _PostTransport:
    def __init__(self, cost: float = 0.0) -> None:
        self.cost = cost
        self.urls: list[str] = []
        self.bodies: list[dict[str, object]] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], timeout: float, body: bytes | None
    ) -> HttpResponse:
        self.urls.append(url)
        self.bodies.append(json.loads(body or b"{}"))
        payload = {
            "model": "vendor/big-model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": self.cost},
        }
        return HttpResponse(200, json.dumps(payload).encode(), "application/json", {})


def test_adapter_posts_to_custom_base_url() -> None:
    from amrp.domain.llm import ModelCallId, ModelMessage, ModelRequest

    t = _PostTransport()
    http = BoundedHttpClient(t, timeout_seconds=2.0, max_retries=0, user_agent="t/1")
    adapter = OpenRouterAdapter(
        http,
        api_key=SecretStr("k"),
        model="vendor/big-model",
        max_output_tokens_ceiling=64,
        base_url="https://router.example/v1/",
    )
    req = ModelRequest(
        call_id=ModelCallId.new(),
        prompt_version="synthesis-v1",
        messages=(ModelMessage(role="user", content="hi"),),
        max_output_tokens=8,
    )
    resp = adapter.complete(req)
    assert t.urls == ["https://router.example/v1/chat/completions"]
    assert t.bodies[0]["model"] == "vendor/big-model"
    assert resp.status is ModelCallStatus.SUCCEEDED


# ---------------------------------------------------------------- Finnhub (fixtures only)
REC = [
    {"period": "2026-09-01", "strongBuy": 1, "buy": 1, "hold": 9, "sell": 0, "strongSell": 0},
    {"period": "2026-10-01", "strongBuy": 12, "buy": 20, "hold": 8, "sell": 1, "strongSell": 0},
]
EARN = {"earningsCalendar": [{"date": "2026-10-30", "epsEstimate": 1.71, "symbol": "AAPL"}]}
NEWS = [
    {
        "id": 1,
        "headline": "Apple unveils new chips",
        "url": "https://www.reuters.com/apple-chips",
        "source": "Reuters",
        "datetime": 1760400000 + 365 * 86400,
        "summary": "Apple announced...",
    },
    {"id": 2, "headline": "", "url": "https://x.example"},
    {"id": 3, "headline": "Bad link", "url": "javascript:alert(1)"},
]


def test_parse_recommendation_and_earnings() -> None:
    rec = parse_recommendation(REC)
    assert rec is not None and rec.period == "2026-10-01" and rec.consensus == "Buy"
    assert rec.total == 41 and "12 strong buy" in rec.headline()
    assert parse_recommendation([]) is None
    assert parse_next_earnings(EARN, "2026-10-14") == EARN["earningsCalendar"][0]
    assert parse_next_earnings(EARN, "2026-11-01") is None


def test_finnhub_source_emits_news_recommendation_and_earnings() -> None:
    def handler(url: str, headers: dict[str, str]) -> HttpResponse:
        assert headers.get("X-Finnhub-Token") == "fh-secret"
        assert "fh-secret" not in url
        if "/stock/recommendation" in url:
            return json_response(REC)
        if "/calendar/earnings" in url:
            return json_response(EARN)
        return json_response(NEWS)

    t = FakeTransport(handler)
    src = FinnhubSource(
        http_client(t),
        api_key=SecretStr("fh-secret"),
        rate_limiter=no_wait_limiter(),
        cache_ttl_seconds=60,
        lookback_days=7,
        clock=Clock(),
    )
    apple = company("AAPL")
    assert us_ticker(apple) == "AAPL"
    events = src.fetch(apple)
    titles = [e.title for e in events]
    assert titles[0].startswith("Analyst consensus: Buy")
    assert any(t.startswith("Next earnings report scheduled 2026-10-30") for t in titles)
    assert "Apple unveils new chips" in titles and len(events) == 3
    assert events[0].evidence.provider_name == RECOMMENDATION_PROVIDER
    assert all("fh-secret" not in e.evidence.locator for e in events)
    assert src.fetch(company("RELIANCE")) == ()


def test_finnhub_failure_returns_empty() -> None:
    t = FakeTransport(lambda u, h: json_response({"error": "x"}, status=500))
    src = FinnhubSource(
        http_client(t),
        api_key=SecretStr("k"),
        rate_limiter=no_wait_limiter(),
        cache_ttl_seconds=60,
        lookback_days=7,
        clock=Clock(),
    )
    assert src.fetch(company("AAPL")) == ()


def test_finnhub_wired_only_with_key() -> None:
    from amrp.composition import _live_news_sources

    assert not any(isinstance(s, FinnhubSource) for s in _live_news_sources(_settings(), None))
    with_key = _settings(FINNHUB_API_KEY="fh-secret")
    assert any(isinstance(s, FinnhubSource) for s in _live_news_sources(with_key, None))


# ---------------------------------------------------------------- commodity + views
def _daily(sym: str, closes: list[tuple[str, float]]) -> ml.Daily:
    bars = tuple(ml.Bar(d, c, c, c, c, 0) for d, c in closes)
    return ml.Daily(sym, sym, "USD", bars, "2026-10-23T10:00+00:00")


def test_to_inr_uses_same_day_fx_and_mcx_unit() -> None:
    gold = _daily("GC=F", [("2026-10-01", 3110.34768), ("2026-10-02", 3110.34768)])
    fx = _daily("USDINR=X", [("2026-10-01", 80.0), ("2026-10-02", 90.0)])
    inr = cm.to_inr(gold, fx, cm.COMMODITIES["Gold"].to_mcx_unit)
    assert inr is not None and inr.currency == "INR"
    assert round(inr.bars[0].close, 2) == 80000.0  # 100 USD per 10 g x 80
    assert round(inr.bars[1].close, 2) == 90000.0
    late_fx = _daily("USDINR=X", [("2026-10-05", 80.0)])
    assert cm.to_inr(gold, late_fx, 1.0) is None
    assert round(cm.COMMODITIES["Copper"].to_mcx_unit, 3) == 2.205


def test_parse_google_rss_strips_publisher() -> None:
    xml = (
        "<rss><channel><item><title>Gold hits high - Mint</title>"
        "<link>https://news.google.com/a</link><source>Mint</source>"
        "<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>"
        "<item><title>x</title><link>ftp://bad</link></item></channel></rss>"
    )
    items = cm.parse_google_rss(xml)
    assert len(items) == 1 and items[0].title == "Gold hits high" and items[0].source == "Mint"
    assert items[0].published.startswith("2026-10-23T10:00")
    assert cm.parse_google_rss("not xml") == []
    assert cm.news_queries(cm.COMMODITIES["Gold"], True)[0] == "MCX gold"


def test_model_options_free_first_and_fail_closed() -> None:
    payload = {
        "data": [
            {"id": "z/paid", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
            {"id": "a/free:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "b/zero-priced", "pricing": {"prompt": "0", "completion": "0"}},
        ]
    }
    opts = model_options(payload)
    assert [o["id"] for o in opts] == ["a/free:free", "b/zero-priced", "z/paid"]
    assert opts[0]["free"] and not opts[1]["free"]  # only ':free' counts as free
    assert "$2.00 in / $10.00 out" in opts[2]["label"]
    assert model_options(None) == []


def test_agent_states_and_analyst_rows() -> None:
    steps = [
        {"kind": "market_intelligence", "state": "succeeded"},
        {"kind": "news_event_intelligence", "state": "pending_approval"},
    ]
    states = agent_states(steps)
    assert states["market_intelligence"] == "succeeded"
    assert states["regulatory_intelligence"] == "queued"
    assert set(agent_states([]).values()) == {"waiting"}
    assert len(pending_steps(steps)) == 1
    rows = [{"title": "Analyst consensus: Buy - ..."}, {"title": "Apple news"}]
    assert analyst_rows(rows) == [rows[0]]


def test_paid_cost_rejected_unless_adapter_opted_in() -> None:
    from amrp.domain.llm import ModelCallId, ModelMessage, ModelRequest

    def run(allow_paid: bool) -> ModelCallStatus:
        http = BoundedHttpClient(
            _PostTransport(cost=0.01), timeout_seconds=2.0, max_retries=0, user_agent="t/1"
        )
        adapter = OpenRouterAdapter(
            http,
            api_key=SecretStr("k"),
            model="vendor/big-model",
            max_output_tokens_ceiling=64,
            allow_paid=allow_paid,
        )
        req = ModelRequest(
            call_id=ModelCallId.new(),
            prompt_version="synthesis-v1",
            messages=(ModelMessage(role="user", content="hi"),),
            max_output_tokens=8,
        )
        return adapter.complete(req).status

    assert run(False) is ModelCallStatus.FAILED
    assert run(True) is ModelCallStatus.SUCCEEDED
    free = build_container(
        _settings(
            OPENROUTER_LIVE_ENABLED="true",
            OPENROUTER_API_KEY="k" * 20,
            OPENROUTER_SELECTED_MODEL="a/b:free",
        )
    ).llm_router
    assert isinstance(free, OpenRouterAdapter) and free._allow_paid is False
