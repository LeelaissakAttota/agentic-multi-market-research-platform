"""LLM provider catalog, connection-test requests and low-cost model selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    key_name: str  # name in the control key store (write-only, masked)
    key_hint: str
    base_url: str
    signup_url: str
    needs_base_url: bool = False
    used_by_backend: bool = False


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "openrouter",
        "OpenRouter (recommended, free models)",
        "OPENROUTER_API_KEY",
        "starts with sk-or-",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/keys",
        used_by_backend=True,
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "OPENAI_API_KEY",
        "starts with sk-",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
    ),
    ProviderSpec(
        "anthropic",
        "Anthropic",
        "ANTHROPIC_API_KEY",
        "starts with sk-ant-",
        "https://api.anthropic.com/v1",
        "https://console.anthropic.com/settings/keys",
    ),
    ProviderSpec(
        "gemini",
        "Google Gemini",
        "GEMINI_API_KEY",
        "from Google AI Studio",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://aistudio.google.com/apikey",
    ),
    ProviderSpec(
        "groq",
        "Groq",
        "GROQ_API_KEY",
        "starts with gsk_",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
    ),
    ProviderSpec(
        "custom",
        "Custom (OpenAI-compatible)",
        "CUSTOM_LLM_API_KEY",
        "any key your server expects",
        "",
        "",
        needs_base_url=True,
    ),
)

#: Extra key names the control key store accepts (stored only; see README).
EXTRA_KEY_NAMES: tuple[str, ...] = tuple(p.key_name for p in PROVIDERS[1:])


def provider(key: str) -> ProviderSpec:
    for spec in PROVIDERS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def connection_test_request(
    spec: ProviderSpec, api_key: str, base_url: str = ""
) -> tuple[str, dict[str, str]]:
    """(url, headers) for a cheap read-only 'list models' call that proves the key works."""

    base = (base_url or spec.base_url).rstrip("/")
    if not base:
        raise ValueError("base URL is required for this provider")
    if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
        raise ValueError("base URL must use https (or http://127.0.0.1 for a local server)")
    if spec.key == "openrouter":
        return f"{base}/key", {"Authorization": f"Bearer {api_key}"}
    if spec.key == "anthropic":
        return f"{base}/models", {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if spec.key == "gemini":
        return f"{base}/models", {"x-goog-api-key": api_key}
    return f"{base}/models", {"Authorization": f"Bearer {api_key}"}


# ---- model selection -------------------------------------------------------

#: Minimum context window per task (tokens). Reports cite many evidence rows.
TASK_MIN_CONTEXT = {"summary": 8_000, "planning": 16_000, "report": 32_000}


@dataclass(frozen=True)
class ModelChoice:
    model_id: str | None
    price_per_million: float | None
    free: bool
    reason: str


def _price(model: dict[str, Any]) -> float | None:
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt", "nan"))
        completion = float(pricing.get("completion", "nan"))
    except (TypeError, ValueError):
        return None
    if prompt != prompt or completion != completion or prompt < 0 or completion < 0:
        return None  # NaN / negative (e.g. "-1" router placeholders) -> unknown price
    return (prompt + completion) * 1_000_000


def _text_only(model: dict[str, Any]) -> bool:
    """Chat models only: text output (skip image/audio/music generators)."""

    arch = model.get("architecture")
    if not isinstance(arch, dict):
        return True
    outputs = arch.get("output_modalities")
    if isinstance(outputs, list):
        return outputs == ["text"]
    return str(arch.get("modality", "->text")).endswith("->text")


def choose_model(
    models: list[dict[str, Any]], task: str = "summary", *, allow_paid: bool = False
) -> ModelChoice:
    """Free first; otherwise (only if allowed) the cheapest model with enough context."""

    need = TASK_MIN_CONTEXT.get(task, 8_000)
    usable = [
        m
        for m in models
        if isinstance(m, dict)
        and m.get("id")
        and _text_only(m)
        and int(m.get("context_length") or 0) >= need
        and _price(m) is not None
    ]
    free = sorted(
        (m for m in usable if _price(m) == 0.0),
        key=lambda m: (-int(m.get("context_length") or 0), str(m["id"])),
    )
    if free:
        best = free[0]
        return ModelChoice(
            str(best["id"]),
            0.0,
            True,
            f"free model with the largest context ({best.get('context_length')} tokens) "
            f"for task '{task}' (needs ≥{need}); {len(free)} free models available",
        )
    paid = sorted((m for m in usable if (_price(m) or 0) > 0), key=lambda m: _price(m) or 0)
    if not paid:
        return ModelChoice(None, None, False, f"no model has ≥{need} context for '{task}'")
    if not allow_paid:
        return ModelChoice(
            None,
            None,
            False,
            "no free model available and paid models are off (ALLOW_PAID_MODELS=false); "
            f"cheapest would be {paid[0]['id']}",
        )
    best = paid[0]
    price = _price(best) or 0.0
    return ModelChoice(
        str(best["id"]),
        price,
        False,
        f"no free model; cheapest suitable is ${price:.2f} per million tokens (in+out)",
    )
