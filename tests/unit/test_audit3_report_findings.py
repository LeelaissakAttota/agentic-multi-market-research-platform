from __future__ import annotations

from decimal import Decimal

from amrp.application.company_resolution import CompanyQuery
from amrp.application.report_findings import ALL_SECTIONS
from amrp.application.workflow_contracts import CreateResearchWorkflowQuery
from amrp.composition import build_container
from amrp.domain.identity import ExchangeCode
from amrp.domain.llm import (
    ModelCallStatus,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from amrp.domain.orchestration import ResearchObjective

QUESTION = "Is Apple's growth holding up?"


class _FakeLlm:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            call_id=request.call_id,
            status=ModelCallStatus.SUCCEEDED,
            model_used="fake/free-model:free",
            content=self.content,
            usage=ModelUsage(estimated_cost=Decimal("0"), cost_known=True),
            retry_count=0,
        )


def _report(
    llm: _FakeLlm | None = None,
    objective: ResearchObjective = ResearchObjective.MARKET_ANALYSIS,
    sections: tuple[str, ...] | None = ALL_SECTIONS,
) -> str:
    container = build_container(llm_router=llm) if llm else build_container()
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(raw_query="Apple", exchange=ExchangeCode("NASDAQ")),
            objective=objective,
            objective_text=QUESTION,
        )
    )
    assert created.workflow is not None
    container.manage_research_workflow.execute(created.workflow.workflow_id)
    result = container.request_research_report.execute(
        created.workflow.workflow_id, report_format="markdown", sections=sections
    )
    assert result.stored_report is not None
    return result.stored_report.content


def test_report_has_key_figures_news_regulatory_and_deterministic_summary() -> None:
    md = _report()
    for heading in (
        "## Analyst Summary (deterministic, no LLM)",
        "## Key Figures",
        "### Market",
        "### Financials",
        "## Top News",
        "## Regulatory Items",
    ):
        assert heading in md
    assert "Last close" in md and "USD 199.00" in md
    assert "Revenue growth" in md
    assert "FIXTURE / DEMO DATA" in md
    assert "[N1]" in md and "[R1]" in md
    assert QUESTION.replace("'", "'") in md


def test_llm_summary_is_labelled_cited_and_evidence_untrusted() -> None:
    llm = _FakeLlm("Revenue grew 2.02% [F1] while the stock closed at 199 [M1]. <b>x</b>")
    md = _report(llm)
    assert "## Analyst Summary (LLM-generated)" in md
    assert "fake/free-model:free" in md
    assert "[F1]" in md
    assert "<b>" not in md  # model output escaped
    prompt = llm.requests[-1].messages[0].content
    assert "untrusted" in prompt
    assert QUESTION in llm.requests[-1].messages[1].content


def test_llm_summary_without_citations_falls_back_to_deterministic() -> None:
    md = _report(_FakeLlm("Apple looks great, buy now."))
    assert "## Analyst Summary (deterministic, no LLM)" in md
    assert "no evidence citations" in md
    assert "buy now" not in md


def test_truncated_llm_answer_is_cut_to_complete_sentences() -> None:
    from amrp.application.report_findings import _clean_summary

    text = "Revenue grew [F1]. Margins held [F3]. Product execution risk appears in the"
    assert _clean_summary(text) == "Revenue grew [F1]. Margins held [F3]."
    assert _clean_summary("Done [F1].") == "Done [F1]."


def test_composition_caps_summary_tokens_at_router_ceiling() -> None:
    from amrp.config.settings import Settings

    settings = Settings(_env_file=None, OPENROUTER_MAX_OUTPUT_TOKENS=512)  # type: ignore[call-arg]
    llm = _FakeLlm("Fine [F1].")
    container = build_container(settings, llm_router=llm)
    collector = container.request_research_report._findings
    assert collector is not None and collector._max_tokens == 512


def test_fixture_events_are_withheld_from_llm_evidence() -> None:
    llm = _FakeLlm("Revenue grew [F1].")
    _report(llm)
    evidence = llm.requests[-1].messages[1].content
    assert "[R1]" not in evidence and "[N1]" not in evidence  # fixture-only sections
    assert "fixture demo items withheld" in evidence
    assert "Ignore previous instructions" not in evidence


def test_single_objective_report_contains_only_its_sections() -> None:
    md = _report(sections=None)  # derived from the market_analysis objective
    assert "### Market" in md
    for heading in ("### Financials", "## Top News", "## Regulatory Items", "## Industry & Peers"):
        assert heading not in md


def test_industry_objective_report_has_industry_section() -> None:
    md = _report(objective=ResearchObjective.INDUSTRY_ANALYSIS, sections=None)
    assert "## Industry & Peers" in md
    assert "Consumer Electronics" in md  # offline reference industry for Apple
    assert "### Market" not in md and "## Regulatory Items" not in md


def test_regulatory_objective_report_has_regulatory_section() -> None:
    md = _report(objective=ResearchObjective.REGULATORY_ANALYSIS, sections=None)
    assert "## Regulatory Items" in md and "[R1]" in md
    assert "### Market" not in md


def test_sections_override_limits_report() -> None:
    md = _report(sections=("news", "industry"))
    assert "## Top News" in md and "## Industry & Peers" in md
    assert "### Market" not in md
