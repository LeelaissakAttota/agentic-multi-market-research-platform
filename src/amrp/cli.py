"""``amrp`` command line: run a research workflow end to end and export the report.

Example::

    amrp research --ticker AAPL --market us --format pdf --approve
    amrp research --ticker TCS.NS --market in --as-of 2026-06-30 --resume

Each stage (create -> approve -> execute -> report -> agents -> export) is checkpointed in
``.amrp/cli/<ticker>-<as-of>.json`` so ``--resume`` continues where a failed or
interrupted run stopped. The API (``http://127.0.0.1:8000``) must be running.
Approval stays human-controlled: without ``--approve`` the CLI stops at a pending approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

STAGES = ("created", "approved", "executed", "reported", "agents", "exported")


class Api(Protocol):
    def get(self, path: str, **params: Any) -> Any: ...

    def post(self, path: str, body: Any = None, **params: Any) -> Any: ...


@dataclass
class Checkpoint:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, resume: bool) -> Checkpoint:
        if resume and path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path, {"stages": []})

    def done(self, stage: str) -> bool:
        return stage in self.data.get("stages", [])

    def mark(self, stage: str, **values: Any) -> None:
        self.data.update(values)
        if stage not in self.data["stages"]:
            self.data["stages"].append(stage)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def _say(msg: str) -> None:
    print(f"[amrp] {msg}", flush=True)


def run_research(
    args: argparse.Namespace,
    api: Api,
    *,
    deliberate_fn: Callable[[dict[str, Any], str, str, str | None, str], dict[str, Any]]
    | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    ticker = args.ticker.strip().upper()
    as_of = args.as_of or ""
    cp = Checkpoint.load(Path(args.state_dir) / f"{ticker}-{as_of or 'latest'}.json", args.resume)
    if cp.done("exported") and args.resume:
        _say(f"already finished: {cp.data.get('output')}")
        return 0

    if not cp.done("created"):
        body = {
            "objective": args.objective,
            "objective_text": f"Research {ticker}",
            "q": ticker,
            "ticker": ticker,
            "exchange": "NSE" if args.market == "in" and ticker.endswith(".NS") else None,
        }
        wf = api.post("/research/workflows", body)
        cp.mark("created", workflow_id=wf["workflow_id"], approval=wf.get("approval_status"))
        _say(f"workflow {wf['workflow_id']} created")
    wid = cp.data["workflow_id"]

    if not cp.done("approved"):
        status = cp.data.get("approval")
        if status == "pending":
            if not args.approve:
                _say(
                    "approval pending - approve it in the dashboard (Workflow Approvals) "
                    "or pass --approve, then re-run with --resume"
                )
                return 2
            api.post(
                f"/research/workflows/{wid}/approval",
                {"decision": "approved", "note": "approved via amrp CLI --approve"},
            )
        cp.mark("approved")

    if not cp.done("executed"):
        _say("executing research plan (this can take a few minutes)...")
        api.post(f"/research/workflows/{wid}/execute")
        cp.mark("executed")

    if not cp.done("reported"):
        rep = api.post(f"/research/workflows/{wid}/report", format="json")
        md = api.post(f"/research/workflows/{wid}/report", format="markdown")
        raw = json.loads(rep["stored_report"]["content"])
        cp.mark(
            "reported", findings=raw.get("findings") or {}, markdown=md["stored_report"]["content"]
        )
    findings: dict[str, Any] = cp.data["findings"]
    markdown: str = cp.data["markdown"]

    if not cp.done("agents"):
        fn = deliberate_fn or _default_deliberate
        result = fn(findings, markdown, ticker, as_of or None, wid)
        cp.mark(
            "agents",
            markdown=result["markdown"],
            rating=result["rating"],
            confidence=result["confidence"],
        )
        _say(f"agents: {result['rating']} (confidence {result['confidence']:.0%})")
    markdown = cp.data["markdown"]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    from amrp.dashboard.report_export import (
        markdown_to_html,
        markdown_to_pdf,
        pdf_available,
        report_filename,
    )

    title = f"{ticker} research"
    fmt = args.format
    if fmt == "pdf" and not pdf_available():
        _say("fpdf2 not installed - writing HTML instead (pip install fpdf2)")
        fmt = "html"
    target = out_dir / report_filename(ticker, fmt, now())
    if fmt == "pdf":
        target.write_bytes(markdown_to_pdf(markdown, title))
    elif fmt == "html":
        target.write_text(markdown_to_html(markdown, title), encoding="utf-8")
    else:
        target.write_text(markdown, encoding="utf-8")
    cp.mark("exported", output=str(target))
    _say(f"report written: {target}")
    return 0


def _default_deliberate(
    findings: dict[str, Any], markdown: str, ticker: str, as_of: str | None, workflow_id: str
) -> dict[str, Any]:
    """Agents with the configured LLM tiers (deterministic without keys) + outcome memory."""

    from amrp.application.deliberation import append_to_report, deliberate
    from amrp.application.outcome_memory import OutcomeMemory, parse_price
    from amrp.control.secrets_store import default_config_dir
    from amrp.infrastructure.llm.providers import build_tier_routers, load_config

    cfg = load_config()
    routers = build_tier_routers(cfg) if cfg.llm_enabled else None
    mem = OutcomeMemory(default_config_dir("amrp") / "outcome_memory.sqlite3")
    lessons, acc = mem.lessons(ticker)
    d = deliberate(
        findings,
        ticker,
        rounds=cfg.debate_rounds,
        quick=routers.quick if routers else None,
        deep=routers.deep if routers else None,
        quick_label=routers.quick_label if routers else "deterministic",
        deep_label=routers.deep_label if routers else "deterministic",
        lessons=lessons,
        past_accuracy=acc,
        as_of=as_of,
    )
    if not as_of:
        for row in findings.get("market_figures") or []:
            if str(row.get("label", "")).lower() == "last close":
                price, cur = parse_price(str(row.get("value", "")))
                day = str(row.get("detail", ""))[-10:]
                if price:
                    mem.record(
                        ticker=ticker,
                        company=ticker,
                        rating=d.rating.rating.value,
                        score=d.rating.score,
                        confidence=d.rating.confidence,
                        price_at=price,
                        currency=cur,
                        as_of=day if day[:2] == "20" else d.generated_at[:10],
                        workflow_id=workflow_id,
                    )
                break
    mem.close()
    return {
        "markdown": append_to_report(markdown, d),
        "rating": d.rating.rating.value,
        "confidence": d.rating.confidence,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="amrp", description="AMRP research command line")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("research", help="run one research workflow and export the report")
    r.add_argument("--ticker", required=True, help="e.g. AAPL, TCS.NS, RELIANCE.NS")
    r.add_argument("--market", choices=["us", "in"], default="us")
    r.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="YYYY-MM-DD point-in-time date for the agents (no look-ahead)",
    )
    r.add_argument("--format", choices=["pdf", "html", "md"], default="pdf")
    r.add_argument("--out", default="reports", help="output folder (default: reports)")
    r.add_argument("--objective", default="comprehensive_equity_research")
    r.add_argument("--api", default="http://127.0.0.1:8000")
    r.add_argument(
        "--approve",
        action="store_true",
        help="approve the workflow if it is pending (you are the approver)",
    )
    r.add_argument("--resume", action="store_true", help="continue from the last checkpoint")
    r.add_argument("--state-dir", dest="state_dir", default=".amrp/cli")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from amrp.dashboard.client import ApiError, JsonClient

    api = JsonClient(args.api, timeout=600, operator="cli")
    try:
        return run_research(args, api)
    except ApiError as exc:
        _say(f"API error: {exc} - fix it and re-run with --resume")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
