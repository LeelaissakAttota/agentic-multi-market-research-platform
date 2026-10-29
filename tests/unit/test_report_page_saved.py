"""Research Report page: a completed report shows even in a fresh session (regression)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from amrp.dashboard.views import list_saved_reports, load_saved_report, save_report

RAW = {
    "findings": {
        "sections": ["industry"],
        "industry": {"status": "ok"},
        "industry_figures": [{"label": "Sector", "value": "Technology"}],
    }
}


def _save(base: Path, wid: str, company: str, status: str) -> None:
    save_report(
        wid,
        {
            "raw": json.dumps(RAW),
            "markdown": f"# {company}",
            "workflow": {
                "objective": "industry_analysis",
                "status": "completed",
                "company_query": {"ticker": "TST"},
            },
            "workflow_id": wid,
            "company": company,
            "research_status": status,
            "missing_sections": [],
        },
        base,
    )


def test_saved_reports_listed_newest_first_with_status(tmp_path: Path) -> None:
    _save(tmp_path, "old-1", "Tata Consultancy Services", "partial")
    old = tmp_path / "old-1.json"
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    _save(tmp_path, "new-2", "Apple", "completed")
    rows = list_saved_reports(tmp_path)
    assert [r["workflow_id"] for r in rows] == ["new-2", "old-1"]
    assert rows[0]["company"] == "Apple" and rows[0]["research_status"] == "completed"
    assert load_saved_report("new-2", tmp_path)["research_status"] == "completed"
    assert list_saved_reports(tmp_path / "missing") == []


def test_report_page_shows_latest_saved_report_in_fresh_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from amrp.application import debate

    monkeypatch.setattr(debate, "ask_llm", lambda *a, **k: None)  # offline: no model calls
    monkeypatch.setenv("AMRP_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AMRP_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from amrp.control.secrets_store import default_config_dir

    base = default_config_dir("amrp") / "dashboard_reports"
    _save(base, "old-1", "Tata Consultancy Services", "partial")
    os.utime(base / "old-1.json", (time.time() - 3600, time.time() - 3600))
    _save(base, "new-2", "Apple", "completed")

    app = str(Path(__file__).resolve().parents[2] / "dashboard" / "app.py")
    at = AppTest.from_file(app, default_timeout=30)
    at.session_state["nav"] = "Research Report"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(m.value for m in at.markdown)
    assert "No report" not in " ".join(i.value for i in at.info)
    assert "Apple" in text  # latest report opens by default
    pick = at.selectbox[0]
    assert pick.value == "new-2" and len(pick.options) == 2
    pick.set_value("old-1").run()
    assert not at.exception
    assert "Tata Consultancy Services" in " ".join(m.value for m in at.markdown)
