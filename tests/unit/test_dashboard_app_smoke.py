"""Every dashboard page renders without an exception even when the APIs are down."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[2] / "dashboard" / "app.py")
PAGES = [
    "Home",
    "Research",
    "Workflow Approvals",
    "Research Report",
    "Reports",
    "Watchlist",
    "Provider Health",
    "Settings",
]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_api_down(
    page: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AERP_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AERP_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EQUITY_RESEARCH_CONTROL_TOKEN", "")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["nav"] = page
    at.run()
    assert not at.exception, [e.value for e in at.exception]
