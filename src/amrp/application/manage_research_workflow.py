"""Execute / pause / resume / approve / cancel / list research workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from amrp.application.agent_decisions import (
    REPLAN_DESCRIPTION_PREFIX,
    encode_decision,
)
from amrp.application.company_resolution import CompanyQuery
from amrp.application.execute_research_plan import ExecuteResearchPlan
from amrp.application.ports import (
    NotificationPort,
    ResearchMemoryPort,
    ResearchWorkflowStorePort,
)
from amrp.application.replan_round import Replanner
from amrp.application.research_execution_contracts import (
    ExecuteResearchPlanQuery,
    ResearchExecutionResult,
    ResearchExecutionStatus,
)
from amrp.application.resolve_company import ResolveCompany
from amrp.application.workflow_contracts import (
    ApprovalActionQuery,
    WorkflowListQuery,
    WorkflowListResult,
    WorkflowOperationResult,
    WorkflowOperationStatus,
)
from amrp.domain.data_origin import DataOrigin
from amrp.domain.identity import CompanyIdentity
from amrp.domain.memory import (
    MemoryRecordId,
    MemoryRecordStatus,
    ResearchMemoryRecord,
)
from amrp.domain.notification import (
    NotificationEvent,
    NotificationId,
    NotificationType,
)
from amrp.domain.orchestration import (
    ExecutionControl,
    ResearchPlan,
    ResearchTask,
    TaskStatus,
)
from amrp.domain.orchestration.results import TaskEvidenceRef, TaskExecutionResult
from amrp.domain.workflow import (
    ApprovalDecision,
    ApprovalStatus,
    ResearchWorkflow,
    WorkflowCheckpoint,
    WorkflowId,
    WorkflowStatus,
    WorkflowTransitionError,
    is_terminal,
)


class ManageResearchWorkflow:
    """Coordinate Phase 6 execution through a persisted workflow aggregate."""

    def __init__(
        self,
        workflow_store: ResearchWorkflowStorePort,
        execute_research_plan: ExecuteResearchPlan,
        resolve_company: ResolveCompany,
        *,
        research_memory: ResearchMemoryPort | None = None,
        notifications: NotificationPort | None = None,
        clock: Callable[[], datetime] | None = None,
        replanner: Replanner | None = None,
    ) -> None:
        self._replanner = replanner
        self._store = workflow_store
        self._execute = execute_research_plan
        self._resolve = resolve_company
        self._memory = research_memory
        self._notifications = notifications
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, workflow_id: WorkflowId) -> WorkflowOperationResult:
        now = self._clock()
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        return WorkflowOperationResult(
            status=WorkflowOperationStatus.OK,
            message="workflow loaded",
            workflow=workflow,
            evaluated_at=now,
        )

    def list(self, query: WorkflowListQuery) -> WorkflowListResult:
        now = self._clock()
        workflows = self._store.list_workflows(
            status=query.status,
            company_id_text=query.company_id,
            limit=query.limit,
            offset=query.offset,
        )
        return WorkflowListResult(
            status=WorkflowOperationStatus.OK,
            message="workflow list",
            workflows=workflows,
            limit=query.limit,
            offset=query.offset,
            evaluated_at=now,
        )

    def execute(
        self,
        workflow_id: WorkflowId,
        *,
        control: ExecutionControl | None = None,
    ) -> WorkflowOperationResult:
        now = self._clock()
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if is_terminal(workflow.status):
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"cannot execute terminal workflow status={workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )
        if workflow.approval_status is ApprovalStatus.PENDING:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.APPROVAL_REQUIRED,
                message="human approval required before execution",
                workflow=workflow,
                evaluated_at=now,
            )
        if workflow.approval_status is ApprovalStatus.REJECTED:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.REJECTED,
                message="workflow approval was rejected",
                workflow=workflow,
                evaluated_at=now,
            )
        if workflow.status not in {
            WorkflowStatus.READY,
            WorkflowStatus.PAUSED,
            WorkflowStatus.RUNNING,
        }:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"invalid status for execution: {workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )

        try:
            running = workflow.with_status(WorkflowStatus.RUNNING, at=now)
        except ValueError as exc:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=str(exc),
                workflow=workflow,
                evaluated_at=now,
            )
        conflict = self._commit_or_conflict(workflow_id, running, now=now)
        if conflict is not None:
            return conflict

        company_query = CompanyQuery(
            raw_query=running.company_query.raw_query,
            country=running.company_query.country,
            exchange=running.company_query.exchange,
            ticker=running.company_query.ticker,
        )
        resolution = self._resolve.execute(company_query)
        if resolution.company is None or resolution.company.company_id != running.company_id:
            failed = running.with_status(WorkflowStatus.FAILED, at=self._clock())
            failed = replace(failed, execution_message="company identity mismatch on execute")
            conflict = self._commit_or_conflict(workflow_id, failed, now=self._clock())
            if conflict is not None:
                return conflict
            self._notify(
                NotificationType.WORKFLOW_FAILED,
                failed,
                message="company identity mismatch on execute",
            )
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.FAILED,
                message="company identity mismatch on execute",
                workflow=failed,
                resolution=resolution,
                evaluated_at=self._clock(),
            )

        control = control or ExecutionControl()
        prior = running.latest_checkpoint
        exec_result = self._execute.execute_prepared(
            running.plan,
            company=resolution.company,
            company_query=company_query,
            resolution=resolution,
            query=ExecuteResearchPlanQuery(
                company_query=company_query,
                objective=running.objective,
            ),
            control=control,
            started_at=now,
            initial_total_attempts=prior.total_attempts if prior else 0,
            initial_external_calls=prior.external_calls if prior else 0,
            initial_results=prior.task_results if prior else (),
            initial_warnings=prior.warnings if prior else (),
        )
        if self._should_replan(running, exec_result, control):
            replanned = self._replan_round(
                running,
                exec_result,
                company=resolution.company,
                company_query=company_query,
                resolution=resolution,
                control=control,
            )
            if isinstance(replanned, WorkflowOperationResult):
                return replanned
            running, exec_result = replanned
        return self._apply_execution_result(running, exec_result, pause_requested=control.is_pause)

    # ------------------------------------------------------------ replan round

    def _should_replan(
        self,
        workflow: ResearchWorkflow,
        exec_result: ResearchExecutionResult,
        control: ExecutionControl,
    ) -> bool:
        return (
            self._replanner is not None
            and workflow.replan_rounds == 0
            and not control.is_pause
            and not control.is_cancelled
            and exec_result.plan is not None
            and exec_result.orchestration is not None
            and exec_result.status
            in {
                ResearchExecutionStatus.COMPLETED,
                ResearchExecutionStatus.PARTIAL,
                ResearchExecutionStatus.FAILED,
                ResearchExecutionStatus.BLOCKED,
            }
        )

    def _replan_round(
        self,
        running: ResearchWorkflow,
        exec_result: ResearchExecutionResult,
        *,
        company: CompanyIdentity,
        company_query: CompanyQuery,
        resolution: object,
        control: ExecutionControl,
    ) -> tuple[ResearchWorkflow, ResearchExecutionResult] | WorkflowOperationResult:
        """Review the first pass, propose extra tasks, and run them (or ask for approval)."""

        assert self._replanner is not None
        assert exec_result.plan is not None and exec_result.orchestration is not None
        orchestration = exec_result.orchestration
        decision = self._replanner.propose(
            exec_result.plan,
            exec_result.task_results,
            question=running.company_query.objective_text,
            used_attempts=orchestration.total_attempts,
            used_external_calls=orchestration.external_calls,
        )
        record = decision.to_record()
        if not decision.tasks:
            record["outcome"] = "no extra tasks"
            return (
                replace(
                    running,
                    replan_rounds=1,
                    agent_decisions=(*running.agent_decisions, encode_decision(record)),
                ),
                exec_result,
            )
        try:
            extended = decision.apply(exec_result.plan)
        except ValueError:
            record["outcome"] = "rejected: extended plan failed validation"
            return (
                replace(
                    running,
                    replan_rounds=1,
                    agent_decisions=(*running.agent_decisions, encode_decision(record)),
                ),
                exec_result,
            )
        if running.approval_requirement.required:
            record["outcome"] = "awaiting human approval"
            return self._await_replan_approval(running, exec_result, extended, record)
        record["outcome"] = "executed"
        updated = replace(
            running,
            replan_rounds=1,
            agent_decisions=(*running.agent_decisions, encode_decision(record)),
        )
        second = self._execute.execute_prepared(
            extended,
            company=company,
            company_query=company_query,
            resolution=resolution,
            query=ExecuteResearchPlanQuery(
                company_query=company_query, objective=running.objective
            ),
            control=control,
            started_at=self._clock(),
            initial_total_attempts=orchestration.total_attempts,
            initial_external_calls=orchestration.external_calls,
            initial_results=exec_result.task_results,
            initial_warnings=(
                *exec_result.warnings,
                f"replan round ({decision.source}) added {len(decision.tasks)} task(s)",
            ),
        )
        return updated, second

    def _await_replan_approval(
        self,
        running: ResearchWorkflow,
        exec_result: ResearchExecutionResult,
        extended: ResearchPlan,
        record: dict[str, object],
    ) -> WorkflowOperationResult:
        """Checkpoint the first pass and wait for a fresh human approval of the replan tasks."""

        now = self._clock()
        assert exec_result.orchestration is not None
        message = (
            f"replan round proposed {len(extended.tasks) - len(running.plan.tasks)} task(s); "
            "human approval required before they run"
        )
        checkpoint = WorkflowCheckpoint(
            workflow_id=running.workflow_id,
            research_run_id=running.research_run_id,
            version=running.checkpoint_version + 1,
            plan=extended,
            created_at=now,
            total_attempts=exec_result.orchestration.total_attempts,
            external_calls=exec_result.orchestration.external_calls,
            warnings=exec_result.warnings,
            task_results=exec_result.task_results,
            evidence_refs=exec_result.evidence_refs,
            message=message,
        )
        updated = running.with_checkpoint(checkpoint, at=now)
        updated = replace(
            updated,
            replan_rounds=1,
            agent_decisions=(*running.agent_decisions, encode_decision(record)),
            # The first approval stays recorded; the replan tasks need a fresh decision.
            approval_status=ApprovalStatus.PENDING,
            completed_count=exec_result.completed_count,
            failed_count=exec_result.failed_count,
            blocked_count=exec_result.blocked_count,
            partial_count=exec_result.partial_count,
            evidence_count=len(exec_result.evidence_refs),
            execution_message=message,
        ).with_status(WorkflowStatus.AWAITING_APPROVAL, at=now)
        conflict = self._commit_or_conflict(running.workflow_id, updated, now=now)
        if conflict is not None:
            return conflict
        self._store.save_checkpoint(checkpoint)
        self._notify(NotificationType.APPROVAL_REQUIRED, updated, message=message)
        return WorkflowOperationResult(
            status=WorkflowOperationStatus.APPROVAL_REQUIRED,
            message=message,
            workflow=updated,
            evaluated_at=now,
        )

    def pause(self, workflow_id: WorkflowId) -> WorkflowOperationResult:
        now = self._clock()
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if workflow.status is WorkflowStatus.PAUSED:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.OK,
                message="workflow already paused",
                workflow=workflow,
                evaluated_at=now,
            )
        if workflow.status is not WorkflowStatus.RUNNING:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"cannot pause workflow status={workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )
        checkpoint = self._build_checkpoint(workflow, at=now, message="manual pause checkpoint")
        paused = workflow.with_checkpoint(checkpoint, at=now).with_status(
            WorkflowStatus.PAUSED, at=now
        )
        # Commit the workflow status first: if the store rejects it because the
        # record has moved on concurrently (e.g. a cancel landed first), the
        # checkpoint below must not be persisted either (F03).
        conflict = self._commit_or_conflict(workflow_id, paused, now=now)
        if conflict is not None:
            return conflict
        self._store.save_checkpoint(checkpoint)
        return WorkflowOperationResult(
            status=WorkflowOperationStatus.OK,
            message="workflow paused",
            workflow=paused,
            evaluated_at=now,
        )

    def resume(self, workflow_id: WorkflowId) -> WorkflowOperationResult:
        now = self._clock()
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if workflow.status is not WorkflowStatus.PAUSED:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"cannot resume workflow status={workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )
        if workflow.approval_status is ApprovalStatus.PENDING:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.APPROVAL_REQUIRED,
                message="human approval required before resume",
                workflow=workflow,
                evaluated_at=now,
            )
        ready = workflow.with_status(WorkflowStatus.READY, at=now)
        conflict = self._commit_or_conflict(workflow_id, ready, now=now)
        if conflict is not None:
            return conflict
        return self.execute(workflow_id)

    def cancel(self, workflow_id: WorkflowId) -> WorkflowOperationResult:
        """Terminal cancel — distinct from soft pause; not resumable."""

        now = self._clock()
        workflow = self._store.get_workflow(workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if is_terminal(workflow.status):
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"cannot cancel terminal workflow status={workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )
        try:
            cancelled = workflow.with_status(WorkflowStatus.CANCELLED, at=now)
        except ValueError as exc:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=str(exc),
                workflow=workflow,
                evaluated_at=now,
            )
        cancelled = replace(cancelled, execution_message="workflow cancelled by trusted API")
        conflict = self._commit_or_conflict(workflow_id, cancelled, now=now)
        if conflict is not None:
            return conflict
        self._notify(
            NotificationType.WORKFLOW_CANCELLED,
            cancelled,
            message="workflow cancelled",
        )
        return WorkflowOperationResult(
            status=WorkflowOperationStatus.OK,
            message="workflow cancelled",
            workflow=cancelled,
            evaluated_at=now,
        )

    def approve(self, query: ApprovalActionQuery) -> WorkflowOperationResult:
        now = self._clock()
        workflow = self._store.get_workflow(query.workflow_id)
        if workflow is None:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.NOT_FOUND,
                message="workflow not found",
                evaluated_at=now,
            )
        if workflow.status is not WorkflowStatus.AWAITING_APPROVAL:
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=f"workflow not awaiting approval status={workflow.status.value}",
                workflow=workflow,
                evaluated_at=now,
            )
        decision = ApprovalDecision(
            status=query.decision,
            decided_at=now,
            note=query.note,
            decision_source=query.decision_source,
        )
        if query.decision is ApprovalStatus.REJECTED and _pending_replan_tasks(workflow):
            return self._reject_replan(workflow, decision, now=now)
        if query.decision is ApprovalStatus.APPROVED:
            updated = workflow.with_approval(decision, at=now, next_status=WorkflowStatus.READY)
            message = "workflow approved"
            op = WorkflowOperationStatus.OK
        else:
            updated = workflow.with_approval(decision, at=now, next_status=WorkflowStatus.FAILED)
            updated = replace(updated, execution_message="approval rejected")
            message = "workflow approval rejected"
            op = WorkflowOperationStatus.REJECTED
        conflict = self._commit_or_conflict(query.workflow_id, updated, now=now)
        if conflict is not None:
            return conflict
        return WorkflowOperationResult(
            status=op,
            message=message,
            workflow=updated,
            evaluated_at=now,
        )

    def _reject_replan(
        self, workflow: ResearchWorkflow, decision: ApprovalDecision, *, now: datetime
    ) -> WorkflowOperationResult:
        """A rejected replan drops only the proposed tasks; the first pass is finalized as-is."""

        pending = {t.task_id.as_text() for t in _pending_replan_tasks(workflow)}
        reverted = workflow.plan.with_tasks(
            tuple(
                t
                if t.description.startswith(REPLAN_DESCRIPTION_PREFIX)
                else replace(t, required=True)
                for t in workflow.plan.tasks
                if t.task_id.as_text() not in pending
            )
        )
        record = encode_decision(
            {
                "agent": "human",
                "source": decision.decision_source,
                "outcome": "replan rejected; first-pass results kept",
                "note": decision.note,
                "dropped_tasks": len(pending),
            }
        )
        updated = replace(
            workflow,
            plan=reverted,
            approval_status=ApprovalStatus.APPROVED,
            agent_decisions=(*workflow.agent_decisions, record),
        ).with_status(WorkflowStatus.READY, at=now)
        conflict = self._commit_or_conflict(workflow.workflow_id, updated, now=now)
        if conflict is not None:
            return conflict
        return self.execute(workflow.workflow_id)

    def _apply_execution_result(
        self,
        workflow: ResearchWorkflow,
        exec_result: object,
        *,
        pause_requested: bool,
    ) -> WorkflowOperationResult:
        now = self._clock()
        assert isinstance(exec_result, ResearchExecutionResult)
        plan = exec_result.plan if exec_result.plan is not None else workflow.plan
        checkpoint = WorkflowCheckpoint(
            workflow_id=workflow.workflow_id,
            research_run_id=workflow.research_run_id,
            version=workflow.checkpoint_version + 1,
            plan=plan,
            created_at=now,
            total_attempts=(
                exec_result.orchestration.total_attempts
                if exec_result.orchestration is not None
                else 0
            ),
            external_calls=(
                exec_result.orchestration.external_calls
                if exec_result.orchestration is not None
                else 0
            ),
            warnings=exec_result.warnings,
            task_results=exec_result.task_results,
            evidence_refs=exec_result.evidence_refs,
            message=exec_result.message,
        )
        updated = workflow.with_checkpoint(checkpoint, at=now)
        notify_type: NotificationType | None = None
        if pause_requested:
            terminal = WorkflowStatus.PAUSED
            op = WorkflowOperationStatus.OK
            message = "workflow paused with checkpoint"
        elif exec_result.status is ResearchExecutionStatus.CANCELLED:
            terminal = WorkflowStatus.CANCELLED
            op = WorkflowOperationStatus.OK
            message = "workflow cancelled"
            notify_type = NotificationType.WORKFLOW_CANCELLED
        elif exec_result.status is ResearchExecutionStatus.COMPLETED:
            terminal = WorkflowStatus.COMPLETED
            op = WorkflowOperationStatus.OK
            message = "workflow completed"
            notify_type = NotificationType.WORKFLOW_COMPLETED
        elif exec_result.status is ResearchExecutionStatus.PARTIAL:
            terminal = WorkflowStatus.PARTIAL
            op = WorkflowOperationStatus.OK
            message = "workflow completed with partial results"
            notify_type = NotificationType.WORKFLOW_PARTIAL
        elif exec_result.status is ResearchExecutionStatus.BUDGET_EXCEEDED:
            terminal = WorkflowStatus.FAILED
            op = WorkflowOperationStatus.FAILED
            message = "workflow failed: budget exceeded"
            notify_type = NotificationType.WORKFLOW_FAILED
        else:
            terminal = WorkflowStatus.FAILED
            op = WorkflowOperationStatus.FAILED
            message = f"workflow failed: {exec_result.status.value}"
            notify_type = NotificationType.WORKFLOW_FAILED

        warnings = list(exec_result.warnings)
        memory_warning = self._persist_memory(updated, plan=plan, at=now)
        if memory_warning is not None:
            warnings.append(memory_warning)

        updated = replace(
            updated,
            completed_count=exec_result.completed_count,
            failed_count=exec_result.failed_count,
            blocked_count=exec_result.blocked_count,
            partial_count=exec_result.partial_count,
            evidence_count=len(exec_result.evidence_refs),
            execution_message=exec_result.message,
            warnings=tuple(warnings),
        )
        updated = updated.with_status(terminal, at=now)
        # Commit the workflow status first, and only if it is accepted persist
        # the checkpoint: a stale execution (one whose long-running work
        # finished after the workflow was cancelled/paused/completed by a
        # concurrent operation) must not be able to commit either its result or
        # its checkpoint data (F03: cancellation race).
        conflict = self._commit_or_conflict(workflow.workflow_id, updated, now=now)
        if conflict is not None:
            return conflict
        self._store.save_checkpoint(checkpoint)
        if notify_type is not None:
            notify_warning = self._notify(notify_type, updated, message=message)
            if notify_warning is not None:
                updated = replace(updated, warnings=(*updated.warnings, notify_warning))
                self._store.save_workflow(updated)
        return WorkflowOperationResult(
            status=op,
            message=message,
            workflow=updated,
            evaluated_at=now,
        )

    def _commit_or_conflict(
        self,
        workflow_id: WorkflowId,
        workflow: ResearchWorkflow,
        *,
        now: datetime,
    ) -> WorkflowOperationResult | None:
        """Attempt to persist ``workflow``; return a CONFLICT result instead of
        raising if the store rejects it because the record has moved on
        concurrently (e.g. cancelled/paused/completed by another operation
        while this one was still running -- F03). Returns None on success.
        """
        try:
            self._store.save_workflow(workflow)
        except WorkflowTransitionError as exc:
            current = self._store.get_workflow(workflow_id)
            return WorkflowOperationResult(
                status=WorkflowOperationStatus.CONFLICT,
                message=(
                    "workflow lifecycle changed concurrently; stale execution "
                    f"result discarded: {exc}"
                ),
                workflow=current,
                evaluated_at=now,
            )
        return None

    def _persist_memory(
        self, workflow: ResearchWorkflow, *, plan: object, at: datetime
    ) -> str | None:
        if self._memory is None:
            return None
        from amrp.domain.orchestration import ResearchPlan

        assert isinstance(plan, ResearchPlan)
        checkpoint = workflow.latest_checkpoint
        results_by_task = {
            r.task_id.as_text(): r for r in (checkpoint.task_results if checkpoint else ())
        }
        try:
            for task in plan.tasks:
                if task.status not in {
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.SKIPPED,
                    TaskStatus.BLOCKED,
                }:
                    continue
                status_map = {
                    TaskStatus.SUCCEEDED: MemoryRecordStatus.SUCCEEDED,
                    TaskStatus.FAILED: MemoryRecordStatus.FAILED,
                    TaskStatus.SKIPPED: MemoryRecordStatus.SKIPPED,
                    TaskStatus.BLOCKED: MemoryRecordStatus.FAILED,
                }
                # Per-task evidence only: each capability carries its OWN refs and
                # origin (a live market task must not inherit a fixture origin).
                task_result = results_by_task.get(task.task_id.as_text())
                refs = tuple(
                    r
                    for r in (task_result.evidence_refs if task_result else ())
                    if r.company_id == workflow.company_id
                )
                origin = _memory_origin(refs)
                record = ResearchMemoryRecord(
                    record_id=MemoryRecordId.new(),
                    workflow_id=workflow.workflow_id,
                    research_run_id=workflow.research_run_id,
                    company_id=workflow.company_id,
                    capability=task.capability_id,
                    task_id=task.task_id,
                    status=status_map[task.status],
                    summary=_memory_summary(task, task_result),
                    created_at=at,
                    evidence_refs=refs,
                    data_origin=origin,
                )
                try:
                    self._memory.append(record)
                except ValueError:
                    # Duplicate task completion on resume is expected; skip.
                    continue
        except Exception as exc:
            return f"research_memory_write_failed:{type(exc).__name__}"
        return None

    def _notify(
        self,
        notification_type: NotificationType,
        workflow: ResearchWorkflow,
        *,
        message: str,
    ) -> str | None:
        if self._notifications is None:
            return None
        event = NotificationEvent(
            notification_id=NotificationId.new(),
            notification_type=notification_type,
            created_at=self._clock(),
            workflow_id=workflow.workflow_id,
            company_id=workflow.company_id.as_text(),
            message=message,
            metadata=(
                ("status", workflow.status.value),
                ("approval_status", workflow.approval_status.value),
            ),
        )
        try:
            self._notifications.publish(event)
        except Exception as exc:
            return f"notification_failed:{type(exc).__name__}"
        return None

    def _build_checkpoint(
        self, workflow: ResearchWorkflow, *, at: datetime, message: str
    ) -> WorkflowCheckpoint:
        return WorkflowCheckpoint(
            workflow_id=workflow.workflow_id,
            research_run_id=workflow.research_run_id,
            version=workflow.checkpoint_version + 1,
            plan=workflow.plan,
            created_at=at,
            total_attempts=(
                workflow.latest_checkpoint.total_attempts if workflow.latest_checkpoint else 0
            ),
            external_calls=(
                workflow.latest_checkpoint.external_calls if workflow.latest_checkpoint else 0
            ),
            warnings=workflow.warnings,
            task_results=(
                workflow.latest_checkpoint.task_results if workflow.latest_checkpoint else ()
            ),
            evidence_refs=(
                workflow.latest_checkpoint.evidence_refs if workflow.latest_checkpoint else ()
            ),
            message=message,
        )


def _memory_origin(refs: tuple[TaskEvidenceRef, ...]) -> DataOrigin | None:
    """Single origin for one task's evidence; never overclaims live data.

    Any fixture evidence makes the record fixture; otherwise live beats cached.
    """

    origins = {r.data_origin for r in refs if r.data_origin is not None}
    for candidate in (DataOrigin.FIXTURE, DataOrigin.UNAVAILABLE, DataOrigin.CACHED_LIVE):
        if candidate in origins and (candidate is DataOrigin.FIXTURE or len(origins) == 1):
            return candidate
    if DataOrigin.LIVE in origins:
        return DataOrigin.LIVE
    return next(iter(origins), None)


def _memory_summary(task: ResearchTask, result: TaskExecutionResult | None) -> str:
    """What the task FOUND (result line), not the planner's instruction text."""

    text = (result.output_summary or result.message) if result is not None else None
    if not text:
        text = f"planned: {task.description}" if task.description else task.capability_id
    return text[:1000]


def _pending_replan_tasks(workflow: ResearchWorkflow) -> tuple[ResearchTask, ...]:
    """Replan-round tasks still waiting for approval (only after a proposed replan)."""

    if workflow.replan_rounds == 0:
        return ()
    return tuple(
        t
        for t in workflow.plan.tasks
        if t.description.startswith(REPLAN_DESCRIPTION_PREFIX) and t.status is TaskStatus.PENDING
    )
