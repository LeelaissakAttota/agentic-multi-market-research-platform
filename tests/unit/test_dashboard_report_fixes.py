"""Regression tests: provider-health run logs, saved reports, client timeouts."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from financial_intelligence.dashboard.client import ApiError, JsonClient
from financial_intelligence.dashboard.views import (
    latest_run_logs,
    load_saved_report,
    save_report,
)


def test_latest_run_logs_accepts_list_payload() -> None:
    logs = latest_run_logs(
        [{"run_id": "a"}, {"run_id": "b"}],
        lambda rid: [{"level": "info", "message": f"run {rid}"}],
    )
    assert logs == ["info  run b"]


def test_latest_run_logs_accepts_dict_payload_and_garbage() -> None:
    assert latest_run_logs(
        {"runs": [{"run_id": "x"}]}, lambda rid: {"logs": [{"message": "m"}]}
    ) == ["info  m"]
    assert latest_run_logs({}, lambda rid: None) == []
    assert latest_run_logs(["oops"], lambda rid: None) == []
    assert latest_run_logs([{"run_id": "x"}], lambda rid: None) == []


def test_saved_report_round_trip(tmp_path: Path) -> None:
    save_report("wf-1", {"raw": {"a": 1}, "markdown": "# R", "workflow_id": "wf-1"}, base=tmp_path)
    got = load_saved_report("wf-1", base=tmp_path)
    assert got is not None and got["markdown"] == "# R" and got["raw"] == {"a": 1}
    assert load_saved_report("missing", base=tmp_path) is None


def test_saved_report_id_is_sanitised(tmp_path: Path) -> None:
    save_report("../../evil", {"raw": {}, "markdown": ""}, base=tmp_path)
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_client_timeout_becomes_api_error() -> None:
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            threading.Event().wait(2)

        def log_message(self, *a: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    client = JsonClient(f"http://127.0.0.1:{srv.server_address[1]}", timeout=0.3)
    with pytest.raises(ApiError) as err:
        client.post("/x")
    assert "within" in err.value.message
    srv.server_close()
    assert socket  # keep import for clarity


def test_app_wires_fixes() -> None:
    src = (Path(__file__).resolve().parents[2] / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "auto_approve=True" in src  # comprehensive "Open the finished report"
    assert "load_saved_report(workflow_id)" in src  # reopen after restart
    assert "RUNNER.post(" in src  # bounded research run
    assert "latest_run_logs(" in src
