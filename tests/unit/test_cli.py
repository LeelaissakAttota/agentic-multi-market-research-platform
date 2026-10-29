"""amrp CLI: stages, approval gate, checkpoint resume and export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from amrp.cli import build_parser, run_research
from tests.unit.deliberation_fixtures import FINDINGS


class FakeApi:
    def __init__(self, approval: str = "pending", fail_execute: int = 0) -> None:
        self.calls: list[str] = []
        self.approval = approval
        self.fail_execute = fail_execute

    def get(self, path: str, **params: Any) -> Any:
        raise AssertionError("unused")

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        self.calls.append(path + (f"?{params['format']}" if params else ""))
        if path == "/research/workflows":
            assert body["ticker"] == "AAPL" and body["objective"]
            return {"workflow_id": "w-1", "approval_status": self.approval}
        if path.endswith("/execute") and self.fail_execute:
            self.fail_execute -= 1
            raise RuntimeError("boom")
        if path.endswith("/report"):
            if params["format"] == "json":
                return {"stored_report": {"content": json.dumps({"findings": FINDINGS})}}
            return {"stored_report": {"content": "# AAPL\n\nBody [F1].\n"}}
        return {}


def fake_agents(
    findings: dict[str, Any], md: str, ticker: str, as_of: str | None, wid: str
) -> dict[str, Any]:
    from amrp.application.deliberation import append_to_report, deliberate

    d = deliberate(findings, ticker, as_of=as_of)
    return {
        "markdown": append_to_report(md, d),
        "rating": d.rating.rating.value,
        "confidence": d.rating.confidence,
    }


def _args(tmp_path: Path, *extra: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(
        [
            "research",
            "--ticker",
            "aapl",
            "--format",
            "md",
            "--out",
            str(tmp_path / "out"),
            "--state-dir",
            str(tmp_path / "state"),
            *extra,
        ]
    )


NOW = lambda: datetime(2026, 10, 29, tzinfo=UTC)  # noqa: E731


def test_parser_defaults() -> None:
    a = build_parser().parse_args(["research", "--ticker", "TCS.NS", "--market", "in"])
    assert (a.format, a.as_of, a.approve, a.resume) == ("pdf", None, False, False)


def test_pending_approval_stops_without_flag(tmp_path: Path) -> None:
    api = FakeApi()
    assert run_research(_args(tmp_path), api, deliberate_fn=fake_agents, now=NOW) == 2
    assert not any("/approval" in c for c in api.calls)
    api2 = FakeApi()
    assert (
        run_research(
            _args(tmp_path, "--resume", "--approve"), api2, deliberate_fn=fake_agents, now=NOW
        )
        == 0
    )
    assert api2.calls[0] == "/research/workflows/w-1/approval"  # resumed, not re-created


def test_full_run_writes_report_with_agent_sections(tmp_path: Path) -> None:
    rc = run_research(_args(tmp_path, "--approve"), FakeApi(), deliberate_fn=fake_agents, now=NOW)
    assert rc == 0
    files = list((tmp_path / "out").iterdir())
    assert len(files) == 1 and files[0].suffix == ".md"
    text = files[0].read_text(encoding="utf-8")
    assert "## Rating" in text and "## Debate" in text and "## Risk" in text


def test_resume_after_failure_skips_finished_stages(tmp_path: Path) -> None:
    api = FakeApi(approval="approved", fail_execute=1)
    with pytest.raises(RuntimeError):
        run_research(_args(tmp_path), api, deliberate_fn=fake_agents, now=NOW)
    api2 = FakeApi(approval="approved")
    assert run_research(_args(tmp_path, "--resume"), api2, deliberate_fn=fake_agents, now=NOW) == 0
    assert "/research/workflows" not in api2.calls
    assert api2.calls[0].endswith("/execute")
    api3 = FakeApi()
    assert run_research(_args(tmp_path, "--resume"), api3, deliberate_fn=fake_agents) == 0
    assert api3.calls == []  # already exported


def test_as_of_is_passed_to_agents(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def spy(f, md, t, as_of, w):  # type: ignore[no-untyped-def]
        seen.append(as_of)
        return fake_agents(f, md, t, as_of, w)

    run_research(
        _args(tmp_path, "--approve", "--as-of", "2026-10-28"), FakeApi(), deliberate_fn=spy, now=NOW
    )
    assert seen == ["2026-10-28"]
    assert (tmp_path / "state" / "AAPL-2026-10-28.json").exists()
