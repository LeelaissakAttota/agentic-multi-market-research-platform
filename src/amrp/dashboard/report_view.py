"""Shape the workflow JSON report for display (badges, sections)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_BADGES = {
    "live": ("LIVE", "green"),
    "cached_live": ("LIVE (cached)", "blue"),
    "fixture": ("SAMPLE DATA", "orange"),
}


def origin_badge(origin: str | None, status: str | None = None) -> tuple[str, str]:
    """(label, colour) for a data_origin value; unknown origins are shown honestly."""

    if not origin and status == "unavailable":
        return ("NOT AVAILABLE", "gray")
    if not origin:
        return ("UNKNOWN SOURCE", "gray")
    return _BADGES.get(origin, (origin.upper(), "gray"))


def badge_markdown(origin: str | None, status: str | None = None) -> str:
    label, colour = origin_badge(origin, status)
    return f":{colour}-background[{label}]"


@dataclass
class Section:
    title: str
    origin: str | None
    status: str | None
    message: str | None
    as_of: str | None
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReportView:
    company: str
    question: str | None
    summary: str
    summary_by: str
    summary_note: str | None
    sections: list[Section]
    errors: list[str]


def _section(title: str, meta: Any, rows: Any) -> Section:
    meta = meta if isinstance(meta, dict) else {}
    return Section(
        title=title,
        origin=meta.get("data_origin"),
        status=meta.get("status"),
        message=meta.get("message"),
        as_of=meta.get("as_of"),
        rows=[r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [],
    )


def build_report_view(report_json: str | dict[str, Any], company: str = "") -> ReportView:
    data = json.loads(report_json) if isinstance(report_json, str) else report_json
    findings = data.get("findings") or {}
    summary = findings.get("analyst_summary") or {}
    sections = [
        _section("Market", findings.get("market"), findings.get("market_figures")),
        _section("Financials", findings.get("financial"), findings.get("financial_figures")),
        _section("Top news", findings.get("news"), findings.get("news_items")),
        _section("Regulatory", findings.get("regulatory"), findings.get("regulatory_items")),
        _section(
            "Industry",
            findings.get("industry"),
            [*(findings.get("industry_figures") or []), *(findings.get("industry_items") or [])],
        ),
    ]
    return ReportView(
        company=company,
        question=findings.get("question"),
        summary=str(summary.get("text") or "No analyst summary available."),
        summary_by=str(summary.get("generated_by") or "unknown"),
        summary_note=summary.get("note"),
        sections=[s for s in sections if s.rows or s.status],
        errors=[str(e) for e in findings.get("errors") or []],
    )


def provider_strip(payload: Any) -> list[tuple[str, str, str]]:
    """[(provider, state, icon)] from /health/providers."""

    items = payload.get("providers", []) if isinstance(payload, dict) else []
    icons = {"ok": "🟢", "degraded": "🟡", "failing": "🔴"}
    out = []
    for p in items:
        if isinstance(p, dict) and p.get("provider"):
            state = str(p.get("state") or "unknown")
            out.append((str(p["provider"]), state, icons.get(state, "⚪")))
    return out
