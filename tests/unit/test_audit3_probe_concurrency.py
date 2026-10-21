from __future__ import annotations

import threading
import time
from typing import Any

from pydantic import SecretStr

from financial_intelligence.infrastructure.llm.free_model_router import FreeModelCatalog

MODELS = ["a/fast:free", "b/hang:free", "c/broken:free"]


class _Http:
    def __init__(self) -> None:
        self.release = threading.Event()

    def get_json(self, url: str, **_: Any) -> dict[str, Any]:
        return {"data": [{"id": m, "context_length": 1000} for m in MODELS]}

    def post_json(self, url: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        model = body["model"]
        if model.startswith("a/"):
            time.sleep(0.2)
            return {"choices": [{"message": {"content": "ok"}}]}
        if model.startswith("b/"):
            self.release.wait(5)  # simulates a hung free model
            return {"choices": [{"message": {"content": "ok"}}]}
        return {"error": {"code": 429}}


def test_probes_run_concurrently_and_are_time_boxed() -> None:
    http = _Http()
    catalog = FreeModelCatalog(
        http,  # type: ignore[arg-type]
        api_key=SecretStr("k"),
        cache_path=None,
        probe_budget_seconds=1.0,
    )
    start = time.monotonic()
    ranked = catalog.ranked()
    elapsed = time.monotonic() - start
    http.release.set()
    assert elapsed < 2.0  # sequential would be 0.2 + 5 s
    assert ranked == ["a/fast:free", "b/hang:free", "c/broken:free"]
