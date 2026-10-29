"""Comprehensive run modes: per-run Auto/Human approval, Skip, and the final summary."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from amrp.config.settings import Settings
from amrp.control.api import build_control_app
from amrp.control.service import EquityResearchControl
from amrp.dashboard.views import run_summary
from tests.support.asgi_client import TestClient

TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}", "X-Operator": "tester"}
APPLE = {
    "q": "Apple",
    "ticker": "AAPL",
    "country": "US",
    "objective": "comprehensive_equity_research",
}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    control = EquityResearchControl(
        Settings(_env_file=None, APP_ENV="test", LOG_LEVEL="WARNING"), config_dir=tmp_path
    )
    control.manager.settings.update(approval_mode=True, auto_approve_low_risk=False)
    app = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="amrp")
    return TestClient(app)


def _wait(c: TestClient, run_id: str, pred: Any, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = c.get(f"/control/runs/{run_id}", headers=H).json()
        if pred(run):
            return dict(run)
        time.sleep(0.05)
    raise AssertionError(f"timeout: {run}")


def _done(run: dict[str, Any]) -> bool:
    return run["status"] in ("completed", "failed", "stopped")


def test_auto_mode_runs_without_prompts(client: TestClient) -> None:
    run = client.post(
        "/control/runs", json={"params": {**APPLE, "approval_mode": False}}, headers=H
    ).json()
    assert run["approval_mode"] is False
    final = _wait(client, run["run_id"], _done)
    assert final["status"] == "completed"
    assert final["steps"] and all(s["decision"] == "not_required" for s in final["steps"])


def test_human_mode_skip_continues_and_marks_skipped(client: TestClient) -> None:
    run = client.post(
        "/control/runs", json={"params": {**APPLE, "approval_mode": True}}, headers=H
    ).json()
    rid, skipped_kind = run["run_id"], None
    while True:
        cur = _wait(client, rid, lambda r: r["pending_approvals"] or _done(r))
        if _done(cur):
            break
        sid = cur["pending_approvals"][0]
        step = next(s for s in cur["steps"] if s["step_id"] == sid)
        if skipped_kind is None:
            skipped_kind = step["kind"]
            r = client.post(f"/control/runs/{rid}/steps/{sid}/skip", json={}, headers=H)
            assert r.status_code == 200 and r.json()["state"] == "skipped"
        else:
            client.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H)
    final = _wait(client, rid, _done)
    assert final["status"] == "completed"  # skip does not stop the run
    states = {s["kind"]: s["state"] for s in final["steps"]}
    assert states[skipped_kind] == "skipped"
    assert sum(1 for v in states.values() if v == "succeeded") >= 3
    summary = run_summary(final["steps"], final["result"])
    assert summary["status"] == "Partial"
    assert [s["kind"] for s in summary["skipped"]] == [skipped_kind]
    assert summary["failed"] == []


def test_run_summary_failed_then_retried() -> None:
    steps = [
        {
            "step_id": "a",
            "kind": "industry_intelligence",
            "state": "failed",
            "error": "unavailable: no data",
            "inputs": {"task_id": "t1"},
        },
        {
            "step_id": "b",
            "kind": "market_intelligence",
            "state": "succeeded",
            "inputs": {"task_id": "t2"},
        },
    ]
    s = run_summary(steps, {"status": "partial"})
    assert s["status"] == "Partial"
    assert (
        s["failed"][0]["agent"] == "Industry agent"
        or s["failed"][0]["kind"] == "industry_intelligence"
    )
    assert "no data" in s["failed"][0]["reason"]
    steps.append(
        {
            "step_id": "c",
            "kind": "industry_intelligence",
            "state": "succeeded",
            "inputs": {"task_id": "t1"},
        }
    )
    assert run_summary(steps, {"status": "partial"}) == {
        "status": "Completed",
        "failed": [],
        "skipped": [],
    }
