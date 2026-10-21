from __future__ import annotations

from datetime import UTC, datetime

from financial_intelligence.application.company_resolution import CompanyQuery
from financial_intelligence.application.manage_research_workflow import _memory_origin
from financial_intelligence.application.workflow_contracts import CreateResearchWorkflowQuery
from financial_intelligence.composition import build_container
from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.identity import ExchangeCode
from financial_intelligence.domain.orchestration import ResearchObjective, TaskEvidenceRef
from financial_intelligence.domain.sources import SourceId
from tests.unit.market_fixtures import APPLE_COMPANY_ID


def _ref(origin: DataOrigin) -> TaskEvidenceRef:
    return TaskEvidenceRef(
        source_id=SourceId.new(),
        company_id=APPLE_COMPANY_ID,
        retrieved_at=datetime(2026, 10, 1, tzinfo=UTC),
        data_origin=origin,
    )


def test_origin_never_overclaims_live() -> None:
    assert _memory_origin(()) is None
    assert _memory_origin((_ref(DataOrigin.LIVE),)) is DataOrigin.LIVE
    assert _memory_origin((_ref(DataOrigin.LIVE), _ref(DataOrigin.FIXTURE))) is DataOrigin.FIXTURE
    assert _memory_origin((_ref(DataOrigin.LIVE), _ref(DataOrigin.CACHED_LIVE))) is DataOrigin.LIVE


def test_memory_records_carry_only_their_own_task_evidence() -> None:
    container = build_container()
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
            objective=ResearchObjective.COMPANY_OVERVIEW,
        )
    )
    assert created.workflow is not None
    wf_id = created.workflow.workflow_id
    executed = container.manage_research_workflow.execute(wf_id)
    assert executed.workflow is not None and executed.workflow.latest_checkpoint is not None
    by_task = {
        r.task_id.as_text(): {e.source_id for e in r.evidence_refs}
        for r in executed.workflow.latest_checkpoint.task_results
    }
    records = container.research_memory.list_for_workflow(wf_id, limit=50)
    assert len(records) >= 2
    for record in records:
        assert {e.source_id for e in record.evidence_refs} <= by_task[record.task_id.as_text()]


def test_memory_summary_is_the_task_result_not_the_instruction() -> None:
    container = build_container()
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
            objective=ResearchObjective.MARKET_ANALYSIS,
        )
    )
    assert created.workflow is not None
    executed = container.manage_research_workflow.execute(created.workflow.workflow_id)
    assert executed.workflow is not None and executed.workflow.latest_checkpoint is not None
    results = {r.task_id.as_text(): r for r in executed.workflow.latest_checkpoint.task_results}
    records = container.research_memory.list_for_workflow(created.workflow.workflow_id, limit=9)
    assert records
    for record in records:
        result = results[record.task_id.as_text()]
        assert record.summary == (result.output_summary or result.message)
        assert "origin=" in record.summary  # e.g. "market status=... origin=fixture ..."
