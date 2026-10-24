from __future__ import annotations

from amrp.application.company_resolution import CompanyQuery
from amrp.application.workflow_contracts import CreateResearchWorkflowQuery
from amrp.composition import build_container
from amrp.domain.identity import ExchangeCode
from amrp.domain.orchestration import ResearchObjective


def _report(objective: ResearchObjective = ResearchObjective.MARKET_ANALYSIS) -> str:
    container = build_container()
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
            objective=objective,
        )
    )
    assert created.workflow is not None
    container.manage_research_workflow.execute(created.workflow.workflow_id)
    result = container.request_research_report.execute(
        created.workflow.workflow_id, report_format="markdown"
    )
    assert result.stored_report is not None
    return result.stored_report.content


def test_task_counts_do_not_double_count_partial() -> None:
    md = _report()
    assert "| Tasks | 1 total — 0 fully completed, 1 partial, 0 failed, 0 blocked |" in md


def test_tables_show_capabilities_and_no_none_cells() -> None:
    md = _report(ResearchObjective.COMPANY_OVERVIEW)
    assert "| Capability | Status | Result | Message |" in md
    assert "| None |" not in md and " None |" not in md
    section = md[md.index("## Task Results") : md.index("## Evidence References")]
    import re

    assert not re.search(r"\| [0-9a-f]{8}-[0-9a-f]{4}-", section)  # no raw task UUIDs
