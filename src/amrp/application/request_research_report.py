"""Report workflow use case: contract-only request or deterministic rendering + storage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from amrp.application.ports import (
    ResearchMemoryPort,
    ResearchWorkflowStorePort,
    WorkflowReportStorePort,
)
from amrp.application.report_findings import (
    ReportFindings,
    ReportFindingsCollector,
    company_query_from_workflow,
)
from amrp.application.workflow_report_rendering import (
    render_workflow_json,
    render_workflow_markdown,
)
from amrp.domain.report import (
    WORKFLOW_REPORT_FORMATS,
    ReportArtifactMetadata,
    ReportRequest,
    ReportRequestId,
    ReportStatus,
    StoredWorkflowReport,
)
from amrp.domain.workflow import WorkflowId, WorkflowStatus, is_terminal


class ReportOperationStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ReportOperationResult:
    status: ReportOperationStatus
    message: str
    report: ReportRequest | None = None
    evaluated_at: datetime | None = None
    stored_report: StoredWorkflowReport | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "message": self.message,
            "report": self.report.to_dict() if self.report else None,
            "evaluated_at": (
                self.evaluated_at.isoformat().replace("+00:00", "Z") if self.evaluated_at else None
            ),
            "stored_report": self.stored_report.to_dict() if self.stored_report else None,
            "kind": "report_operation_result",
        }


class RequestResearchReport:
    """Establish report-request state for a completed/partial workflow.

    Prompt 2 intentionally leaves rendering deferred (ADR).
    """

    def __init__(
        self,
        workflow_store: ResearchWorkflowStorePort,
        *,
        clock: Callable[[], datetime] | None = None,
        research_memory: ResearchMemoryPort | None = None,
        report_store: WorkflowReportStorePort | None = None,
        findings_collector: ReportFindingsCollector | None = None,
    ) -> None:
        self._findings = findings_collector
        self._store = workflow_store
        self._memory = research_memory
        self._report_store = report_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._requests: dict[str, ReportRequest] = {}

    def execute(
        self, workflow_id: WorkflowId, *, report_format: str | None = None
    ) -> ReportOperationResult:
        """Record a report request; render and store it when ``report_format`` is given.

        ``report_format=None`` keeps the original contract-only behaviour
        (``report_pending``). ``"markdown"`` or ``"json"`` renders the
        workflow deterministically and stores it (``report_ready``).
        """
        now = self._clock()
        if report_format is not None and report_format not in WORKFLOW_REPORT_FORMATS:
            return ReportOperationResult(
                status=ReportOperationStatus.INVALID,
                message="unsupported report format; use markdown or json",
                evaluated_at=now,
            )
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return ReportOperationResult(
                status=ReportOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if workflow.status not in {WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL}:
            return ReportOperationResult(
                status=ReportOperationStatus.CONFLICT,
                message=(
                    "report request requires completed or partial workflow; "
                    f"status={workflow.status.value}"
                ),
                evaluated_at=now,
            )
        if not is_terminal(workflow.status):
            return ReportOperationResult(
                status=ReportOperationStatus.CONFLICT,
                message="workflow not terminal",
                evaluated_at=now,
            )
        if report_format is not None:
            return self._render(workflow_id, report_format, now)
        report = ReportRequest(
            request_id=ReportRequestId.new(),
            workflow_id=workflow_id,
            status=ReportStatus.REPORT_PENDING,
            created_at=now,
            updated_at=now,
            artifact=ReportArtifactMetadata(format="deferred", title="research_report"),
            message=(
                "report generation deferred: contract established; "
                "no Word/PDF/LLM synthesis in Prompt 2"
            ),
        )
        self._requests[report.request_id.as_text()] = report
        return ReportOperationResult(
            status=ReportOperationStatus.OK,
            message="report request recorded (rendering deferred)",
            report=report,
            evaluated_at=now,
        )

    def _render(
        self, workflow_id: WorkflowId, report_format: str, now: datetime
    ) -> ReportOperationResult:
        workflow = self._store.get_workflow(workflow_id)
        assert workflow is not None  # checked by execute()
        checkpoint = self._store.get_latest_checkpoint(workflow_id)
        memory = self._memory.list_for_workflow(workflow_id, limit=500) if self._memory else ()
        renderer = render_workflow_json if report_format == "json" else render_workflow_markdown
        content = renderer(workflow, checkpoint, memory, self._collect(workflow))
        request_id = ReportRequestId.new()
        stored = StoredWorkflowReport(
            report_id=request_id,
            workflow_id=workflow_id,
            report_format=report_format,
            title="research_report",
            content=content,
            created_at=now,
        )
        if self._report_store is not None:
            self._report_store.save(stored)
        report = ReportRequest(
            request_id=request_id,
            workflow_id=workflow_id,
            status=ReportStatus.REPORT_READY,
            created_at=now,
            updated_at=now,
            artifact=ReportArtifactMetadata(format=report_format, title="research_report"),
            message=f"report rendered deterministically as {report_format}",
        )
        self._requests[request_id.as_text()] = report
        return ReportOperationResult(
            status=ReportOperationStatus.OK,
            message="report rendered and stored",
            report=report,
            evaluated_at=now,
            stored_report=stored,
        )

    def _collect(self, workflow: object) -> ReportFindings | None:
        if self._findings is None:
            return None
        wf = workflow.to_dict()  # type: ignore[attr-defined]
        query = wf.get("company_query")
        if not isinstance(query, dict):
            return None
        try:
            company_query = company_query_from_workflow(query)
        except ValueError:
            return None
        objective = str(wf.get("objective") or "research").replace("_", " ")
        question = str(query.get("objective_text") or "") or (
            f"{objective.capitalize()}: what do the latest market data, financials, "
            "news and regulatory items say about this company?"
        )
        return self._findings.collect(company_query, question)
