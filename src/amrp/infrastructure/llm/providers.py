"""Multi-provider LLM adapters behind the existing ``LlmRouterPort``.

Providers: OpenRouter (existing free-model router), OpenAI, Anthropic, Google Gemini
and a local Ollama server. Two model *tiers* are configured independently:

* ``quick`` - cheap/fast model used by the analyst-style agents (bull, bear, risk views);
* ``deep``  - stronger model used by the judge that settles the debate.

API keys are read only from the process environment, the local dashboard key store
or the repo ``.env`` file; they are never logged, returned or written by this module.
Network I/O goes through ``BoundedHttpClient`` so tests inject a fake transport.
When a provider has no key the tier resolves to ``None`` and callers fall back to the
deterministic (LLM-off) path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import SecretStr

from amrp.application.ports import LlmRouterPort
from amrp.domain.llm import (
    ModelCallStatus,
    ModelFailureKind,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from amrp.infrastructure.http import (
    BoundedHttpClient,
    HttpFailureKind,
    HttpTransportError,
    UrlLibHttpTransport,
)
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.llm.providers")

TIERS = ("quick", "deep")


@dataclass(frozen=True, slots=True)
class LlmProviderInfo:
    key: str
    label: str
    key_env: str | None
    base_url_env: str
    default_base_url: str
    quick_model: str
    deep_model: str
    style: str  # "openai" | "anthropic" | "gemini"


LLM_PROVIDERS: dict[str, LlmProviderInfo] = {
    p.key: p
    for p in (
        LlmProviderInfo(
            "openrouter",
            "OpenRouter (automatic free models)",
            "OPENROUTER_API_KEY",
            "OPENROUTER_BASE_URL",
            "https://openrouter.ai/api/v1",
            "auto",
            "auto",
            "openai",
        ),
        LlmProviderInfo(
            "openai",
            "OpenAI",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
            "gpt-4o-mini",
            "gpt-4o",
            "openai",
        ),
        LlmProviderInfo(
            "anthropic",
            "Anthropic Claude",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "https://api.anthropic.com/v1",
            "claude-3-5-haiku-latest",
            "claude-sonnet-4-5",
            "anthropic",
        ),
        LlmProviderInfo(
            "gemini",
            "Google Gemini",
            "GEMINI_API_KEY",
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-2.0-flash",
            "gemini-2.5-pro",
            "gemini",
        ),
        LlmProviderInfo(
            "ollama",
            "Ollama (local, no key)",
            None,
            "OLLAMA_BASE_URL",
            "http://localhost:11434/v1",
            "llama3.1",
            "llama3.1",
            "openai",
        ),
    )
}


# ----------------------------------------------------------------- tier configuration
@dataclass(frozen=True, slots=True)
class TierChoice:
    provider: str
    model: str = ""  # blank -> provider default for the tier

    def resolved_model(self, tier: str) -> str:
        info = LLM_PROVIDERS[self.provider]
        return self.model.strip() or (info.quick_model if tier == "quick" else info.deep_model)


@dataclass(frozen=True, slots=True)
class DeliberationConfig:
    """Saved LLM-tier and debate settings (no secrets in here)."""

    quick: TierChoice = TierChoice("openrouter")
    deep: TierChoice = TierChoice("openrouter")
    llm_enabled: bool = True
    debate_rounds: int = 1

    def __post_init__(self) -> None:
        for choice in (self.quick, self.deep):
            if choice.provider not in LLM_PROVIDERS:
                msg = f"unknown LLM provider: {choice.provider}"
                raise ValueError(msg)
        if not 1 <= self.debate_rounds <= 3:
            msg = "debate_rounds must be between 1 and 3"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> DeliberationConfig:
        def tier(name: str) -> TierChoice:
            value = raw.get(name)
            if isinstance(value, Mapping):
                return TierChoice(
                    str(value.get("provider") or "openrouter"), str(value.get("model") or "")
                )
            return TierChoice("openrouter")

        rounds = raw.get("debate_rounds", 1)
        return cls(
            quick=tier("quick"),
            deep=tier("deep"),
            llm_enabled=bool(raw.get("llm_enabled", True)),
            debate_rounds=max(1, min(3, int(rounds) if isinstance(rounds, int | str) else 1)),
        )


def default_config_path() -> Path:
    from amrp.control.secrets_store import default_config_dir

    return default_config_dir("amrp") / "llm_tiers.json"


def load_config(path: Path | None = None) -> DeliberationConfig:
    try:
        raw = json.loads((path or default_config_path()).read_text(encoding="utf-8"))
        return DeliberationConfig.from_dict(raw if isinstance(raw, dict) else {})
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return DeliberationConfig()


def save_config(config: DeliberationConfig, path: Path | None = None) -> None:
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")


# ----------------------------------------------------------------- key lookup
def _dotenv(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def env_lookup(
    environ: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
) -> Callable[[str], str]:
    """name -> value from the environment, then the dashboard key store, then ``.env``."""

    env = os.environ if environ is None else environ
    cache: dict[str, str] | None = None

    def get(name: str) -> str:
        nonlocal cache
        if env.get(name):
            return str(env[name])
        if environ is None:  # only consult local stores for the real process env
            try:
                from amrp.control.secrets_store import SecretStore, default_config_dir

                store = SecretStore(default_config_dir("amrp") / "secrets.json", (name,))
                value = store.get(name)
                if value:
                    return value
            except Exception:  # key store is optional
                pass
        if dotenv_path is None:
            return ""
        if cache is None:
            cache = _dotenv(dotenv_path)
        return cache.get(name, "")

    return get


def provider_ready(provider: str, lookup: Callable[[str], str]) -> bool:
    info = LLM_PROVIDERS[provider]
    return info.key_env is None or bool(lookup(info.key_env))


# ----------------------------------------------------------------- adapters
def _failed(request: ModelRequest, kind: ModelFailureKind) -> ModelResponse:
    return ModelResponse(
        call_id=request.call_id,
        status=ModelCallStatus.FAILED,
        model_used=None,
        content=None,
        usage=ModelUsage(),
        retry_count=0,
        failure_kind=kind,
    )


def _map_transport(exc: HttpTransportError) -> ModelFailureKind:
    return {
        HttpFailureKind.TIMEOUT: ModelFailureKind.TIMEOUT,
        HttpFailureKind.NETWORK_ERROR: ModelFailureKind.NETWORK_ERROR,
        HttpFailureKind.UNAUTHORIZED: ModelFailureKind.AUTHENTICATION_ERROR,
        HttpFailureKind.RATE_LIMITED: ModelFailureKind.RATE_LIMITED,
        HttpFailureKind.UPSTREAM_ERROR: ModelFailureKind.UPSTREAM_ERROR,
        HttpFailureKind.OVERSIZED: ModelFailureKind.UPSTREAM_ERROR,
        HttpFailureKind.NOT_FOUND: ModelFailureKind.INVALID_REQUEST,
        HttpFailureKind.INVALID_RESPONSE: ModelFailureKind.INVALID_REQUEST,
    }.get(exc.kind, ModelFailureKind.UNKNOWN)


def _usage(raw: object, in_key: str, out_key: str) -> ModelUsage:
    if not isinstance(raw, dict):
        return ModelUsage()
    i, o = raw.get(in_key), raw.get(out_key)
    return ModelUsage(
        input_tokens=i if isinstance(i, int) and i >= 0 else None,
        output_tokens=o if isinstance(o, int) and o >= 0 else None,
    )


class _ChatAdapter:
    """Shared request/response plumbing; subclasses build payloads and parse bodies."""

    provider_name = "base"

    def __init__(self, http: BoundedHttpClient, *, model: str, base_url: str) -> None:
        if not model.strip():
            msg = "model is required"
            raise ValueError(msg)
        self._http = http
        self._model = model.strip()
        self._base = base_url.rstrip("/")

    @property
    def model(self) -> str:
        return self._model

    def _call(
        self, request: ModelRequest, url: str, payload: dict[str, object], headers: dict[str, str]
    ) -> ModelResponse:
        try:
            body = self._http.post_json(url, payload, extra_headers=headers)
        except HttpTransportError as exc:
            kind = _map_transport(exc)
            logger.info(
                "llm_provider_call_failed",
                extra={
                    "provider": self.provider_name,
                    "model": self._model,
                    "failure_kind": kind.value,
                    "status_code": exc.status_code,
                },
            )
            return _failed(request, kind)
        try:
            content, usage = self._parse(body)
        except (TypeError, ValueError, KeyError, IndexError):
            return _failed(request, ModelFailureKind.MALFORMED_OUTPUT)
        if not content.strip():
            return _failed(request, ModelFailureKind.MALFORMED_OUTPUT)
        return ModelResponse(
            call_id=request.call_id,
            status=ModelCallStatus.SUCCEEDED,
            model_used=f"{self.provider_name}/{self._model}"[:128],
            content=content,
            usage=usage,
            retry_count=0,
        )

    def _parse(self, body: dict[str, object]) -> tuple[str, ModelUsage]:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatibleAdapter(_ChatAdapter):
    """OpenAI chat-completions protocol (OpenAI, Ollama's /v1, OpenRouter explicit model)."""

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        provider_name: str,
        model: str,
        base_url: str,
        api_key: SecretStr | None,
    ) -> None:
        super().__init__(http, model=model, base_url=base_url)
        self.provider_name = provider_name
        self._key = api_key

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"} if self._key else {}
        return self._call(request, f"{self._base}/chat/completions", payload, headers)

    def _parse(self, body: dict[str, object]) -> tuple[str, ModelUsage]:
        choices = body["choices"]
        assert isinstance(choices, list)
        message = choices[0]["message"]
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError("content")
        return content, _usage(body.get("usage"), "prompt_tokens", "completion_tokens")


