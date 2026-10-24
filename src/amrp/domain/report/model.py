"""Automated report workflow contracts — no document rendering in Prompt 2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from amrp.domain.workflow.ids import WorkflowId


class ReportStatus(StrEnum):
    REPORT_PENDING = "report_pending"
    REPORT_READY = "report_ready"
    REPORT_FAILED = "report_failed"


class ReportRequestId:
    __slots__ = ("_value",)

    def __init__(self, value: UUID) -> None:
        if value.version != 4:
            msg = "report_request_id must be a UUIDv4"
            raise ValueError(msg)
        self._value = value

    @classmethod
    def new(cls) -> ReportRequestId:
        return cls(uuid4())

    def as_text(self) -> str:
        return str(self._value)


@dataclass(frozen=True, slots=True)
class ReportArtifactMetadata:
    """Metadata placeholder for a future report artifact (no binary content)."""

    format: str = "deferred"
    title: str = "research_report"

    def __post_init__(self) -> None:
        fmt = self.format.strip().lower()
        if fmt not in {"deferred", "markdown", "docx", "json"}:
            msg = "unsupported report format"
            raise ValueError(msg)
        title = " ".join(self.title.strip().split())
        if not title or len(title) > 128:
            msg = "report title empty or exceeds bounds"
            raise ValueError(msg)
        object.__setattr__(self, "format", fmt)
        object.__setattr__(self, "title", title)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "title": self.title,
            "kind": "report_artifact_metadata",
        }


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """Request to generate a research report from a completed workflow.

    Prompt 2 establishes the contract only. Actual Word/PDF rendering is deferred.
    """

    request_id: ReportRequestId
    workflow_id: WorkflowId
    status: ReportStatus
    created_at: datetime
    updated_at: datetime
    artifact: ReportArtifactMetadata | None = None
    message: str = "report generation deferred"

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            msg = "report timestamps must be timezone-aware"
            raise ValueError(msg)
        text = " ".join(self.message.strip().split())
        if not text or len(text) > 512:
            msg = "report message empty or exceeds bounds"
            raise ValueError(msg)
        object.__setattr__(self, "message", text)

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id.as_text(),
            "workflow_id": self.workflow_id.as_text(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": self.updated_at.isoformat().replace("+00:00", "Z"),
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "message": self.message,
            "kind": "report_request",
        }


WORKFLOW_REPORT_FORMATS: dict[str, str] = {
    "markdown": "text/markdown; charset=utf-8",
    "json": "application/json",
}
MAX_WORKFLOW_REPORT_CHARS = 1_000_000


@dataclass(frozen=True, slots=True)
class StoredWorkflowReport:
    """A rendered, stored workflow report (deterministic content, no LLM)."""

    report_id: ReportRequestId
    workflow_id: WorkflowId
    report_format: str
    title: str
    content: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.report_format not in WORKFLOW_REPORT_FORMATS:
            msg = "unsupported workflow report format"
            raise ValueError(msg)
        if self.created_at.tzinfo is None:
            msg = "report created_at must be timezone-aware"
            raise ValueError(msg)
        if not self.content or len(self.content) > MAX_WORKFLOW_REPORT_CHARS:
            msg = "report content empty or exceeds bounds"
            raise ValueError(msg)
        title = " ".join(self.title.strip().split())
        if not title or len(title) > 128:
            msg = "report title empty or exceeds bounds"
            raise ValueError(msg)
        object.__setattr__(self, "title", title)

    @property
    def media_type(self) -> str:
        return WORKFLOW_REPORT_FORMATS[self.report_format]

    def to_dict(self, *, include_content: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "report_id": self.report_id.as_text(),
            "workflow_id": self.workflow_id.as_text(),
            "format": self.report_format,
            "media_type": self.media_type,
            "title": self.title,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "content_length": len(self.content),
            "kind": "stored_workflow_report",
        }
        if include_content:
            data["content"] = self.content
        return data
