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
    "All Research Reports",
    "Watchlist",
    "Provider Health",
    "Settings",
    "Crypto Market",
]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_api_down(
    page: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from amrp.dashboard import market_live

    # Offline: the public market/news fetchers report "not available".
    monkeypatch.setattr(market_live, "fetch_daily", lambda *a, **k: None)
    monkeypatch.setattr(market_live, "trending_us", lambda *a, **k: [])
    monkeypatch.setattr(market_live, "most_active_india", lambda *a, **k: [])
    monkeypatch.setattr(market_live, "news_one_per_company", lambda *a, **k: [])
    monkeypatch.setenv("AMRP_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AMRP_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AMRP_CONTROL_TOKEN", "")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["nav"] = page
    at.run()
    assert not at.exception, [e.value for e in at.exception]


def test_crypto_page_has_no_inr_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMRP_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AMRP_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["nav"] = "Crypto Market"
    at.run()
    assert not at.exception
    labels = [r.label for r in at.radio]
    assert "Currency" not in labels