class AnthropicAdapter(_ChatAdapter):
    """Anthropic Messages API (system prompt is a top-level field)."""

    provider_name = "anthropic"

    def __init__(
        self, http: BoundedHttpClient, *, model: str, base_url: str, api_key: SecretStr
    ) -> None:
        super().__init__(http, model=model, base_url=base_url)
        self._key = api_key

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role != "system"
            ],
        }
        if system:
            payload["system"] = system
        headers = {"x-api-key": self._key.get_secret_value(), "anthropic-version": "2023-06-01"}
        return self._call(request, f"{self._base}/messages", payload, headers)

    def _parse(self, body: dict[str, object]) -> tuple[str, ModelUsage]:
        blocks = body["content"]
        assert isinstance(blocks, list)
        text = "".join(
            str(b.get("text", ""))
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return text, _usage(body.get("usage"), "input_tokens", "output_tokens")


class GeminiAdapter(_ChatAdapter):
    """Google Gemini ``generateContent`` API (key sent as a header, never in the URL)."""

    provider_name = "gemini"

    def __init__(
        self, http: BoundedHttpClient, *, model: str, base_url: str, api_key: SecretStr
    ) -> None:
        super().__init__(http, model=model, base_url=base_url)
        self._key = api_key

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        payload: dict[str, object] = {
            "contents": [
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
                for m in request.messages
                if m.role != "system"
            ],
            "generationConfig": {"maxOutputTokens": request.max_output_tokens, "temperature": 0.2},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        headers = {"x-goog-api-key": self._key.get_secret_value()}
        url = f"{self._base}/models/{self._model}:generateContent"
        return self._call(request, url, payload, headers)

    def _parse(self, body: dict[str, object]) -> tuple[str, ModelUsage]:
        candidates = body["candidates"]
        assert isinstance(candidates, list)
        parts = candidates[0]["content"]["parts"]
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        return text, _usage(body.get("usageMetadata"), "promptTokenCount", "candidatesTokenCount")


# ----------------------------------------------------------------- factory
def default_http(timeout_seconds: float = 60.0) -> BoundedHttpClient:
    return BoundedHttpClient(
        UrlLibHttpTransport(max_response_bytes=2 * 1024 * 1024),
        timeout_seconds=timeout_seconds,
        max_retries=1,
        user_agent="AMRP/1.0 (agentic-multi-market-research-platform)",
    )


def build_router(
    choice: TierChoice,
    tier: str,
    *,
    lookup: Callable[[str], str] | None = None,
    http: BoundedHttpClient | None = None,
) -> LlmRouterPort | None:
    """Return an ``LlmRouterPort`` for the tier, or ``None`` when its key is missing."""

    get = lookup or env_lookup()
    info = LLM_PROVIDERS[choice.provider]
    if not provider_ready(choice.provider, get):
        return None
    client = http or default_http()
    model = choice.resolved_model(tier)
    base = get(info.base_url_env) or info.default_base_url
    key = SecretStr(get(info.key_env)) if info.key_env else None
    if info.key == "openrouter":
        assert key is not None
        if model == "auto":
            return _openrouter_auto(client, key)
        from amrp.infrastructure.llm.openrouter_adapter import OpenRouterAdapter

        # An explicitly chosen non-":free" model is the user's own paid opt-in.
        return OpenRouterAdapter(
            client,
            api_key=key,
            model=model,
            max_output_tokens_ceiling=4096,
            base_url=base,
            allow_paid=not model.endswith(":free"),
        )
    if info.style == "anthropic":
        assert key is not None
        return AnthropicAdapter(client, model=model, base_url=base, api_key=key)
    if info.style == "gemini":
        assert key is not None
        return GeminiAdapter(client, model=model, base_url=base, api_key=key)
    return OpenAICompatibleAdapter(
        client, provider_name=info.key, model=model, base_url=base, api_key=key
    )


def _openrouter_auto(client: BoundedHttpClient, key: SecretStr) -> LlmRouterPort:
    from amrp.control.secrets_store import default_config_dir
    from amrp.infrastructure.llm.free_model_router import AutoFreeModelRouter, FreeModelCatalog
    from amrp.infrastructure.llm.openrouter_adapter import OpenRouterAdapter

    probe = BoundedHttpClient(
        UrlLibHttpTransport(max_response_bytes=16 * 1024 * 1024),
        timeout_seconds=10.0,
        max_retries=0,
        user_agent="AMRP/1.0 (agentic-multi-market-research-platform)",
    )
    catalog = FreeModelCatalog(
        probe, api_key=key, cache_path=default_config_dir("amrp") / "openrouter_free_models.json"
    )

    def factory(model_id: str) -> LlmRouterPort:
        return OpenRouterAdapter(
            client, api_key=key, model=model_id, max_output_tokens_ceiling=4096
        )

    return AutoFreeModelRouter(catalog, factory)


@dataclass(frozen=True, slots=True)
class TierRouters:
    quick: LlmRouterPort | None
    deep: LlmRouterPort | None
    quick_label: str
    deep_label: str

    @property
    def any_llm(self) -> bool:
        return self.quick is not None or self.deep is not None


def build_tier_routers(
    config: DeliberationConfig,
    *,
    lookup: Callable[[str], str] | None = None,
    http: BoundedHttpClient | None = None,
) -> TierRouters:
    """Routers for both tiers; ``None`` entries mean deterministic fallback for that tier."""

    if not config.llm_enabled:
        return TierRouters(None, None, "deterministic (LLM off)", "deterministic (LLM off)")
    get = lookup or env_lookup()
    quick = build_router(config.quick, "quick", lookup=get, http=http)
    deep = build_router(config.deep, "deep", lookup=get, http=http)

    def label(router: LlmRouterPort | None, choice: TierChoice, tier: str) -> str:
        if router is None:
            return f"deterministic ({choice.provider} key missing)"
        return f"{choice.provider}/{choice.resolved_model(tier)}"

    return TierRouters(
        quick, deep, label(quick, config.quick, "quick"), label(deep, config.deep, "deep")
    )
