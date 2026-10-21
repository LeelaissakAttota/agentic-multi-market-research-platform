from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from financial_intelligence.domain.llm import (
    ModelCallStatus,
    ModelFailureKind,
    ModelResponse,
    ModelUsage,
)
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpResponse
from financial_intelligence.infrastructure.llm.free_model_router import (
    AutoFreeModelRouter,
    FreeModelCatalog,
)
from tests.unit.test_market_live import FakeTransport

SECRET = "sk-or-should-never-leak"
MODELS = {
    "data": [
        {"id": "paid/model", "context_length": 999999},
        {"id": "a/slow:free", "context_length": 200000},
        {"id": "b/fast:free", "context_length": 100000},
        {"id": "c/dead:free", "context_length": 300000},
        {"id": "d/vision-only:free", "architecture": {"input_modalities": ["image"]}},
        {"id": "e/llama-guard:free", "context_length": 900000},
    ]
}


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _catalog(tmp: Path, wall: _Clock, calls: list[str]) -> FreeModelCatalog:
    mono = {"t": 0.0}

    def handler(method: str, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        assert headers["Authorization"] == f"Bearer {SECRET}"
        calls.append(url)
        if url.endswith("/models"):
            return HttpResponse(200, json.dumps(MODELS).encode(), "application/json", {})
        return HttpResponse(200, b"{}", "application/json", {})

    class _Transport(FakeTransport):
        def request(  # type: ignore[override]
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            timeout: float,
            body: bytes | None = None,
        ) -> HttpResponse:
            if body is not None:
                model = json.loads(body)["model"]
                calls.append(model)
                mono["t"] += {"a/slow:free": 2.0, "b/fast:free": 0.3}.get(model, 0.1)
                if model == "c/dead:free":
                    return HttpResponse(404, b"{}", "application/json", {})
                content = {"choices": [{"message": {"content": "ok"}}]}
                return HttpResponse(200, json.dumps(content).encode(), "application/json", {})
            return handler(method, url, headers, timeout)

    http = BoundedHttpClient(_Transport(handler), timeout_seconds=1, max_retries=0, user_agent="t")
    return FreeModelCatalog(
        http,
        api_key=SecretStr(SECRET),
        cache_path=tmp / "free.json",
        ttl_seconds=21_600,
        probe_count=3,
        wall_clock=wall,
        monotonic=lambda: mono["t"],
    )


def test_discovers_probes_and_ranks_fastest_free_first(tmp_path: Path) -> None:
    calls: list[str] = []
    ranked = _catalog(tmp_path, _Clock(), calls).ranked()
    assert ranked[0] == "b/fast:free"
    assert "paid/model" not in ranked
    assert "d/vision-only:free" not in ranked
    assert ranked[-1] == "c/dead:free"  # probed-failing last
    assert ranked.index("e/llama-guard:free") > ranked.index("a/slow:free")
    saved = (tmp_path / "free.json").read_text()
    assert SECRET not in saved


def test_disk_cache_reused_within_6h_and_refreshed_after(tmp_path: Path) -> None:
    wall = _Clock()
    _catalog(tmp_path, wall, []).ranked()
    calls: list[str] = []
    wall.t += 3600
    _catalog(tmp_path, wall, calls).ranked()
    assert calls == []  # served from disk
    wall.t += 6 * 3600
    _catalog(tmp_path, wall, calls).ranked()
    assert any(c.endswith("/models") for c in calls)


def _resp(ok: bool, model: str, kind: ModelFailureKind | None = None) -> ModelResponse:
    from financial_intelligence.domain.llm import ModelCallId

    return ModelResponse(
        call_id=ModelCallId.new(),
        status=ModelCallStatus.SUCCEEDED if ok else ModelCallStatus.FAILED,
        model_used=model if ok else None,
        content="hi" if ok else None,
        usage=ModelUsage(),
        retry_count=0,
        failure_kind=None if ok else kind,
    )


class _Fake:
    def __init__(self, model: str, outcomes: dict[str, Any], used: list[str]) -> None:
        self.model = model
        self.outcomes = outcomes
        self.used = used

    def complete(self, request: Any) -> ModelResponse:
        self.used.append(self.model)
        return self.outcomes[self.model]


def test_router_falls_back_and_demotes(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, _Clock(), [])
    used: list[str] = []
    outcomes = {
        "b/fast:free": _resp(False, "b/fast:free", ModelFailureKind.RATE_LIMITED),
        "a/slow:free": _resp(True, "a/slow:free"),
    }
    router = AutoFreeModelRouter(catalog, lambda m: _Fake(m, outcomes, used))
    result = router.complete(object())  # type: ignore[arg-type]
    assert result.status is ModelCallStatus.SUCCEEDED
    assert used == ["b/fast:free", "a/slow:free"]
    assert catalog.ranked()[-1] == "b/fast:free"  # demoted


def test_router_does_not_fallback_on_context_exceeded(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, _Clock(), [])
    used: list[str] = []
    outcomes = {"b/fast:free": _resp(False, "x", ModelFailureKind.CONTEXT_EXCEEDED)}
    router = AutoFreeModelRouter(catalog, lambda m: _Fake(m, outcomes, used))
    assert router.complete(object()).failure_kind is ModelFailureKind.CONTEXT_EXCEEDED  # type: ignore[arg-type]
    assert used == ["b/fast:free"]


def test_pinned_free_models_first_paid_ignored(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, _Clock(), [])
    router = AutoFreeModelRouter(
        catalog, lambda m: _Fake(m, {}, []), pinned=("z/pin:free", "paid/x")
    )
    assert router.candidates()[0] == "z/pin:free"
    assert "paid/x" not in router.candidates()


def test_composition_auto_wires_router(tmp_path: Path) -> None:
    from financial_intelligence.composition import build_container
    from financial_intelligence.config.settings import Settings

    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        OPENROUTER_LIVE_ENABLED=True,
        PRIMARY_FREE_MODEL="auto",
        OPENROUTER_API_KEY=SECRET,
        OPENROUTER_FREE_MODEL_CACHE_PATH=str(tmp_path / "c.json"),
    )
    container = build_container(settings)
    assert isinstance(container.llm_router, AutoFreeModelRouter)
