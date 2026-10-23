"""v8: research layout, selected-card state, agent status text, forex news filter, names."""

from __future__ import annotations

from pathlib import Path

import pytest

from financial_intelligence.control import server
from financial_intelligence.dashboard.markets import forex_relevant
from financial_intelligence.dashboard.views import agent_states

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "dashboard" / "app.py"
CSS = ROOT / "dashboard" / "assets" / "aerp.css"


@pytest.mark.parametrize(
    ("pair", "title", "ok"),
    [
        ("USD/INR", "Rupee weakens against the dollar as oil rises", True),
        ("USD/INR", "Sri Lanka rupee closes firmer against the dollar", False),
        ("USD/INR", "Pakistan rupee hits record low vs dollar", False),
        ("USD/INR", "Rupee steady; RBI seen intervening", True),
        ("USD/INR", "Sensex closes higher on IT gains", False),
        ("USD/INR", "Wipro stock: Jefferies raises target from INR 150.00", False),
        ("USD/INR", "ETH to INR: Ethereum Price in Indian Rupee", False),
        ("EUR/USD", "EUR/USD climbs after ECB minutes", True),
        ("EUR/USD", "Euro firms as dollar slips on Fed bets", True),
        ("EUR/USD", "Canadian dollar gains as oil jumps", False),
        ("USD/JPY", "Yen slides past 150 per dollar", True),
        ("GBP/INR", "Pound to rupee rate today", True),
    ],
)
def test_forex_relevance(pair: str, title: str, ok: bool) -> None:
    assert forex_relevant(pair, title) is ok


def test_agent_status_waiting_for_start_then_queued() -> None:
    assert set(agent_states([]).values()) == {"waiting"}
    states = agent_states([{"kind": "market_intelligence", "state": "running"}])
    assert states["market_intelligence"] == "running"
    assert states["financial_intelligence"] == "queued"
    src = APP.read_text(encoding="utf-8")
    assert '"waiting": ("Waiting for Start"' in src
    assert '"queued": ("Queued"' in src


def test_names_are_amrp() -> None:
    assert server.SERVICE_NAME == "amrp"
    assert 'page_title="AMRP' in APP.read_text(encoding="utf-8")


def test_no_deprecated_use_container_width() -> None:
    assert "use_container_width" not in APP.read_text(encoding="utf-8")


def test_selected_card_and_layout_css() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert '[class*="st-key-objc_"][class*="__sel"]' in css
    assert "overflow-wrap:anywhere" in css
    assert "st-key-sugbox_" in css and ".comp-gap" in css


def test_objective_rows_split_four_then_rest() -> None:
    import ast

    tree = ast.parse(APP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "objective_rows")
    ns: dict[str, object] = {}
    exec(compile(ast.Module([fn], []), "objective_rows", "exec"), ns)
    rows = ns["objective_rows"](list("abcdefg"))  # type: ignore[operator]
    assert rows == [list("abcd"), list("efg")]


def test_research_page_renders_suggested_boxes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from financial_intelligence.dashboard import market_live

    monkeypatch.setattr(market_live, "fetch_daily", lambda *a, **k: None)
    monkeypatch.setattr(market_live, "trending_us", lambda *a, **k: [])
    monkeypatch.setattr(market_live, "most_active_india", lambda *a, **k: [])
    monkeypatch.setattr(market_live, "news_one_per_company", lambda *a, **k: [])
    monkeypatch.setenv("AERP_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AERP_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EQUITY_RESEARCH_CONTROL_TOKEN", "")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.session_state["nav"] = "Research"
    at.session_state["objective_pick"] = "comprehensive_equity_research"
    at.run()
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert md.index("Suggested Companies") < md.index("Research Objective")
    assert "US &ndash; Top trending" in md and "India &ndash; Top trending" in md
