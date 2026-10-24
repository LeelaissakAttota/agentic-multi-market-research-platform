from __future__ import annotations

from decimal import Decimal

from amrp.domain.llm import (
    ModelCallId,
    ModelCallStatus,
    ModelFailureKind,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from amrp.infrastructure.llm.free_model_router import AutoFreeModelRouter


class _Catalog:
    def ranked(self) -> list[str]:
        return ["a/x:free", "b/x:free", "c/x:free"]

    def demote(self, model_id: str) -> None:
        pass


def test_router_stops_trying_models_after_deadline() -> None:
    now = [0.0]
    used: list[str] = []

    class _Slow:
        def __init__(self, model: str) -> None:
            self.model = model

        def complete(self, request: ModelRequest) -> ModelResponse:
            used.append(self.model)
            now[0] += 30.0  # each attempt burns 30 s
            return ModelResponse(
                call_id=request.call_id,
                status=ModelCallStatus.FAILED,
                model_used=None,
                content=None,
                usage=ModelUsage(estimated_cost=Decimal("0")),
                retry_count=0,
                failure_kind=ModelFailureKind.TIMEOUT,
            )

    router = AutoFreeModelRouter(
        _Catalog(),  # type: ignore[arg-type]
        _Slow,
        deadline_seconds=45.0,
        monotonic=lambda: now[0],
    )
    request = ModelRequest(
        call_id=ModelCallId.new(),
        prompt_version="t",
        messages=(ModelMessage(role="user", content="hi"),),
        max_output_tokens=8,
    )
    response = router.complete(request)
    assert response.status is ModelCallStatus.FAILED
    assert used == ["a/x:free", "b/x:free"]  # third skipped: 60 s >= 45 s budget


def test_hung_model_call_is_abandoned_at_deadline() -> None:
    import threading
    import time

    release = threading.Event()

    class _Hang:
        def __init__(self, model: str) -> None:
            pass

        def complete(self, request: ModelRequest) -> ModelResponse:
            release.wait(10)
            raise AssertionError("unreachable in test")

    router = AutoFreeModelRouter(_Catalog(), _Hang, deadline_seconds=1.0)  # type: ignore[arg-type]
    request = ModelRequest(
        call_id=ModelCallId.new(),
        prompt_version="t",
        messages=(ModelMessage(role="user", content="hi"),),
        max_output_tokens=8,
    )
    start = time.monotonic()
    response = router.complete(request)
    release.set()
    assert time.monotonic() - start < 3.0
    assert response.failure_kind is ModelFailureKind.TIMEOUT
