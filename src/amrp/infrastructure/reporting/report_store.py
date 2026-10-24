"""In-memory WorkflowReportStorePort adapter (process-local, not durable)."""

from __future__ import annotations

from threading import RLock

from amrp.domain.report import ReportRequestId, StoredWorkflowReport
from amrp.domain.workflow import WorkflowId


class ReportStoreError(ValueError):
    """Report store conflict or bounds violation."""


class InMemoryWorkflowReportStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._reports: dict[str, StoredWorkflowReport] = {}

    def save(self, report: StoredWorkflowReport) -> None:
        key = report.report_id.as_text()
        with self._lock:
            if key in self._reports:
                msg = f"duplicate report_id: {key}"
                raise ReportStoreError(msg)
            self._reports[key] = report

    def get(self, report_id: ReportRequestId) -> StoredWorkflowReport | None:
        with self._lock:
            return self._reports.get(report_id.as_text())

    def list_for_workflow(
        self, workflow_id: WorkflowId, *, limit: int = 20
    ) -> tuple[StoredWorkflowReport, ...]:
        if limit < 1 or limit > 200:
            msg = "limit must be between 1 and 200"
            raise ReportStoreError(msg)
        with self._lock:
            items = [
                r
                for r in self._reports.values()
                if r.workflow_id.as_text() == workflow_id.as_text()
            ]
        items.sort(key=lambda r: (r.created_at.isoformat(), r.report_id.as_text()), reverse=True)
        return tuple(items[:limit])
