from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amrp.dashboard.report_export import (
    DISCLAIMER,
    markdown_to_html,
    markdown_to_pdf,
    parse_markdown,
    pdf_available,
    report_filename,
)

SAMPLE = """# Research Report: AAPL

> Not investment advice. Fixture/demo data is labelled.

## Summary

| Field | Value |
|---|---|
| Objective | company\\_overview |
| Tasks | 5 total \u2014 5 completed |

## Analyst Summary

Apple closed at **$343.83** [M1].
Second line of paragraph.

- [N1] Apple news [link](https://example.com/a)
"""


def test_filename_is_safe_and_dated() -> None:
    when = datetime(2026, 10, 29, tzinfo=UTC)
    assert report_filename("Apple (AAPL)", "pdf", when) == "AMRP-Apple-2026-10-29.pdf"
    assert report_filename("A/B: C&D", ".md", when) == "AMRP-A_B_C_D-2026-10-29.md"
    assert report_filename("", "html", when) == "AMRP-report-2026-10-29.html"


def test_parse_markdown_blocks() -> None:
    kinds = [b.kind for b in parse_markdown(SAMPLE)]
    assert kinds == ["h1", "quote", "h2", "table", "h2", "p", "bullet"]
    table = next(b for b in parse_markdown(SAMPLE) if b.kind == "table")
    assert table.rows[0] == ["Field", "Value"]
    assert table.rows[1] == ["Objective", "company_overview"]


def test_html_document_has_tables_links_and_disclaimer() -> None:
    doc = markdown_to_html(SAMPLE, "Apple <report>")
    assert doc.startswith("<!doctype html>")
    assert "<title>Apple &lt;report&gt;</title>" in doc
    assert "<table>" in doc and "<th>Field</th>" in doc
    assert '<a href="https://example.com/a">link</a>' in doc
    assert "<b>$343.83</b>" in doc
    assert DISCLAIMER in doc


def test_empty_markdown_is_handled() -> None:
    assert parse_markdown("") == []
    assert DISCLAIMER in markdown_to_html("", "x")


@pytest.mark.skipif(not pdf_available(), reason="fpdf2 not installed")
def test_pdf_is_valid_and_multi_section() -> None:
    long_md = SAMPLE + "\n".join(f"| [F{i}] | metric {i} | {i}% | FY2025 |" for i in range(80))
    pdf = markdown_to_pdf(long_md, "Apple research report \u2014 test")
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 2000
