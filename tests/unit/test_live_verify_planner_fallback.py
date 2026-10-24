from __future__ import annotations

from decimal import Decimal

from amrp.application.company_resolution import CompanyQuery
from amrp.application.workflow_contracts import CreateResearchWorkflowQuery
from amrp.composition import build_container
from amrp.config.settings import Settings
from amrp.domain.identity import ExchangeCode
from amrp.domain.llm import (
    ModelCallStatus,
    ModelFailureKind,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from amrp.domain.orchestration import ResearchObjective


class _RateLimited:
    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            call_id=request.call_id,
            status=ModelCallStatus.FAILED,
            model_used=None,
            content=None,
            usage=ModelUsage(estimated_cost=Decimal("0")),
            retry_count=0,
            failure_kind=ModelFailureKind.RATE_LIMITED,
        )


def test_rate_limited_llm_planner_falls_back_and_is_labelled() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        PLANNER_MODE="llm",
        OPENROUTER_LIVE_ENABLED=True,
        OPENROUTER_API_KEY="test-key",
        PRIMARY_FREE_MODEL="x/y:free",
    )
    container = build_container(settings, llm_router=_RateLimited())
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
            objective=ResearchObjective.MARKET_ANALYSIS,
        )
    )
    assert created.workflow is not None, created.message
    assert "fallback" in created.workflow.plan.planner_version
    container.manage_research_workflow.execute(created.workflow.workflow_id)
    report = container.request_research_report.execute(
        created.workflow.workflow_id, report_format="markdown"
    )
    assert report.stored_report is not None
    assert "fallback: LLM planner failed" in report.stored_report.content
