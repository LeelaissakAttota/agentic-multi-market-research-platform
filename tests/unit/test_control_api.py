"""Equity Research supervised-control API: gates, lifecycle, risk auto-approval, masked keys."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from financial_intelligence.config.settings import Settings
from financial_intelligence.control.api import build_control_app
from financial_intelligence.control.risk import RiskLevel, classify_step
from financial_intelligence.control.runtime import AuditLog, ControlRun, DecisionKind
from financial_intelligence.control.service import EquityResearchControl
from tests.support.asgi_client import TestClient

TOKEN = "t" * 32
APPLE = {"q": "Apple", "ticker": "AAPL", "country": "US", "objective": "company_overview"}
H = {"Authorization": f"Bearer {TOKEN}", "X-Operator": "tester"}


@pytest.fixture()
def ctl(tmp_path: Path) -> tuple[TestClient, EquityResearchControl]:
    control = EquityResearchControl(
        Settings(_env_file=None, APP_ENV="test", LOG_LEVEL="WARNING"), config_dir=tmp_path
    )
    app = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="amrp")
    return TestClient(app), control


def _wait(client: TestClient, run_id: str, pred: Any, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/control/runs/{run_id}", headers=H).json()
        if pred(run):
            return dict(run)
        time.sleep(0.05)
    raise AssertionError(f"timeout: {run}")


def _pending(run: dict[str, Any]) -> bool:
    return bool(run["pending_approvals"])


def _done(run: dict[str, Any]) -> bool:
    return run["status"] in ("completed", "failed", "stopped")


# --- risk classification --------------------------------------------------------


def test_classification_low_and_high() -> None:
    for kind in (
        "company_resolution",
        "market_intelligence",
        "financial_intelligence",
        "news_event_intelligence",
        "regulatory_intelligence",
        "render_report",
    ):
        assert classify_step(kind) is RiskLevel.LOW
    for kind in ("send_message", "delete", "publish", "spend", "key_change", "modify"):
        assert classify_step(kind) is RiskLevel.HIGH


def test_unknown_kind_defaults_high() -> None:
    assert classify_step("something_new") is RiskLevel.HIGH
    assert classify_step("") is RiskLevel.HIGH
    assert classify_step(None) is RiskLevel.HIGH


def _gate_in_thread(run: ControlRun, kind: str) -> dict[str, Any]:
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(r=run.gate(kind, kind, {})), daemon=True)
    thread.start()
    box["thread"] = thread
    return box


def test_high_risk_never_auto_approved(tmp_path: Path) -> None:
    run = ControlRun(
        "r",
        {},
        settings={"approval_mode": True, "auto_approve_low_risk": True},
        audit=AuditLog(tmp_path / "a.jsonl"),
    )
    box = _gate_in_thread(run, "publish")
    time.sleep(0.3)
    assert box["thread"].is_alive()  # blocked
    assert run.steps[0].state.value == "pending_approval"
    run.approve(run.steps[0].step_id, by="human")
    box["thread"].join(2)
    assert run.steps[0].decision is DecisionKind.MANUAL
    # low-risk is auto-approved without blocking
    step, result = run.gate("market_intelligence", "t", {})
    assert result.approved and step.decision is DecisionKind.AUTO


def test_toggle_off_blocks_everything(tmp_path: Path) -> None:
    run = ControlRun(
        "r",
        {},
        settings={"approval_mode": True, "auto_approve_low_risk": False},
        audit=AuditLog(tmp_path / "a.jsonl"),
    )
    box = _gate_in_thread(run, "market_intelligence")
    time.sleep(0.3)
    assert box["thread"].is_alive()
    run.reject(run.steps[0].step_id, by="human", note="no")
    box["thread"].join(2)
    assert box["r"][1].approved is False


# --- API ------------------------------------------------------------------------


def test_requires_bearer_token(ctl: tuple[TestClient, EquityResearchControl]) -> None:
    client, _ = ctl
    assert client.get("/control/health").status_code == 200
    assert client.get("/control/runs").status_code == 401
    assert client.get("/control/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_short_token_rejected(ctl: tuple[TestClient, EquityResearchControl]) -> None:
    _, control = ctl
    with pytest.raises(ValueError):
        build_control_app(control.manager, control.secrets, admin_token="short", title="x")


def test_approval_off_runs_unchanged(ctl: tuple[TestClient, EquityResearchControl]) -> None:
    client, _ = ctl
    run = client.post("/control/runs", json={"params": APPLE}, headers=H).json()
    final = _wait(client, run["run_id"], _done)
    assert final["status"] == "completed", final
    assert final["result"]["status"] in ("succeeded", "partial"), final["result"]["status"]
    assert all(s["decision"] == "not_required" for s in final["steps"])


def _approve_all(
    client: TestClient, run_id: str, *, reject_first_task: bool = False, override: str | None = None
) -> dict[str, Any]:
    rejected = overridden = False
    while True:
        run = _wait(client, run_id, lambda r: _pending(r) or _done(r))
        if _done(run):
            return run
        sid = run["pending_approvals"][0]
        base = f"/control/runs/{run_id}/steps/{sid}"
        if reject_first_task and not rejected:
            assert (
                client.post(base + "/reject", json={"note": "skip"}, headers=H).status_code == 200
            )
            rejected = True
        elif override and not overridden:
            r = client.post(
                base + "/approve", json={"inputs": {"description": override}}, headers=H
            )
            assert r.json()["overridden"] is True
            overridden = True
        else:
            assert client.post(base + "/approve", json={}, headers=H).status_code == 200


def test_gates_block_and_approve_reject_override(
    ctl: tuple[TestClient, EquityResearchControl],
) -> None:
    client, _ = ctl
    assert client.put("/control/settings", json={"approval_mode": True}, headers=H).json()[
        "approval_mode"
    ]
    run = client.post("/control/runs", json={"params": APPLE}, headers=H).json()
    first = _wait(client, run["run_id"], _pending)
    assert first["status"] == "waiting_approval"
    assert first["steps"][0]["state"] == "pending_approval"
    time.sleep(0.3)  # still blocked: nothing executed
    assert len(client.get(f"/control/runs/{run['run_id']}/steps", headers=H).json()) == 1
    final = _approve_all(
        client,
        run["run_id"],
        reject_first_task=True,
        override="Resolve Apple Inc (operator override)",
    )
    steps = final["steps"]
    assert any(s["state"] == "rejected" for s in steps)
    assert any(
        s["overridden"] and s["inputs"]["description"] == "Resolve Apple Inc (operator override)"
        for s in steps
    )
    feed = client.get("/control/approvals", params={"run_id": run["run_id"]}, headers=H).json()
    assert {e["decision"] for e in feed} >= {"manual", "rejected"}
    assert all(e["by"] == "tester" for e in feed)
    # retry a finished research step with overridden input
    task = next(s for s in steps if s["state"] == "succeeded")
    retried = client.post(
        f"/control/runs/{run['run_id']}/steps/{task['step_id']}/retry",
        json={"inputs": {"description": "retry description"}},
        headers=H,
    ).json()
    assert (
        retried["state"] == "succeeded" and retried["inputs"]["description"] == "retry description"
    )


def test_auto_approve_low_risk_runs_without_human(
    ctl: tuple[TestClient, EquityResearchControl],
) -> None:
    client, _ = ctl
    client.put(
        "/control/settings", json={"approval_mode": True, "auto_approve_low_risk": True}, headers=H
    )
    run = client.post("/control/runs", json={"params": APPLE}, headers=H).json()
    final = _wait(client, run["run_id"], _done)
    assert final["status"] == "completed"
    assert all(s["decision"] == "auto" for s in final["steps"])


def test_pause_resume_stop(ctl: tuple[TestClient, EquityResearchControl]) -> None:
    client, _ = ctl
    client.put("/control/settings", json={"approval_mode": True}, headers=H)
    run = client.post("/control/runs", json={"params": APPLE}, headers=H).json()
    rid = run["run_id"]
    _wait(client, rid, _pending)
    assert client.post(f"/control/runs/{rid}/pause", headers=H).status_code == 200
    assert client.post(f"/control/runs/{rid}/resume", headers=H).status_code == 200
    assert client.post(f"/control/runs/{rid}/stop", headers=H).status_code == 200
    final = _wait(client, rid, _done)
    assert final["status"] == "stopped"
    logs = client.get(f"/control/runs/{rid}/logs", headers=H).json()["logs"]
    assert any("stop requested" in x["message"] for x in logs)


def test_keys_masked_write_only_and_reloaded(
    ctl: tuple[TestClient, EquityResearchControl], tmp_path: Path
) -> None:
    client, control = ctl
    secret = "sk-or-v1-abcdefghijklmnop1234"
    resp = client.put("/control/keys/OPENROUTER_API_KEY", json={"value": secret}, headers=H)
    assert (
        resp.status_code == 200 and secret not in resp.text and resp.json()["masked"] == "****1234"
    )
    listing = client.get("/control/keys", headers=H)
    assert secret not in listing.text
    assert listing.json()["keys"]["OPENROUTER_API_KEY"] == {
        "configured": True,
        "masked": "****1234",
    }
    assert (
        client.put("/control/keys/NOT_ALLOWED", json={"value": "x"}, headers=H).status_code == 404
    )
    path = tmp_path / "secrets.json"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # reload without restart: next run's settings see the new key
    assert control.settings_with_keys().openrouter_api_key is not None
    assert control.settings_with_keys().openrouter_api_key.get_secret_value() == secret  # type: ignore[union-attr]
    audit = client.get("/control/audit", headers=H).text
    assert secret not in audit and "key_changed" in audit
