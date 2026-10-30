"""Debate / Risk / Rating sections reach the report Markdown, HTML and PDF."""

from __future__ import annotations

from amrp.application.deliberation import append_to_report, deliberate, deliberation_markdown
from amrp.dashboard.report_export import markdown_to_html, parse_markdown, pdf_available
from tests.unit.deliberation_fixtures import FINDINGS

BASE = "# Apple research\n\n## Summary\n\nSolid quarter [F1].\n"


def test_sections_and_disclaimer_present() -> None:
    md = deliberation_markdown(deliberate(FINDINGS, "Apple"))
    for heading in ("## Rating", "## Debate", "## Risk"):
        assert heading in md
    assert "not investment advice" in md.lower()
    assert "| Aggressive |" in md and "Execution risk" in md


def test_append_is_idempotent() -> None:
    d = deliberate(FINDINGS, "Apple")
    once = append_to_report(BASE, d)
    assert append_to_report(once, d) == once
    assert once.startswith(BASE.rstrip()) and once.count("## Rating") == 1


def test_marker_is_invisible_in_exports() -> None:
    md = append_to_report(BASE, deliberate(FINDINGS, "Apple"))
    texts = [b.text for b in parse_markdown(md)]
    assert not any("amrp:deliberation" in t for t in texts)
    assert any(b.kind == "table" for b in parse_markdown(md))
    page = markdown_to_html(md, "Apple")
    assert (
        "Rating" in page
        and "amrp:deliberation" not in page.replace("&lt;", "<").split("<body", 1)[-1]
    )


def test_pdf_includes_agent_sections() -> None:
    if not pdf_available():
        return
    from amrp.dashboard.report_export import markdown_to_pdf

    pdf = markdown_to_pdf(append_to_report(BASE, deliberate(FINDINGS, "Apple")), "Apple")
    assert pdf.startswith(b"%PDF") and len(pdf) > 1500
