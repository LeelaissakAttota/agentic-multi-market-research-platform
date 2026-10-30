"""v9 dashboard layout rules: no overflow, renamed nav, step-mode help, AMRP service name."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dashboard"))

import theme  # type: ignore[import-not-found]  # noqa: E402

from amrp.config import settings  # noqa: E402
from amrp.dashboard.views import (  # noqa: E402
    STEP_MODE_HELP,
    step_mode_company,
)

APP = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
CSS = (ROOT / "dashboard" / "assets" / "amrp.css").read_text(encoding="utf-8")


def test_agent_grid_wraps_and_keeps_pill_inside_card() -> None:
    html = theme.agent_grid([("Market <agent>", '<span class="pill">Waiting</span>', "x")])
    assert html.startswith('<div class="agent-grid">')
    assert "Market &lt;agent&gt;" in html
    assert '<div class="agent-s"><span class="pill">Waiting</span></div>' in html
    assert "grid-template-columns:repeat(auto-fit,minmax(160px,1fr))" in CSS


def test_search_box_is_above_suggested_companies() -> None:
    body = APP.split("def research_page", 1)[1]
    assert body.index('search_box("research"') < body.index("suggested_companies(args")


def test_reports_nav_renamed() -> None:
    assert '"All Research Reports": ("reports", reports_page)' in APP
    assert '    "Reports": (' not in APP


def test_step_mode_help_and_prefill() -> None:
    for word in ("Plan", "Research", "Verify", "Report", "prefilled"):
        assert word in STEP_MODE_HELP
    assert step_mode_company("  Apple ") == "Apple"
    assert step_mode_company(None) == ""


def test_stat_cards_identical_height_and_radios_wrap() -> None:
    assert ".stat{height:96px" in CSS
    assert "flex-wrap:wrap!important" in CSS
    assert ".st-key-fx_pair,.st-key-cm_market" in CSS
    assert 'class="stat' in theme.stat_card("Total", "1", "", "blue", "x")


def test_instrument_header_has_no_ticker_text() -> None:
    body = APP.split("def instrument_header", 1)[1].split("def forex_page", 1)[0]
    assert "esc(symbol)} · as of" not in body
    assert 'key="fx_news", height=NEWS_HEIGHT' in APP
    assert 'key="cr_news", height=NEWS_HEIGHT' in APP


def test_service_name_is_amrp() -> None:
    assert settings.SERVICE_NAME == "amrp"
