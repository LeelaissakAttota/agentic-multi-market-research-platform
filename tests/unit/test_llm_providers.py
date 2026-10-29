"""Multi-provider LLM adapters: payload shapes, parsing, failures, tier config (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from amrp.domain.llm import (
    ModelCallId,
    ModelCallStatus,
    ModelFailureKind,
    ModelMessage,
    ModelRequest,
)
from amrp.infrastructure.http import HttpFailureKind, HttpTransportError
from amrp.infrastructure.llm.providers import (
    LLM_PROVIDERS,
    AnthropicAdapter,
    DeliberationConfig,
    GeminiAdapter,
    OpenAICompatibleAdapter,
    TierChoice,
    build_router,
    build_tier_routers,
    env_lookup,
    load_config,
    save_config,
)


class FakeHttp:
    def __init__(self, body: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.body, self.error = body or {}, error
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def post_json(self, url: str, payload: dict[str, Any], *, extra_headers: dict[str, str]) -> Any:
        self.calls.append((url, payload, extra_headers))
        if self.error:
            raise self.error
        return self.body


def _req() -> ModelRequest:
    return ModelRequest(
        call_id=ModelCallId.new(),
        prompt_version="t1",
        messages=(ModelMessage("system", "be brief"), ModelMessage("user", "hello")),
        max_output_tokens=50,
    )


def test_openai_compatible_payload_and_parse() -> None:
    http = FakeHttp({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3}})
    a = OpenAICompatibleAdapter(
        http,
        provider_name="openai",
        model="gpt-4o-mini",
        base_url="https://x/v1/",  # type: ignore[arg-type]
        api_key=SecretStr("sk-test"),
    )
    r = a.complete(_req())
    assert r.status is ModelCallStatus.SUCCEEDED and r.content == "hi"
    url, payload, headers = http.calls[0]
    assert url == "https://x/v1/chat/completions"
    assert payload["messages"][0]["role"] == "system"
    assert headers["Authorization"] == "Bearer sk-test"
    assert r.model_used == "openai/gpt-4o-mini"


def test_ollama_sends_no_auth_header() -> None:
    http = FakeHttp({"choices": [{"message": {"content": "ok"}}]})
    a = OpenAICompatibleAdapter(
        http,
        provider_name="ollama",
        model="llama3.1",
        base_url="http://l/v1",
        api_key=None,  # type: ignore[arg-type]
    )
    assert a.complete(_req()).content == "ok"
    assert http.calls[0][2] == {}


def test_anthropic_system_is_top_level() -> None:
    http = FakeHttp({"content": [{"type": "text", "text": "yo"}], "usage": {"input_tokens": 4}})
    a = AnthropicAdapter(http, model="claude", base_url="https://a/v1", api_key=SecretStr("k"))  # type: ignore[arg-type]
    r = a.complete(_req())
    url, payload, headers = http.calls[0]
    assert url.endswith("/messages") and payload["system"] == "be brief"
    assert all(m["role"] != "system" for m in payload["messages"])
    assert headers["x-api-key"] == "k" and r.content == "yo" and r.usage.input_tokens == 4


def test_gemini_key_in_header_not_url() -> None:
    http = FakeHttp({"candidates": [{"content": {"parts": [{"text": "g"}]}}]})
    a = GeminiAdapter(http, model="gemini-2.0-flash", base_url="https://g", api_key=SecretStr("gk"))  # type: ignore[arg-type]
    r = a.complete(_req())
    url, payload, headers = http.calls[0]
    assert "gk" not in url and headers["x-goog-api-key"] == "gk"
    assert payload["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert r.content == "g"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (HttpFailureKind.UNAUTHORIZED, ModelFailureKind.AUTHENTICATION_ERROR),
        (HttpFailureKind.RATE_LIMITED, ModelFailureKind.RATE_LIMITED),
        (HttpFailureKind.TIMEOUT, ModelFailureKind.TIMEOUT),
    ],
)
def test_transport_failures_are_typed(kind: HttpFailureKind, expected: ModelFailureKind) -> None:
    http = FakeHttp(error=HttpTransportError(kind, "boom", status_code=401))
    a = AnthropicAdapter(http, model="c", base_url="https://a", api_key=SecretStr("secret-key"))  # type: ignore[arg-type]
    r = a.complete(_req())
    assert r.status is ModelCallStatus.FAILED and r.failure_kind is expected


def test_malformed_body_is_failure() -> None:
    a = GeminiAdapter(
        FakeHttp({"nope": 1}), model="m", base_url="https://g", api_key=SecretStr("k")
    )  # type: ignore[arg-type]
    assert a.complete(_req()).failure_kind is ModelFailureKind.MALFORMED_OUTPUT


def test_missing_key_resolves_to_none_and_ollama_needs_none() -> None:
    lookup = env_lookup({}, dotenv_path=None)
    assert build_router(TierChoice("openai"), "quick", lookup=lookup) is None
    assert build_router(TierChoice("ollama"), "quick", lookup=lookup) is not None


def test_tier_routers_use_tier_default_models() -> None:
    lookup = env_lookup({"OPENAI_API_KEY": "sk-x"}, dotenv_path=None)
    cfg = DeliberationConfig(quick=TierChoice("openai"), deep=TierChoice("anthropic"))
    routers = build_tier_routers(cfg, lookup=lookup, http=FakeHttp())  # type: ignore[arg-type]
    assert routers.quick is not None and routers.deep is None
    assert routers.quick_label == "openai/gpt-4o-mini"
    assert "key missing" in routers.deep_label


def test_llm_disabled_gives_deterministic() -> None:
    cfg = DeliberationConfig(llm_enabled=False)
    routers = build_tier_routers(cfg, lookup=env_lookup({"OPENROUTER_API_KEY": "x"}, None))
    assert not routers.any_llm


def test_config_roundtrip_and_validation(tmp_path: Path) -> None:
    cfg = DeliberationConfig(quick=TierChoice("gemini", "gemini-2.0-flash"), debate_rounds=2)
    save_config(cfg, tmp_path / "c.json")
    assert load_config(tmp_path / "c.json") == cfg
    assert load_config(tmp_path / "missing.json") == DeliberationConfig()
    with pytest.raises(ValueError):
        DeliberationConfig(quick=TierChoice("nope"))
    assert set(LLM_PROVIDERS) == {"openrouter", "openai", "anthropic", "gemini", "ollama"}


def test_dotenv_lookup(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("# c\nOPENAI_API_KEY='abc'\n", encoding="utf-8")
    assert env_lookup({}, tmp_path / ".env")("OPENAI_API_KEY") == "abc"
