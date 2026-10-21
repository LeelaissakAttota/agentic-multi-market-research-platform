"""Agentic planning: LLM planner with deterministic fallback, and the bounded replan round."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest import TestCase

from financial_intelligence.application.agent_decisions import REPLAN_DESCRIPTION_PREFIX
from financial_intelligence.application.capability_registry import CapabilityRegistry
from financial_intelligence.application.company_resolution import CompanyQuery
from financial_intelligence.application.replan_round import Replanner
from financial_intelligence.application.workflow_contracts import (
    ApprovalActionQuery,
    CreateResearchWorkflowQuery,
    WorkflowOperationStatus,
)
from financial_intelligence.composition import build_container
from financial_intelligence.config.settings import Settings
from financial_intelligence.domain.identity import ExchangeCode
from financial_intelligence.domain.llm import (
    ModelCallStatus,
    ModelFailureKind,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from financial_intelligence.domain.orchestration import (
    PlanId,
    PlannerOutcome,
    PlannerOutcomeStatus,
    ResearchObjective,
    ResearchPlan,
    ResearchTask,
    TaskId,
    TaskStatus,
    TaskType,
)
from financial_intelligence.domain.orchestration.results import (
    TaskExecutionResult,
    TaskResultStatus,
)
from financial_intelligence.domain.research_run import ResearchRunId
from financial_intelligence.domain.workflow import ApprovalStatus, WorkflowStatus
from financial_intelligence.infrastructure.orchestration import FallbackPlannerAdapter

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
Reply = str | ModelFailureKind


class ScriptedRouter:
    """LlmRouterPort fake answering per prompt version from queues (last item repeats)."""

    def __init__(self, **queues: list[Reply]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: dict[str, int] = {}

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = (
            "replan"
            if request.prompt_version.startswith("llm-replanner")
            else ("plan" if request.prompt_version.startswith("llm-planner") else "other")
        )
        self.calls[key] = self.calls.get(key, 0) + 1
        queue = self.queues.get(key) or [ModelFailureKind.UNKNOWN]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, ModelFailureKind):
            return ModelResponse(
                call_id=request.call_id,
                status=ModelCallStatus.FAILED,
                model_used=None,
                content=None,
                usage=ModelUsage(),
                retry_count=0,
                failure_kind=reply,
            )
        return ModelResponse(
            call_id=request.call_id,
            status=ModelCallStatus.SUCCEEDED,
            model_used="free/model-a",
            content=reply,
            usage=ModelUsage(),
            retry_count=0,
        )


class FlakyExecutor:
    """Wrap the real capability executor; fail the first N calls of chosen capabilities."""

    def __init__(self, inner: Any, fail_first: dict[str, int]) -> None:
        self.inner = inner
        self.fail_first = dict(fail_first)
        self.calls: list[str] = []

    def execute_task(self, task: ResearchTask, **kwargs: Any) -> TaskExecutionResult:
        self.calls.append(task.capability_id)
        if self.fail_first.get(task.capability_id, 0) > 0:
            self.fail_first[task.capability_id] -= 1
            return TaskExecutionResult(
                task_id=task.task_id,
                status=TaskResultStatus.FAILED,
                message="provider timed out",
                retryable=False,
                error_code="timeout",
            )
        result: TaskExecutionResult = self.inner.execute_task(task, **kwargs)
        return result


def plan_json(*capabilities: str) -> str:
    return json.dumps(
        {
            "type": "research_plan",
            "tasks": [
                {
                    "capability_id": c,
                    "description": f"run {c}",
                    "depends_on": [],
                    "priority": 10 + i,
                }
                for i, c in enumerate(capabilities)
            ],
        }
    )


def replan_json(*capabilities: str, rationale: str = "retry what failed") -> str:
    return json.dumps(
        {
            "type": "replan",
            "rationale": rationale,
            "tasks": [
                {"capability_id": c, "reason": f"retry {c} for missing data", "priority": 20}
                for c in capabilities
            ],
        }
    )


def llm_settings(**extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        OPENROUTER_LIVE_ENABLED=True,
        PRIMARY_FREE_MODEL="free/model-a",
        OPENROUTER_API_KEY="sk-test-key",
        **extra,
    )


def decisions(workflow: Any) -> list[dict[str, Any]]:
    return list(workflow.to_dict()["agent_decisions"])


class _Harness:
    def __init__(self, router: ScriptedRouter, fail_first: dict[str, int], **settings: Any) -> None:
        self.container = build_container(
            llm_settings(**settings), clock=lambda: NOW, llm_router=router
        )
        execute = self.container.manage_research_workflow._execute
        self.executor = FlakyExecutor(execute._executor, fail_first)
        execute._executor = self.executor

    def create(self, objective: ResearchObjective, **kw: Any) -> Any:
        result = self.container.create_research_workflow.execute(
            CreateResearchWorkflowQuery(
                company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
                objective=objective,
                objective_text="How is Apple performing?",
                **kw,
            )
        )
        assert result.workflow is not None, result.message
        return result.workflow

    def approve(self, workflow: Any, decision: ApprovalStatus = ApprovalStatus.APPROVED) -> Any:
        return self.container.manage_research_workflow.approve(
            ApprovalActionQuery(workflow_id=workflow.workflow_id, decision=decision)
        )

    def execute(self, workflow: Any) -> Any:
        return self.container.manage_research_workflow.execute(workflow.workflow_id)


# ------------------------------------------------------------------ planner fallback


class _FixedPlanner:
    def __init__(self, outcome: PlannerOutcome | Exception, version: str) -> None:
        self.outcome = outcome
        self.planner_version = version
        self.calls = 0

    def create_plan(self, request: Any) -> PlannerOutcome:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _plan(version: str = "phase6-deterministic-v1") -> ResearchPlan:
    from financial_intelligence.domain.identity import CompanyId

    return ResearchPlan(
        plan_id=PlanId.new(),
        research_run_id=ResearchRunId.new(created_at=NOW),
        objective=ResearchObjective.MARKET_ANALYSIS,
        company_id=CompanyId.new(),
        tasks=(
            ResearchTask(
                task_id=TaskId.new(),
                task_type=TaskType.MARKET_INTELLIGENCE,
                capability_id="market_intelligence",
                description="market",
                created_at=NOW,
            ),
        ),
        created_at=NOW,
        planner_version=version,
    )


class FallbackPlannerTests(TestCase):
    def _ok(self, version: str) -> PlannerOutcome:
        plan = _plan(version)
        return PlannerOutcome(
            status=PlannerOutcomeStatus.OK, message="ok", plan=plan, planner_version=version
        )

    def test_llm_success_passes_through(self) -> None:
        llm = _FixedPlanner(self._ok("llm-planner-v1"), "llm-planner-v1")
        det = _FixedPlanner(self._ok("phase6-deterministic-v1"), "phase6-deterministic-v1")
        outcome = FallbackPlannerAdapter(llm, det).create_plan(object())  # type: ignore[arg-type]
        assert outcome.plan is not None
        self.assertEqual(outcome.plan.planner_version, "llm-planner-v1")
        self.assertEqual(det.calls, 0)

    def test_llm_failure_falls_back_and_is_labelled(self) -> None:
        for failure in (
            PlannerOutcome(
                status=PlannerOutcomeStatus.FAILED,
                message="planner model call failed (rate_limited)",
            ),
            PlannerOutcome(
                status=PlannerOutcomeStatus.FAILED, message="planner output was not valid JSON"
            ),
            PlannerOutcome(
                status=PlannerOutcomeStatus.UNAVAILABLE, message="planner model is unavailable"
            ),
            RuntimeError("boom"),
        ):
            llm = _FixedPlanner(failure, "llm-planner-v1")
            det = _FixedPlanner(self._ok("phase6-deterministic-v1"), "phase6-deterministic-v1")
            request = type("R", (), {"request_id": TaskId.new()})()
            outcome = FallbackPlannerAdapter(llm, det).create_plan(request)  # type: ignore[arg-type]
            self.assertIs(outcome.status, PlannerOutcomeStatus.OK)
            assert outcome.plan is not None
            self.assertEqual(outcome.plan.planner_version, "phase6-deterministic-v1+llm-fallback")
            self.assertTrue(outcome.message.startswith("deterministic planner used after llm"))

    def test_invalid_request_is_not_retried(self) -> None:
        invalid = PlannerOutcome(
            status=PlannerOutcomeStatus.INVALID, message="company query is invalid"
        )
        det = _FixedPlanner(self._ok("phase6-deterministic-v1"), "phase6-deterministic-v1")
        outcome = FallbackPlannerAdapter(_FixedPlanner(invalid, "llm"), det).create_plan(object())  # type: ignore[arg-type]
        self.assertIs(outcome.status, PlannerOutcomeStatus.INVALID)
        self.assertEqual(det.calls, 0)


# ------------------------------------------------------------------ replanner unit


def _executed(
    statuses: dict[str, TaskResultStatus],
) -> tuple[ResearchPlan, tuple[TaskExecutionResult, ...]]:
    from financial_intelligence.domain.identity import CompanyId

    registry = CapabilityRegistry()
    tasks: list[ResearchTask] = []
    results: list[TaskExecutionResult] = []
    for i, (cid, status) in enumerate(statuses.items()):
        cap = registry.require(cid)
        task = ResearchTask(
            task_id=TaskId.new(),
            task_type=cap.task_type,
            capability_id=cid,
            description=cid,
            status=TaskStatus.SUCCEEDED
            if status in {TaskResultStatus.SUCCESS, TaskResultStatus.PARTIAL}
            else TaskStatus.FAILED,
            priority=10 + i,
            attempt_count=1,
            created_at=NOW,
        )
        tasks.append(task)
        results.append(
            TaskExecutionResult(task_id=task.task_id, status=status, message=f"{cid} done")
        )
    plan = ResearchPlan(
        plan_id=PlanId.new(),
        research_run_id=ResearchRunId.new(created_at=NOW),
        objective=ResearchObjective.COMPANY_OVERVIEW,
        company_id=CompanyId.new(),
        tasks=tuple(tasks),
        created_at=NOW,
    )
    return plan, tuple(results)


S, F, P = TaskResultStatus.SUCCESS, TaskResultStatus.FAILED, TaskResultStatus.PARTIAL


class ReplannerTests(TestCase):
    def _replanner(self, router: ScriptedRouter | None) -> Replanner:
        return Replanner(CapabilityRegistry(), router=router, clock=lambda: NOW)

    def test_llm_retry_and_add_are_validated_and_supersede_failures(self) -> None:
        plan, results = _executed({"market_intelligence": F, "financial_intelligence": P})
        router = ScriptedRouter(
            replan=[replan_json("market_intelligence", "industry_intelligence")]
        )
        decision = self._replanner(router).propose(plan, results)
        self.assertEqual(decision.source, "llm")
        self.assertEqual(
            [t.capability_id for t in decision.tasks],
            ["market_intelligence", "industry_intelligence"],
        )
        self.assertTrue(
            all(t.description.startswith(REPLAN_DESCRIPTION_PREFIX) for t in decision.tasks)
        )
        self.assertEqual(len(decision.superseded), 1)  # only the FAILED one, not the partial
        extended = decision.apply(plan)
        self.assertEqual(len(extended.tasks), 4)
        failed = next(t for t in extended.tasks if t.status is TaskStatus.FAILED)
        self.assertFalse(failed.required)

    def test_llm_failures_fall_back_to_deterministic_retry(self) -> None:
        plan, results = _executed({"market_intelligence": F, "financial_intelligence": S})
        cases: list[tuple[Reply, str]] = [
            (ModelFailureKind.RATE_LIMITED, "rate_limited"),
            (ModelFailureKind.TIMEOUT, "timeout"),
            ("I think you should retry the market data.", "malformed_json"),
            (json.dumps({"type": "replan", "tasks": "all"}), "invalid_structure"),
            (replan_json("company_resolution"), "capability_not_allowed"),
            (replan_json("market_intelligence", "market_intelligence"), "duplicate_capability"),
            (
                replan_json(
                    "market_intelligence",
                    "industry_intelligence",
                    "news_event_intelligence",
                    "regulatory_intelligence",
                ),
                "too_many_tasks",
            ),
        ]
        for reply, reason in cases:
            with self.subTest(reason=reason):
                decision = self._replanner(ScriptedRouter(replan=[reply])).propose(plan, results)
                self.assertEqual(decision.source, "deterministic")
                self.assertEqual(decision.fallback_reason, reason)
                self.assertEqual([t.capability_id for t in decision.tasks], ["market_intelligence"])

    def test_llm_may_decline_and_nothing_to_replan_is_skipped(self) -> None:
        plan, results = _executed({"market_intelligence": F})
        decision = self._replanner(ScriptedRouter(replan=[replan_json()])).propose(plan, results)
        self.assertEqual((decision.source, decision.tasks), ("llm", ()))
        full, ok = _executed(
            {
                c: S
                for c in (
                    "market_intelligence",
                    "financial_intelligence",
                    "news_event_intelligence",
                    "industry_intelligence",
                    "regulatory_intelligence",
                )
            }
        )
        router = ScriptedRouter()
        self.assertEqual(self._replanner(router).propose(full, ok).source, "skipped")
        self.assertEqual(router.calls, {})  # no LLM call when there is nothing to decide

    def test_budget_caps_replan(self) -> None:
        plan, results = _executed({"market_intelligence": F})
        decision = self._replanner(None).propose(plan, results, used_attempts=40)
        self.assertEqual(decision.source, "skipped")


# ------------------------------------------------------------------ workflow integration


class WorkflowReplanTests(TestCase):
    def test_llm_planner_then_replan_recovers_failed_task(self) -> None:
        router = ScriptedRouter(
            plan=[plan_json("market_intelligence", "financial_intelligence")],
            replan=[replan_json("market_intelligence")],
        )
        h = _Harness(router, {"market_intelligence": 1})
        wf = h.create(ResearchObjective.COMPANY_OVERVIEW)
        self.assertEqual(wf.plan.planner_version, "llm-planner-v1")
        result = h.execute(wf)
        workflow = result.workflow
        # Fixture market data is stale (PARTIAL), but the failed task was recovered.
        self.assertIn(workflow.status, {WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL})
        self.assertNotIn("required tasks failed", workflow.execution_message or "")
        self.assertEqual(workflow.replan_rounds, 1)
        self.assertEqual(h.executor.calls.count("market_intelligence"), 2)
        rows = decisions(workflow)
        self.assertEqual(
            [(d["agent"], d["source"]) for d in rows], [("planner", "llm"), ("replanner", "llm")]
        )
        self.assertEqual(rows[1]["outcome"], "executed")
        # bounded: a second execute does not replan again
        self.assertEqual(router.calls["replan"], 1)

    def test_comprehensive_replan_waits_for_fresh_human_approval(self) -> None:
        router = ScriptedRouter(
            plan=[ModelFailureKind.RATE_LIMITED], replan=[replan_json("news_event_intelligence")]
        )
        h = _Harness(router, {"news_event_intelligence": 1})
        wf = h.create(ResearchObjective.COMPREHENSIVE_EQUITY_RESEARCH)
        self.assertIs(wf.status, WorkflowStatus.AWAITING_APPROVAL)
        self.assertEqual(wf.plan.planner_version, "phase6-deterministic-v1+llm-fallback")
        self.assertEqual(
            decisions(wf)[0]["fallback_reason"], "planner model call failed (rate_limited)"
        )
        # replan tasks must not run without approval
        self.assertIs(h.execute(wf).status, WorkflowOperationStatus.APPROVAL_REQUIRED)
        h.approve(wf)
        first = h.execute(wf)
        self.assertIs(first.status, WorkflowOperationStatus.APPROVAL_REQUIRED)
        self.assertIs(first.workflow.status, WorkflowStatus.AWAITING_APPROVAL)
        self.assertIs(first.workflow.approval_status, ApprovalStatus.PENDING)
        calls_before = len(h.executor.calls)
        self.assertEqual(calls_before, 5)
        self.assertIs(h.execute(wf).status, WorkflowOperationStatus.APPROVAL_REQUIRED)
        self.assertEqual(len(h.executor.calls), calls_before)  # still nothing ran
        approved = h.approve(wf)
        self.assertIs(approved.workflow.status, WorkflowStatus.READY)
        final = h.execute(wf).workflow
        self.assertIn(final.status, {WorkflowStatus.COMPLETED, WorkflowStatus.PARTIAL})
        self.assertNotIn("required tasks failed", final.execution_message or "")
        self.assertEqual(len(h.executor.calls), 6)
        rows = decisions(final)
        self.assertEqual(rows[1]["outcome"], "awaiting human approval")
        self.assertEqual(router.calls["replan"], 1)

    def test_rejected_replan_keeps_first_pass_results(self) -> None:
        router = ScriptedRouter(
            plan=[ModelFailureKind.TIMEOUT], replan=[replan_json("news_event_intelligence")]
        )
        h = _Harness(router, {"news_event_intelligence": 1})
        wf = h.create(ResearchObjective.COMPREHENSIVE_EQUITY_RESEARCH)
        h.approve(wf)
        h.execute(wf)
        final = h.approve(wf, ApprovalStatus.REJECTED).workflow
        self.assertIs(final.status, WorkflowStatus.PARTIAL, final.execution_message)
        self.assertIn("required tasks failed", final.execution_message or "")
        self.assertEqual(len(final.plan.tasks), 5)
        self.assertEqual(len(h.executor.calls), 5)
        self.assertEqual(decisions(final)[-1]["agent"], "human")

    def test_llm_down_everywhere_uses_deterministic_paths(self) -> None:
        router = ScriptedRouter(
            plan=[ModelFailureKind.RATE_LIMITED], replan=[ModelFailureKind.RATE_LIMITED]
        )
        h = _Harness(router, {"market_intelligence": 1})
        wf = h.create(ResearchObjective.COMPANY_OVERVIEW)
        workflow = h.execute(wf).workflow
        rows = decisions(workflow)
        self.assertEqual(
            [(d["agent"], d["source"]) for d in rows],
            [("planner", "deterministic"), ("replanner", "deterministic")],
        )
        self.assertEqual(rows[1]["fallback_reason"], "rate_limited")
        self.assertNotIn("required tasks failed", workflow.execution_message or "")

    def test_replan_off_and_default_without_llm(self) -> None:
        h = _Harness(
            ScriptedRouter(plan=[plan_json("market_intelligence")]),
            {"market_intelligence": 1},
            REPLAN_MODE="off",
        )
        workflow = h.execute(h.create(ResearchObjective.COMPANY_OVERVIEW)).workflow
        self.assertEqual(workflow.replan_rounds, 0)
        plain = build_container(Settings(_env_file=None, APP_ENV="test", LOG_LEVEL="WARNING"))
        self.assertIsNone(plain.manage_research_workflow._replanner)
