"""Optional pre-execution step gate for supervised (human-in-the-loop) control.

When no gate is supplied, ``ExecuteResearchPlan`` behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from amrp.domain.orchestration import ResearchTask, TaskExecutionResult
from amrp.domain.workflow import ApprovalStatus


@dataclass(frozen=True, slots=True)
class StepGateOutcome:
    """Decision for one task. ``status`` reuses the workflow ApprovalStatus vocabulary."""

    status: ApprovalStatus
    task: ResearchTask
    stopped: bool = False
    handle: object | None = None

    @property
    def approved(self) -> bool:
        return self.status in (ApprovalStatus.APPROVED, ApprovalStatus.NOT_REQUIRED)


class StepGatePort(Protocol):
    def before(self, task: ResearchTask) -> StepGateOutcome: ...

    def after(self, outcome: StepGateOutcome, result: TaskExecutionResult) -> None: ...
