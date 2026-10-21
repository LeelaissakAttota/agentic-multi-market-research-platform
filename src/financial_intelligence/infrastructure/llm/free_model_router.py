"""Automatic OpenRouter free-model selection with a 6h disk cache.

Mirrors the deep-research assistant's selector:

* ``GET /api/v1/models`` -> keep only ``:free`` text-capable models;
* probe the top ``probe_count`` candidates with a tiny prompt and order the
  working ones fastest-first;
* persist the ranking to a small JSON file (never the API key) and reuse it
  until ``ttl_seconds`` (default 6h) expires, then re-discover automatically;
* at call time try models in ranked order, demoting ones that fail, up to
  ``max_attempts`` per request.

Only ``:free`` model ids are ever used (paid models stay prohibited).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from threading import Lock

from pydantic import SecretStr

from financial_intelligence.application.ports import LlmRouterPort
from financial_intelligence.domain.llm import (
    ModelCallStatus,
    ModelFailureKind,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpTransportError
from financial_intelligence.observability.logging import get_logger

logger = get_logger("financial_intelligence.infrastructure.llm.free_model_router")

AUTO_MODEL = "auto"
_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_CACHE_VERSION = 1
_SPECIALTY = ("content-safety", "guard", "note-preview", "embed")
_NO_FALLBACK = frozenset(
    {
        ModelFailureKind.CONTEXT_EXCEEDED,
        ModelFailureKind.CONTENT_POLICY,
        ModelFailureKind.POLICY_VIOLATION,
    }
)


def _is_free(model_id: str) -> bool:
    return model_id.endswith(":free")


class FreeModelCatalog:
    """Ranked, probed, disk-cached list of OpenRouter ``:free`` text models."""

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        api_key: SecretStr,
        cache_path: Path | None,
        ttl_seconds: float = 21_600.0,
        probe_count: int = 3,
        probe_budget_seconds: float = 12.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._cache_path = cache_path
        self._ttl = ttl_seconds
        self._probe_count = max(0, probe_count)
        self._probe_budget = max(0.1, probe_budget_seconds)
        self._wall = wall_clock
        self._mono = monotonic
        self._lock = Lock()
        self._ranked: list[str] = []
        self._loaded_at: float | None = None
        self._demoted: set[str] = set()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    def ranked(self) -> list[str]:
        with self._lock:
            now = self._wall()
            expired = self._loaded_at is None or now - self._loaded_at >= self._ttl
            if expired and not self._load_disk(now):
                self._refresh(now)
            demoted = self._demoted
            return sorted(self._ranked, key=lambda mid: mid in demoted)

    def demote(self, model_id: str) -> None:
        with self._lock:
            self._demoted.add(model_id)

    # -- internals --------------------------------------------------------
    def _load_disk(self, now: float) -> bool:
        if self._cache_path is None:
            return False
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            return False
        saved_at = data.get("saved_at")
        ranked = data.get("ranked")
        if not isinstance(saved_at, (int, float)) or now - float(saved_at) >= self._ttl:
            return False
        if not isinstance(ranked, list) or not ranked:
            return False
        self._ranked = [m for m in ranked if isinstance(m, str) and _is_free(m)]
        self._loaded_at = float(saved_at)
        return bool(self._ranked)

    def _refresh(self, now: float) -> None:
        try:
            payload = self._http.get_json(_MODELS_URL, extra_headers=self._headers())
        except HttpTransportError as exc:
            logger.warning("openrouter_models_unavailable", extra={"failure_kind": exc.kind.value})
            self._loaded_at = now - self._ttl + 300  # retry discovery in 5 minutes
            return
        rows = payload.get("data")
        candidates: list[tuple[int, int, str]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not _is_free(model_id):
                continue
            arch = row.get("architecture")
            inputs = arch.get("input_modalities") if isinstance(arch, dict) else None
            if isinstance(inputs, list) and inputs and "text" not in inputs:
                continue
            specialty = 1 if any(token in model_id for token in _SPECIALTY) else 0
            try:
                context = int(str(row.get("context_length") or 0))
            except (TypeError, ValueError):
                context = 0
            candidates.append((specialty, -context, model_id))
        ordered = [mid for _, _, mid in sorted(candidates)]
        latencies: dict[str, float] = {}
        failed: set[str] = set()
        to_probe = ordered[: self._probe_count]
        if to_probe:
            logger.info("openrouter_free_model_selection_started", extra={"probing": len(to_probe)})
            # Probe concurrently and time-box the whole step so one slow model
            # cannot stall workflow creation; unfinished probes rank after
            # working models but before failed ones.
            pool = ThreadPoolExecutor(max_workers=len(to_probe), thread_name_prefix="or-probe")
            futures = {pool.submit(self._probe, mid): mid for mid in to_probe}
            done, _pending = wait(futures, timeout=self._probe_budget)
            pool.shutdown(wait=False, cancel_futures=True)
            for future in done:
                model_id = futures[future]
                latency = future.result()
                if latency is None:
                    failed.add(model_id)
                else:
                    latencies[model_id] = latency
        working = sorted(latencies, key=lambda mid: latencies[mid])
        rest = [mid for mid in ordered if mid not in latencies and mid not in failed]
        self._ranked = [*working, *rest, *[m for m in ordered if m in failed]]
        self._loaded_at = now
        logger.info(
            "openrouter_free_models_selected",
            extra={"selected": self._ranked[0] if self._ranked else None, "count": len(ordered)},
        )
        self._save(now, latencies)

    def _probe(self, model_id: str) -> float | None:
        start = self._mono()
        try:
            body = self._http.post_json(
                _CHAT_URL,
                {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 16,
                },
                extra_headers=self._headers(),
            )
        except HttpTransportError:
            return None
        if "error" in body or not body.get("choices"):
            return None
        return (self._mono() - start) * 1000

    def _save(self, now: float, latencies: dict[str, float]) -> None:
        if self._cache_path is None:
            return
        payload = {
            "version": _CACHE_VERSION,
            "saved_at": now,
            "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "ttl_seconds": self._ttl,
            "ranked": self._ranked,
            "probe_latency_ms": {k: round(v, 1) for k, v in latencies.items()},
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except OSError:
            logger.info("openrouter_free_model_cache_not_written")


_ATTEMPT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="or-call")


class AutoFreeModelRouter:
    """LlmRouterPort that walks the ranked free models with bounded fallback."""

    provider_name = "openrouter"

    def __init__(
        self,
        catalog: FreeModelCatalog,
        adapter_factory: Callable[[str], LlmRouterPort],
        *,
        pinned: tuple[str, ...] = (),
        max_attempts: int = 3,
        deadline_seconds: float = 45.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deadline = max(1.0, deadline_seconds)
        self._mono = monotonic
        self._catalog = catalog
        self._factory = adapter_factory
        self._pinned = tuple(m for m in pinned if m and m != AUTO_MODEL and _is_free(m))
        self._max_attempts = max(1, max_attempts)

    def _attempt(self, model_id: str, request: ModelRequest, timeout: float) -> ModelResponse:
        """One model call with a hard wall-clock cap.

        Socket timeouts do not bound a response that trickles in slowly (seen live:
        a single free-model call took ~4 minutes), so the call runs on a worker
        thread and is abandoned after ``timeout`` seconds.
        """

        future = _ATTEMPT_POOL.submit(self._factory(model_id).complete, request)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            logger.info("openrouter_free_model_attempt_timeout", extra={"model": model_id})
            return ModelResponse(
                call_id=request.call_id,
                status=ModelCallStatus.FAILED,
                model_used=None,
                content=None,
                usage=ModelUsage(),
                retry_count=0,
                failure_kind=ModelFailureKind.TIMEOUT,
            )

    def candidates(self) -> list[str]:
        ranked = self._catalog.ranked()
        return list(dict.fromkeys([*self._pinned, *ranked]))

    def complete(self, request: ModelRequest) -> ModelResponse:
        last: ModelResponse | None = None
        started = self._mono()
        for model_id in self.candidates()[: self._max_attempts]:
            if last is not None and self._mono() - started >= self._deadline:
                # Bounded latency: stop walking slow/rate-limited models; callers
                # (e.g. the planner) fall back deterministically.
                logger.info("openrouter_free_model_deadline_reached", extra={"model": model_id})
                break
            remaining = max(1.0, self._deadline - (self._mono() - started))
            response = self._attempt(model_id, request, remaining)
            if response.status is ModelCallStatus.SUCCEEDED:
                return response
            last = response
            if response.failure_kind in _NO_FALLBACK:
                return response
            self._catalog.demote(model_id)
            logger.info(
                "openrouter_free_model_fallback",
                extra={
                    "model": model_id,
                    "failure_kind": response.failure_kind.value if response.failure_kind else None,
                },
            )
        if last is not None:
            return last
        return ModelResponse(
            call_id=request.call_id,
            status=ModelCallStatus.FAILED,
            model_used=None,
            content=None,
            usage=ModelUsage(),
            retry_count=0,
            failure_kind=ModelFailureKind.UPSTREAM_ERROR,
        )
