"""Deterministic workflow report rendering (JSON / Markdown).

Renders a terminal research workflow from data the platform already holds:
the workflow aggregate, its latest checkpoint (task results + evidence
references), and structured research-memory records. No LLM, no network, no
file I/O. All free text is escaped before it enters Markdown so stored
reports cannot carry active HTML or broken table structure. Like every
platform output, this is research context — not investment advice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence

from amrp.application.report_findings import (
    EventItem,
    Figure,
    ReportFindings,
    SectionStatus,
)
from amrp.domain.memory import ResearchMemoryRecord
from amrp.domain.workflow import ResearchWorkflow, WorkflowCheckpoint

DISCLAIMER = (
    "Research context generated deterministically from stored workflow evidence. "
    "Not investment advice. Fixture/demo data is labelled by its data origin."
)
_MAX_CELL = 300


def _md(text: object, *, limit: int = _MAX_CELL) -> str:
    raw = " ".join(str(text).split())
    if len(raw) > limit:
        raw = raw[: limit - 1] + "…"
    out = []
    for char in raw:
        if char in "\\`*_{}[]<>|#!":
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


def _dash(value: object) -> object:
    """Render missing values as an em dash instead of the string "None"."""

    return "—" if value is None or value == "" else value


def _rows(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(_md(cell) for cell in row) + " |" for row in rows)
    return lines


def _payload(
    workflow: ResearchWorkflow,
    checkpoint: WorkflowCheckpoint | None,
    memory: Sequence[ResearchMemoryRecord],
    findings: ReportFindings | None = None,
) -> dict[str, object]:
    return {
        "findings": findings.to_dict() if findings is not None else None,
        "kind": "workflow_report",
        "disclaimer": DISCLAIMER,
        "workflow": workflow.to_dict(),
        "checkpoint": checkpoint.to_dict() if checkpoint is not None else None,
        "research_memory": [record.to_dict() for record in memory],
    }


def render_workflow_json(
    workflow: ResearchWorkflow,
    checkpoint: WorkflowCheckpoint | None,
    memory: Sequence[ResearchMemoryRecord],
    findings: ReportFindings | None = None,
) -> str:
    return json.dumps(_payload(workflow, checkpoint, memory, findings), indent=2, sort_keys=True)


def planner_label(version: str) -> str:
    if "fallback" in version:
        return "deterministic (fallback: LLM planner failed or was rate-limited)"
    if version.startswith("llm"):
        return "LLM (free model)"
    return "deterministic"


def task_breakdown(workflow: ResearchWorkflow) -> str:
    """Human task counts; ``completed_count`` includes partial successes."""

    partial = workflow.partial_count
    full = max(workflow.completed_count - partial, 0)
    total = len(workflow.plan.tasks)
    return (
        f"{total} total — {full} fully completed, {partial} partial, "
        f"{workflow.failed_count} failed, {workflow.blocked_count} blocked"
    )


def render_workflow_markdown(
    workflow: ResearchWorkflow,
    checkpoint: WorkflowCheckpoint | None,
    memory: Sequence[ResearchMemoryRecord],
    findings: ReportFindings | None = None,
) -> str:
    wf = workflow.to_dict()
    query = wf.get("company_query")
    query_map: Mapping[str, object] = query if isinstance(query, Mapping) else {}
    label = " / ".join(
        str(query_map[key]) for key in ("raw_query", "exchange", "country") if query_map.get(key)
    )
    lines = [
        f"# Research Report: {_md(label or workflow.company_id.as_text())}",
        "",
        f"> {_md(DISCLAIMER, limit=500)}",
        "",
        "## Summary",
        "",
        *_rows(
            ("Field", "Value"),
            [
                ("Workflow ID", workflow.workflow_id.as_text()),
                ("Research Run ID", workflow.research_run_id.as_text()),
                ("Company ID", workflow.company_id.as_text()),
                ("Objective", wf.get("objective")),
                ("Status", wf.get("status")),
                ("Approval", wf.get("approval_status")),
                ("Created", wf.get("created_at")),
                ("Updated", wf.get("updated_at")),
                ("Planner", planner_label(workflow.plan.planner_version)),
                ("Tasks", task_breakdown(workflow)),
                ("Evidence references", workflow.evidence_count),
            ],
        ),
        "",
    ]
    if findings is not None:
        lines += _findings_lines(findings)
    if workflow.execution_message:
        lines += ["## Execution Outcome", "", _md(workflow.execution_message, limit=1000), ""]
    agent_rows = wf.get("agent_decisions")
    if isinstance(agent_rows, list) and agent_rows:
        lines += ["## Agent Decisions", ""]
        lines += _rows(
            ("Agent", "Decided by", "Detail"),
            [_decision_row(d) for d in agent_rows if isinstance(d, Mapping)],
        )
        lines.append("")
    results = checkpoint.to_dict().get("task_results", []) if checkpoint is not None else []
    lines += ["## Task Results", ""]
    if isinstance(results, list) and results:
        capability_by_task = {t.task_id.as_text(): t.capability_id for t in workflow.plan.tasks}
        lines += _rows(
            ("Capability", "Status", "Result", "Message"),
            [
                (
                    capability_by_task.get(str(r.get("task_id")), "task"),
                    r.get("status"),
                    _dash(r.get("output_summary")),
                    _dash(r.get("message")),
                )
                for r in results
                if isinstance(r, Mapping)
            ],
        )
    else:
        lines.append("Unavailable: no task results were recorded for this workflow.")
    lines.append("")
    refs = checkpoint.to_dict().get("evidence_refs", []) if checkpoint is not None else []
    lines += ["## Evidence References", ""]
    if isinstance(refs, list) and refs:
        lines += _rows(
            ("Source ID", "Authority tier", "Data origin", "As of", "Retrieved"),
            [
                (
                    _dash(e.get("source_id")),
                    _dash(e.get("authority_tier")),
                    _dash(e.get("data_origin")),
                    _dash(e.get("as_of")),
                    _dash(e.get("retrieved_at")),
                )
                for e in refs
                if isinstance(e, Mapping)
            ],
        )
    else:
        lines.append("Unavailable: no evidence references were recorded.")
    lines += ["", "## Research Memory", ""]
    if memory:
        lines += _rows(
            ("Capability", "Status", "Data origin", "Summary"),
            [
                (m.get("capability"), m.get("status"), m.get("data_origin"), m.get("summary"))
                for m in (record.to_dict() for record in memory)
            ],
        )
    else:
        lines.append("Unavailable: no research-memory records.")
    if workflow.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {_md(w, limit=500)}" for w in workflow.warnings]
    lines.append("")
    return "\n".join(lines)


def _decision_row(decision: Mapping[str, object]) -> tuple[object, object, object]:
    """One labelled agent decision: who decided (llm / deterministic / human) and what."""

    source = str(decision.get("source") or "-")
    if decision.get("fallback_reason"):
        source += f" (fallback: {decision.get('fallback_reason')})"
    parts: list[str] = []
    proposed = decision.get("proposed")
    if isinstance(proposed, list):
        caps = [str(p.get("capability_id")) for p in proposed if isinstance(p, Mapping)]
        parts.append("proposed: " + (", ".join(caps) if caps else "none"))
    for key in ("planner_version", "rationale", "outcome", "note"):
        if decision.get(key):
            parts.append(f"{key}: {decision.get(key)}")
    return (decision.get("agent") or "-", source, _md("; ".join(parts) or "-", limit=600))


_ESCAPED_TAG = re.compile(r"\\\[([MFNR]\d{1,2})\\\](?!\()")


def _cite(line: str) -> str:
    """Show escaped evidence tags as plain ``[F1]`` (never as a link target)."""

    return _ESCAPED_TAG.sub(r"[\1]", line)


def _section_note(status: SectionStatus | None) -> str:
    if status is None:
        return "_Unavailable: this section could not be retrieved._"
    origin = status.data_origin or "unknown"
    badge = {
        "live": "LIVE",
        "cached_live": "LIVE (cached)",
        "fixture": "FIXTURE / DEMO DATA",
    }.get(origin, origin)
    parts = [f"Data origin: **{_md(badge)}**", f"provider: {_md(status.provider or 'n/a')}"]
    parts.append(f"status: {_md(status.status)}")
    if status.as_of:
        parts.append(f"as of: {_md(status.as_of)}")
    note = "_" + ", ".join(parts) + "_"
    if status.status not in {"ok"} and status.message:
        note += f" — {_md(status.message)}"
    return note


def _figure_table(figures: Sequence[Figure]) -> list[str]:
    return _rows(
        ("Ref", "Metric", "Value", "Detail"),
        [(f"[{f.tag}]", f.label, f.value, f.detail or "—") for f in figures],
    )


def _event_list(items: Sequence[EventItem]) -> list[str]:
    out = []
    for item in items:
        link = f" — <{item.url}>" if item.url and item.url.startswith("https://") else ""
        detail = f": {_md(item.detail, limit=220)}" if item.detail else ""
        out.append(f"- **[{item.tag}]** {_md(item.date)} — {_md(item.title)}{detail}{link}")
    return out


def _findings_lines(findings: ReportFindings) -> list[str]:
    lines: list[str] = []
    summary = findings.summary
    if summary is not None:
        heading = (
            "## Analyst Summary (LLM-generated)"
            if summary.generated_by.startswith("llm:")
            else "## Analyst Summary (deterministic, no LLM)"
        )
        lines += [heading, ""]
        lines.append(f"**Question:** {_md(findings.question, limit=500)}")
        lines.append("")
        lines += [_md(par, limit=1500) for par in summary.text.split("\n") if par.strip()]
        lines.append("")
        source = summary.generated_by.removeprefix("llm:")
        lines.append(
            f"_Generated by: {_md(source)}. {_md(summary.note, limit=400)} "
            "Citations refer to the [tags] in the tables below. Not investment advice._"
        )
        lines.append("")
    want = set(findings.sections)
    if want & {"market", "financial"}:
        lines += ["## Key Figures", ""]
    if "market" in want:
        lines += ["### Market", "", _section_note(findings.market), ""]
        lines += (
            _figure_table(findings.market_figures)
            if findings.market_figures
            else ["_No market figures available._"]
        )
        lines.append("")
    if "financial" in want:
        lines += ["### Financials", "", _section_note(findings.financial), ""]
        lines += (
            _figure_table(findings.financial_figures)
            if findings.financial_figures
            else ["_No financial figures available._"]
        )
        lines.append("")
    if "news" in want:
        lines += ["## Top News", "", _section_note(findings.news), ""]
        lines += _event_list(findings.news_items) or ["_No news items found._"]
        lines.append("")
    if "industry" in want:
        lines += ["## Industry & Peers", "", _section_note(findings.industry), ""]
        if findings.industry_figures:
            lines += _figure_table(findings.industry_figures)
            lines.append("")
        lines += _event_list(findings.industry_items) or ["_No peer comparison available._"]
        lines.append("")
    if "regulatory" in want:
        lines += ["## Regulatory Items", "", _section_note(findings.regulatory), ""]
        lines += _event_list(findings.regulatory_items) or ["_No regulatory items found._"]
    if findings.errors:
        lines += ["", *[f"- _{_md(e)}_" for e in findings.errors]]
    lines.append("")
    return [_cite(line) for line in lines]
