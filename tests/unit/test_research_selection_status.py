"""Research objective selection and Completed/Partial status (relative to selection)."""

from __future__ import annotations

from amrp.dashboard.report_view import build_report_view
from amrp.dashboard.views import (
    ALL_SECTIONS,
    merge_workflows,
    research_args,
    research_status,
    status_label,
    workflow_rows,
)


def test_research_args_single_and_multi() -> None:
    assert research_args([]) == {}
    assert research_args(["industry_analysis"]) == {
        "objective": "industry_analysis",
        "sections": ("industry",),
    }
    multi = research_args(["regulatory_analysis", "market_analysis"])
    assert multi["objective"] == "comprehensive_equity_research"
    assert multi["sections"] == ("market", "regulatory")
    assert multi["auto_approve"] is True


def test_status_relative_to_selected_sections() -> None:
    findings = {"industry_figures": [{"label": "Sector"}], "regulatory_items": []}
    assert research_status(findings, ("industry",)) == ("Completed", [])
    assert research_status(findings, ("industry", "regulatory")) == ("Partial", ["Regulatory"])
    status, missing = research_status(findings, ALL_SECTIONS)
    assert status == "Partial" and "Market" in missing and "Industry" not in missing


def test_status_label_and_rows_use_research_status() -> None:
    api = [{"workflow_id": "w1", "status": "partial", "objective": "industry_analysis"}]
    local = [
        {
            "workflow_id": "w1",
            "research_status": "Completed",
            "missing_sections": [],
            "sections": ["industry"],
        }
    ]
    merged = merge_workflows(api, local)
    assert status_label(merged[0]) == "Completed"
    rows = workflow_rows(
        [{"workflow_id": "w2", "research_status": "Partial", "missing_sections": ["News"]}]
    )
    assert rows[0]["Status"] == "Partial" and rows[0]["Missing"] == "News"


def test_report_view_keeps_only_selected_sections() -> None:
    data = {
        "findings": {
            "sections": ["industry"],
            "market": {"status": "ok"},
            "market_figures": [{"label": "Last close", "value": "1"}],
            "industry": {"status": "ok"},
            "industry_figures": [{"label": "Sector", "value": "Tech"}],
        }
    }
    view = build_report_view(data, "X")
    assert [s.title for s in view.sections] == ["Industry"]
