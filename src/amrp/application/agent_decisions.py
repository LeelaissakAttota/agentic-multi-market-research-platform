"""Labelled agent decisions recorded on a research workflow.

Each decision says which agent decided (``planner`` / ``replanner``), whether an
LLM or the deterministic rule made it (``source``), and why a fallback happened.
Stored on ``ResearchWorkflow.agent_decisions`` as compact JSON object strings.
"""

from __future__ import annotations

import json
from typing import Any

FALLBACK_PLANNER_SUFFIX = "+llm-fallback"
FALLBACK_MESSAGE_PREFIX = "deterministic planner used after llm planner failure: "
LLM_PLANNER_PREFIX = "llm-planner"
#: Description prefix marking tasks added by the bounded replan round.
REPLAN_DESCRIPTION_PREFIX = "Replan: "


def encode_decision(entry: dict[str, Any]) -> str:
    """Compact, bounded JSON for one decision."""

    text = json.dumps(entry, sort_keys=True, ensure_ascii=True, default=str)
    if len(text) > 4000:
        trimmed = {k: v for k, v in entry.items() if k in {"agent", "source", "fallback_reason"}}
        trimmed["truncated"] = True
        text = json.dumps(trimmed, sort_keys=True, ensure_ascii=True, default=str)
    return text


def planner_decision(planner_version: str, message: str) -> dict[str, Any]:
    """Describe who built a plan from its ``planner_version`` and outcome message."""

    if planner_version.endswith(FALLBACK_PLANNER_SUFFIX):
        reason = (
            message[len(FALLBACK_MESSAGE_PREFIX) :]
            if message.startswith(FALLBACK_MESSAGE_PREFIX)
            else "llm planner failed"
        )
        return {
            "agent": "planner",
            "source": "deterministic",
            "fallback_reason": reason,
            "planner_version": planner_version,
        }
    source = "llm" if planner_version.startswith(LLM_PLANNER_PREFIX) else "deterministic"
    return {"agent": "planner", "source": source, "planner_version": planner_version}
