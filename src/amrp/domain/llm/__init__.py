"""LLM Foundation domain package for model calls and structured output."""

from amrp.domain.llm.errors import ModelFailureKind
from amrp.domain.llm.ids import ModelCallId, ToolCallId
from amrp.domain.llm.model import (
    ModelCallStatus,
    ModelMessage,
    ModelMessageRole,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from amrp.domain.llm.structured_output import (
    StructuredModelOutput,
    StructuredOutputKind,
    ToolCallPayload,
)

__all__ = [
    "ModelCallId",
    "ModelCallStatus",
    "ModelFailureKind",
    "ModelMessage",
    "ModelMessageRole",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "StructuredModelOutput",
    "StructuredOutputKind",
    "ToolCallId",
    "ToolCallPayload",
]
