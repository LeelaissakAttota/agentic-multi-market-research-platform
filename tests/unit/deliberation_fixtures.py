"""Shared fixtures for the debate / risk / rating tests (no network)."""

from __future__ import annotations

from typing import Any

from amrp.domain.llm import ModelCallStatus, ModelRequest, ModelResponse, ModelUsage

FINDINGS: dict[str, Any] = {
    "market_figures": [
        {"tag": "M1", "label": "Last close", "value": "USD 343.83", "detail": "as of 2026-10-28"},
        {"tag": "M3", "label": "Simple return", "value": "-0.94%", "detail": "as of 2026-10-28"},
        {
            "tag": "M4",
            "label": "SMA (simple moving average)",
            "value": "USD 344.95",
            "detail": "window 3; as of 2026-10-28",
        },
    ],
    "financial_figures": [
        {"tag": "F1", "label": "Revenue growth", "value": "6.43%", "detail": "FY2025"},
        {"tag": "F2", "label": "Net income growth", "value": "19.50%", "detail": "FY2025"},
        {"tag": "F5", "label": "Net margin", "value": "26.92%", "detail": "FY2025"},
        {"tag": "F6", "label": "Current ratio", "value": "0.89x", "detail": "FY2025"},
        {"tag": "F7", "label": "Debt to equity", "value": "1.23x", "detail": "FY2025"},
        {"tag": "F10", "label": "Free cash flow margin", "value": "23.73%", "detail": "FY2025"},
    ],
    "news_items": [
        {
            "tag": "N1",
            "date": "2026-10-29",
            "title": "Wall Street giants block tokenized stocks",
            "detail": "",
            "data_origin": "cached_live",
        },
        {
            "tag": "N3",
            "date": "2026-10-29",
            "title": "Analyst consensus: Buy - 11 strong buy, 20 buy, 14 hold (49 analysts)",
            "detail": "",
            "data_origin": "cached_live",
        },
    ],
    "regulatory_items": [],
}


class FakeRouter:
    """Returns queued replies in order; ``None`` entries simulate a failed call."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies.pop(0) if self.replies else None
        if reply is None:
            from amrp.domain.llm import ModelFailureKind

            return ModelResponse(
                request.call_id,
                ModelCallStatus.FAILED,
                None,
                None,
                ModelUsage(),
                0,
                ModelFailureKind.UPSTREAM_ERROR,
            )
        return ModelResponse(
            request.call_id, ModelCallStatus.SUCCEEDED, "fake/m", reply, ModelUsage(), 0
        )
