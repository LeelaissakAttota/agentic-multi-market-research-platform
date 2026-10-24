"""``PlannerPort`` that tries the LLM planner first and falls back per request.

``PLANNER_MODE=auto`` (the default) wires this adapter when an OpenRouter key and
live LLM calls are configured. Every request first goes to the LLM planner; if it
returns anything but ``OK`` — rate limit, timeout, provider error, malformed JSON,
an unknown/duplicate capability, an invalid dependency graph or an over-budget plan
— the deterministic planner builds the plan instead. ``INVALID`` outcomes (bad
company query, unresolved identity) are returned as-is: the deterministic planner
would reject the same request.

The fallback is labelled: the plan's ``planner_version`` becomes
``<deterministic version>+llm-fallback`` and the outcome message names the LLM
failure (a fixed, safe string produced by the LLM adapter).
"""

from __future__ import annotations

from dataclasses import replace

from amrp.application.agent_decisions import (
    FALLBACK_MESSAGE_PREFIX,
    FALLBACK_PLANNER_SUFFIX,
)
from amrp.application.ports import PlannerPort
from amrp.domain.orchestration import (
    PlannerOutcome,
    PlannerOutcomeStatus,
    ResearchRequest,
)
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.orchestration.fallback_planner_adapter")

FALLBACK_SUFFIX = FALLBACK_PLANNER_SUFFIX


class FallbackPlannerAdapter:
    """LLM planner with automatic, per-request deterministic fallback."""

    def __init__(
        self,
        primary: PlannerPort,
        fallback: PlannerPort,
        *,
        primary_version: str = "llm-planner-v1",
        fallback_version: str = "deterministic",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._versions = (
            str(getattr(primary, "planner_version", primary_version)),
            str(getattr(fallback, "planner_version", fallback_version)),
        )

    @property
    def primary(self) -> PlannerPort:
        return self._primary

    @property
    def fallback(self) -> PlannerPort:
        return self._fallback

    @property
    def planner_version(self) -> str:
        return f"auto({self._versions[0]}|{self._versions[1]})"

    def create_plan(self, request: ResearchRequest) -> PlannerOutcome:
        try:
            outcome = self._primary.create_plan(request)
        except Exception as exc:  # PlannerPort must not raise; be defensive anyway
            outcome = PlannerOutcome(
                status=PlannerOutcomeStatus.FAILED,
                message=f"llm planner raised {type(exc).__name__}",
            )
        if outcome.status is PlannerOutcomeStatus.OK and outcome.plan is not None:
            return outcome
        if outcome.status is PlannerOutcomeStatus.INVALID:
            return outcome
        logger.warning(
            "llm_planner_fallback",
            extra={"request_id": request.request_id.as_text(), "reason": outcome.message},
        )
        fallback = self._fallback.create_plan(request)
        if fallback.status is not PlannerOutcomeStatus.OK or fallback.plan is None:
            return fallback
        plan = replace(
            fallback.plan, planner_version=fallback.plan.planner_version + FALLBACK_SUFFIX
        )
        return PlannerOutcome(
            status=PlannerOutcomeStatus.OK,
            message=FALLBACK_MESSAGE_PREFIX + outcome.message,
            plan=plan,
            planner_version=plan.planner_version,
        )
