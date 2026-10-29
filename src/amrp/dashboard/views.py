"""Pure helpers for the v2 dashboard pages (no UI framework import, unit-tested).

Everything here shapes data that already came from the REST/control APIs or the
local run history. Nothing invents numbers: missing values become ``None`` and the
UI shows them as "not available".
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amrp.control.secrets_store import default_config_dir

OBJECTIVE_LABELS: dict[str, str] = {
    "company_overview": "Company Overview",
    "market_analysis": "Market Analysis",
    "financial_analysis": "Financial Analysis",
    "news_and_events": "News & Events",
    "industry_analysis": "Industry Analysis",
    "regulatory_analysis": "Regulatory Analysis",
    "comprehensive_equity_research": "Comprehensive Research",
}
OBJECTIVE_HINTS: dict[str, str] = {
    "company_overview": "Business, listing, key facts",
    "market_analysis": "Price, returns, moving average",
    "financial_analysis": "Growth, margins, cash flow",
    "news_and_events": "Latest news and events",
    "industry_analysis": "Peers and industry context",
    "regulatory_analysis": "SEC / SEBI items",
    "comprehensive_equity_research": "Everything (needs approval)",
}
POPULAR_COMPANIES: tuple[str, ...] = ("Reliance", "TCS", "Apple", "Infosys", "HDFC Bank")
WORKFLOW_STEPS: tuple[str, ...] = ("Plan", "Research", "Verify", "Report", "Complete")
REPORT_TABS: tuple[str, ...] = (
    "Overview",
    "Financials",
    "News",
    "Industry",
    "Regulatory",
    "Report",
    "Evidence",
)
_LOCK = threading.Lock()


def objective_label(objective: str | None) -> str:
    return OBJECTIVE_LABELS.get(str(objective or ""), str(objective or "-").replace("_", " "))


# ------------------------------------------------------------------ workflows
def company_of(workflow: dict[str, Any]) -> str:
    query = workflow.get("company_query") or {}
    return str(query.get("raw_query") or query.get("ticker") or "-")


@dataclass(frozen=True)
class WorkflowStats:
    total: int
    completed: int
    pending: int
    failed: int


def workflow_stats(workflows: Iterable[dict[str, Any]]) -> WorkflowStats:
    items = [w for w in workflows if isinstance(w, dict)]
    completed = sum(1 for w in items if w.get("status") in ("completed", "partial"))
    pending = sum(1 for w in items if w.get("approval_status") == "pending")
    failed = sum(1 for w in items if w.get("status") in ("failed", "cancelled", "rejected"))
    return WorkflowStats(len(items), completed, pending, failed)


def status_label(workflow: dict[str, Any]) -> str:
    if workflow.get("research_status"):
        return str(workflow["research_status"])
    if workflow.get("approval_status") == "pending":
        return "Pending approval"
    if workflow.get("approval_status") == "rejected":
        return "Rejected"
    return str(workflow.get("status") or "unknown").replace("_", " ").capitalize()


def short_time(value: str | None) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM' (UTC), or '-'."""

    if not value:
        return "-"
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def workflow_rows(workflows: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for w in workflows:
        if not isinstance(w, dict):
            continue
        rows.append(
            {
                "Company": company_of(w),
                "Objective": objective_label(w.get("objective")),
                "Status": status_label(w),
                "Created": short_time(w.get("created_at")),
                "Missing": ", ".join(w.get("missing_sections") or []),
                "workflow_id": str(w.get("workflow_id") or ""),
            }
        )
    return sorted(rows, key=lambda r: r["Created"], reverse=True)


def split_approvals(
    workflows: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"pending": [], "approved": [], "rejected": []}
    for w in workflows:
        if not isinstance(w, dict):
            continue
        state = str(w.get("approval_status") or "")
        if state in out:
            out[state].append(w)
    return out


def step_states(workflow: dict[str, Any] | None) -> list[tuple[str, str]]:
    """[(step, 'done'|'current'|'todo'|'failed')] for the step tracker."""

    if not workflow:
        return [(s, "todo") for s in WORKFLOW_STEPS]
    status = str(workflow.get("status") or "")
    approval = str(workflow.get("approval_status") or "")
    if approval == "pending":
        reached = 0
    elif status in ("completed", "partial"):
        reached = len(WORKFLOW_STEPS)
    elif status in ("running", "executing"):
        reached = 1
    elif status in ("failed", "cancelled") or approval == "rejected":
        return [
            (s, "failed" if i == 1 else ("done" if i < 1 else "todo"))
            for i, s in enumerate(WORKFLOW_STEPS)
        ]
    else:
        reached = 0
    out = []
    for i, step in enumerate(WORKFLOW_STEPS):
        if i < reached:
            out.append((step, "done"))
        elif i == reached:
            out.append((step, "current"))
        else:
            out.append((step, "todo"))
    return out


# ------------------------------------------------------------------ report
def figure_value(rows: Iterable[dict[str, Any]], label: str) -> str | None:
    for r in rows:
        if isinstance(r, dict) and str(r.get("label", "")).lower() == label.lower():
            value = r.get("value")
            return str(value) if value not in (None, "") else None
    return None


def close_series(snapshot: dict[str, Any] | None) -> list[tuple[str, float]]:
    """[(session_date, close)] from a /market/snapshot response; [] when absent."""

    series = (snapshot or {}).get("series") or {}
    out = []
    for bar in series.get("bars") or []:
        try:
            out.append((str(bar["session_date"]), float(bar["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def latest_close(snapshot: dict[str, Any] | None) -> tuple[float | None, str | None, str | None]:
    """(close, currency, data_origin) of the newest bar, or (None, None, origin)."""

    snap = snapshot or {}
    series = snap.get("series") or {}
    bars = close_series(snap)
    origin = series.get("data_origin") or snap.get("data_origin")
    if not bars:
        return None, None, origin
    return bars[-1][1], series.get("currency"), origin


def evidence_refs(report: dict[str, Any]) -> list[dict[str, str]]:
    """Unique evidence references from research memory, for the Evidence tab."""

    seen: set[str] = set()
    out = []
    for rec in report.get("research_memory") or []:
        if not isinstance(rec, dict):
            continue
        for ref in rec.get("evidence_refs") or []:
            sid = str(ref.get("source_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(
                {
                    "Capability": str(rec.get("capability") or "-").replace("_", " "),
                    "Origin": str(ref.get("data_origin") or "unknown"),
                    "Authority tier": str(ref.get("authority_tier") or "-"),
                    "Retrieved": short_time(ref.get("retrieved_at")),
                    "Locator": str(ref.get("locator") or "-"),
                    "Source id": sid[:8],
                }
            )
    return out


# ------------------------------------------------------------------ reports list
def filter_reports(
    rows: Iterable[dict[str, str]],
    text: str = "",
    company: str = "All",
    objective: str = "All",
) -> list[dict[str, str]]:
    needle = text.strip().lower()
    out = []
    for r in rows:
        if company != "All" and r.get("Company") != company:
            continue
        if objective != "All" and r.get("Objective") != objective:
            continue
        if needle and needle not in " ".join(str(v) for v in r.values()).lower():
            continue
        out.append(r)
    return out


def to_csv(rows: list[dict[str, str]], drop: tuple[str, ...] = ("workflow_id",)) -> str:
    if not rows:
        return ""
    fields = [k for k in rows[0] if k not in drop]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ local stores
def _store(name: str) -> Path:
    return default_config_dir("amrp") / name


def _read_list(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_list(path: Path, items: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def load_watchlist(path: Path | None = None) -> list[str]:
    path = path or _store("dashboard_watchlist.json")
    return [str(x) for x in _read_list(path) if isinstance(x, str) and x.strip()]


def add_to_watchlist(name: str, path: Path | None = None) -> list[str]:
    path = path or _store("dashboard_watchlist.json")
    clean = name.strip()[:64]
    with _LOCK:
        items = load_watchlist(path)
        if clean and clean.lower() not in {i.lower() for i in items}:
            items.append(clean)
            _write_list(path, items)
    return items


def remove_from_watchlist(name: str, path: Path | None = None) -> list[str]:
    path = path or _store("dashboard_watchlist.json")
    with _LOCK:
        items = [i for i in load_watchlist(path) if i.lower() != name.strip().lower()]
        _write_list(path, items)
    return items


def load_history(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or _store("dashboard_history.json")
    return [x for x in _read_list(path) if isinstance(x, dict)]


def record_history(entry: dict[str, Any], path: Path | None = None, keep: int = 200) -> None:
    """Append a finished run (company, objective, workflow_id, status, ...) locally."""

    path = path or _store("dashboard_history.json")
    with _LOCK:
        items = [h for h in load_history(path) if h.get("workflow_id") != entry.get("workflow_id")]
        items.append({**entry, "recorded_at": datetime.now(UTC).isoformat()})
        _write_list(path, items[-keep:])


def history_as_workflows(history: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Local history entries in the same shape as API workflows (for merged tables)."""

    out = []
    for h in history:
        out.append(
            {
                "workflow_id": h.get("workflow_id"),
                "objective": h.get("objective"),
                "status": h.get("status"),
                "approval_status": h.get("approval_status"),
                "created_at": h.get("created_at") or h.get("recorded_at"),
                "company_query": {"raw_query": h.get("company")},
                "research_status": h.get("research_status"),
                "missing_sections": h.get("missing_sections") or [],
                "sections": h.get("sections"),
            }
        )
    return out


def merge_workflows(
    api: Iterable[dict[str, Any]], local: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """API workflows win; local history fills in runs from earlier API sessions."""

    merged: dict[str, dict[str, Any]] = {}
    for w in local:
        if w.get("workflow_id"):
            merged[str(w["workflow_id"])] = w
    for w in api:
        if isinstance(w, dict) and w.get("workflow_id"):
            key = str(w["workflow_id"])
            prev = merged.get(key) or {}
            extra = {
                k: prev[k]
                for k in ("research_status", "missing_sections", "sections")
                if prev.get(k)
            }
            merged[key] = {**w, **extra}
    return list(merged.values())


# ------------------------------------------------------------------ health
def provider_cards(payload: Any) -> list[dict[str, Any]]:
    """Cards from /health/providers; values the backend does not report stay None."""

    items = payload.get("providers", []) if isinstance(payload, dict) else []
    out = []
    for p in items:
        if not isinstance(p, dict) or not p.get("provider"):
            continue
        out.append(
            {
                "name": str(p["provider"]),
                "state": str(p.get("state") or "unknown"),
                "latency_ms": _num(p.get("latency_ms")),  # backend does not report it yet
                "successes": _num(p.get("successes")),
                "failures": _num(p.get("failures")),
                "via": p.get("last_success_via"),
                "last_error": p.get("last_failure"),
                "last_success": short_time(p.get("last_success_at")),
                "last_failure_at": short_time(p.get("last_failure_at")),
            }
        )
    return out


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_SCALE = {"k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12, "cr": 1e7, "l": 1e5}


def _amount(text: str) -> float | None:
    m = _NUM.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group().replace(",", ""))
    except ValueError:
        return None
    unit = (text[m.end() :].strip().split() or [""])[0].lower().rstrip(".")
    return value * _SCALE.get(unit, 1.0)


def revenue_profit_series(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str, float]]:
    """[(period, series, amount)] for absolute revenue and net profit figures.

    Uses each figure's ``detail`` as its period. Returns [] unless both series
    have at least two periods, so callers fall back to the price chart.
    """

    out: dict[str, dict[str, float]] = {"Revenue": {}, "Net Profit": {}}
    for r in rows:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label", "")).lower()
        if any(w in label for w in ("growth", "margin", "ratio", "per share", "%")):
            continue
        if label in ("revenue", "total revenue", "revenues"):
            key = "Revenue"
        elif label in ("net income", "net profit", "profit after tax"):
            key = "Net Profit"
        else:
            continue
        period = str(r.get("detail") or "").strip()
        amount = _amount(str(r.get("value") or ""))
        if period and amount is not None:
            out[key][period] = amount
    if min(len(v) for v in out.values()) < 2:
        return []
    return [(per, k, v) for k, d in out.items() for per, v in sorted(d.items())]


#: The five research agents of a comprehensive run: (step kind, name, what it does).
RESEARCH_AGENTS: tuple[tuple[str, str, str], ...] = (
    (
        "market_intelligence",
        "Market agent",
        "Latest share price, 1-year returns, moving average and volatility.",
    ),
    (
        "financial_intelligence",
        "Financial agent",
        "Revenue, profit, margins and cash flow from filings (SEC / Yahoo India).",
    ),
    (
        "news_event_intelligence",
        "News agent",
        "Recent headlines and events (GDELT, RSS, Finnhub when a key is set).",
    ),
    (
        "industry_intelligence",
        "Industry agent",
        "Sector, peers and industry context for the company.",
    ),
    (
        "regulatory_intelligence",
        "Regulatory agent",
        "Filings and regulatory actions (SEC EDGAR, SEBI).",
    ),
)


def agent_states(steps: list[dict[str, Any]]) -> dict[str, str]:
    """Agent step kind -> latest state.

    Before a run starts every agent is ``waiting`` (for Start); once a run has
    steps, agents without a step yet are ``queued``.
    """

    default = "queued" if steps else "waiting"
    out = {kind: default for kind, _, _ in RESEARCH_AGENTS}
    for step in steps:
        kind = str(step.get("kind") or "")
        if kind in out:
            out[kind] = str(step.get("state") or default)
    return out


def pending_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in steps if s.get("state") == "pending_approval"]


def model_options(payload: Any) -> list[dict[str, Any]]:
    """Provider /models JSON -> [{id, label, free}] with free models first.

    ``free`` is fail-closed: only ids ending in ``:free`` (OpenRouter's free
    variants) count as free, whatever the advertised price; anything else is
    treated as a paid model that needs explicit opt-in.
    """

    rows = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        mid = row.get("id") if isinstance(row, dict) else row
        if not isinstance(mid, str) or not mid.strip():
            continue
        free = mid.endswith(":free")
        price = ""
        pricing = row.get("pricing") if isinstance(row, dict) else None
        if not free and isinstance(pricing, dict):
            try:
                per_m = float(pricing.get("prompt") or 0) * 1_000_000
                out_m = float(pricing.get("completion") or 0) * 1_000_000
                price = f" · ${per_m:.2f} in / ${out_m:.2f} out per 1M tokens"
            except (TypeError, ValueError):
                price = ""
        label = f"{mid} · FREE" if free else f"{mid} · PAID{price}"
        out.append({"id": mid, "label": label, "free": free})
    out.sort(key=lambda m: (not m["free"], m["id"]))
    return out


def analyst_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """News items that carry Finnhub analyst consensus / earnings-date evidence."""

    return [
        r
        for r in rows
        if str(r.get("title") or "").startswith(("Analyst consensus:", "Next earnings report"))
    ]


STEP_MODE_HELP = (
    "**What is this?** Step-by-step mode runs the same research, but **pauses after each "
    "stage** (Plan → Research → Verify → Report) and waits for you to **Approve, Reject or "
    "Retry** it, with live logs. Use it to inspect what every stage produced, or to demo "
    "the approval workflow. The company comes **prefilled from the Research page** if you "
    "searched one; otherwise type it."
)


def step_mode_company(company: object) -> str:
    """Prefill for the step-by-step company field: the last Research company, else blank."""

    return company.strip() if isinstance(company, str) else ""


def latest_run_logs(runs_payload: Any, get_logs: Any, limit: int = 12) -> list[str]:
    """Format the newest control run's logs; accepts dict ``{"runs": [...]}`` or a bare list."""

    runs = runs_payload.get("runs", []) if isinstance(runs_payload, dict) else runs_payload
    if not (isinstance(runs, list) and runs and isinstance(runs[-1], dict)):
        return []
    lg = get_logs(runs[-1].get("run_id")) or {}
    items = lg.get("logs", []) if isinstance(lg, dict) else lg
    return [
        f"{x.get('level', 'info'):5} {x.get('message')}"
        for x in (items if isinstance(items, list) else [])[-limit:]
        if isinstance(x, dict)
    ]


def _report_path(workflow_id: str, base: Path | None = None) -> Path:
    safe_id = "".join(c for c in str(workflow_id) if c.isalnum() or c in "-_")[:80] or "unknown"
    return (base or _store("dashboard_reports")) / f"{safe_id}.json"


def save_report(workflow_id: str, report: dict[str, Any], base: Path | None = None) -> None:
    """Persist a finished report locally so it can be reopened after a restart."""

    path = _report_path(workflow_id, base)
    keep = {k: report.get(k) for k in ("raw", "markdown", "workflow", "workflow_id", "company")}
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keep, default=str), encoding="utf-8")


def load_saved_report(workflow_id: str, base: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(_report_path(workflow_id, base).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("raw") is not None else None


# ------------------------------------------------------------ section status
ALL_SECTIONS: tuple[str, ...] = ("market", "financial", "news", "industry", "regulatory")
SECTION_LABELS: dict[str, str] = {
    "market": "Market",
    "financial": "Financials",
    "news": "News",
    "industry": "Industry",
    "regulatory": "Regulatory",
}
OBJECTIVE_SECTIONS: dict[str, tuple[str, ...]] = {
    "company_overview": ("market", "financial", "news"),
    "market_analysis": ("market",),
    "financial_analysis": ("financial",),
    "news_and_events": ("news",),
    "industry_analysis": ("industry",),
    "regulatory_analysis": ("regulatory",),
    "comprehensive_equity_research": ALL_SECTIONS,
}
STATUS_HELP = (
    "Completed = every selected objective produced report content. "
    "Partial = at least one selected section is missing or its data source failed "
    "(the missing sections are listed). Status only counts the objectives you selected."
)


def sections_for(objectives: Iterable[str]) -> tuple[str, ...]:
    wanted: set[str] = set()
    for o in objectives:
        wanted |= set(OBJECTIVE_SECTIONS.get(str(o), ()))
    return tuple(s for s in ALL_SECTIONS if s in wanted) or ALL_SECTIONS


def research_args(picks: Iterable[str]) -> dict[str, Any]:
    """start_research kwargs for the selected objective cards (one or many)."""

    keys = [k for k in picks if k in OBJECTIVE_SECTIONS]
    if not keys:
        return {}
    if len(keys) == 1:
        return {"objective": keys[0], "sections": sections_for(keys)}
    return {
        "objective": "comprehensive_equity_research",
        "sections": sections_for(keys),
        "auto_approve": True,
    }


def _section_rows(findings: dict[str, Any], name: str) -> list[Any]:
    rows: list[Any] = []
    for suffix in ("_figures", "_items"):
        value = findings.get(name + suffix)
        if isinstance(value, list):
            rows.extend(value)
    return rows


def research_status(
    findings: dict[str, Any], sections: Iterable[str] | None = None
) -> tuple[str, list[str]]:
    """('Completed'|'Partial', missing section labels) relative to the selected sections."""

    wanted = [
        s for s in (sections or findings.get("sections") or ALL_SECTIONS) if s in SECTION_LABELS
    ]
    missing = [SECTION_LABELS[s] for s in wanted if not _section_rows(findings, s)]
    return ("Partial" if missing else "Completed"), missing
