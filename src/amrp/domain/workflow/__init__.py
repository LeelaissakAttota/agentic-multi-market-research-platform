"""Phase 7 workflow domain package — autonomous research workflow foundation."""

from amrp.domain.workflow.approval import (
    ApprovalDecision,
    ApprovalRequirement,
    ApprovalStatus,
)
from amrp.domain.workflow.checkpoint import WorkflowCheckpoint
from amrp.domain.workflow.ids import WorkflowId
from amrp.domain.workflow.model import ResearchWorkflow, WorkflowCompanyQuery
from amrp.domain.workflow.status import (
    WorkflowStatus,
    WorkflowTransitionError,
    assert_transition,
    is_terminal,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalRequirement",
    "ApprovalStatus",
    "ResearchWorkflow",
    "WorkflowCheckpoint",
    "WorkflowCompanyQuery",
    "WorkflowId",
    "WorkflowStatus",
    "WorkflowTransitionError",
    "assert_transition",
    "is_terminal",
]
