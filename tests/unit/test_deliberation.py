"""Risk panel, final rating and the full deliberation pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

from amrp.application.debate import evidence_from_findings, run_debate
from amrp.application.deliberation import (
    Rating,
    deliberate,
    final_rating,
    rating_for,
    risk_flags,
    run_risk_panel,
)
from tests.unit.deliberation_fixtures import FINDINGS, FakeRouter


def test_rating_thresholds_and_direction() -> None:
    assert rating_for(0.7) is Rating.STRONG_BUY and rating_for(0.2) is Rating.BUY
    assert rating_for(0.0) is Rating.HOLD and rating_for(-0.3) is Rating.SELL
    assert rating_for(-0.9) is Rating.STRONG_SELL
    assert Rating.BUY.direction == 1 and Rating.HOLD.direction == 0
    assert Rating.STRONG_SELL.direction == -1


def test_risk_flags_from_evidence() -> None:
    flags = risk_flags(evidence_from_findings(FINDINGS))
    assert any(f.startswith("Leverage") and "[F7]" in f for f in flags)
    assert any(f.startswith("Liquidity") for f in flags)
    assert "Coverage: no financial statement evidence" in risk_flags([])


def test_risk_views_are_ordered_by_appetite() -> None:
    items = evidence_from_findings(FINDINGS)
    panel = run_risk_panel(run_debate(items, "Apple"), items)
    s = {v.stance: v.score for v in panel.views}
    assert s["aggressive"] > s["neutral"] > s["conservative"]
    assert panel.execution_risk.startswith("LOW")


def test_llm_risk_view_is_clamped() -> None:
    items = evidence_from_findings(FINDINGS)
    debate = run_debate(items, "Apple")
    base = {v.stance: v.score for v in run_risk_panel(debate, items).views}
    quick = FakeRouter('{"score": -1, "comment": "too risky"}', "garbage", None)
    panel = run_risk_panel(debate, items, quick=quick, quick_label="q")
    agg = panel.views[0]
    assert agg.by == "q" and agg.comment == "too risky"
    assert abs(agg.score - (base["aggressive"] - 0.3)) < 1e-6 or agg.score == -1
    assert agg.score >= base["aggressive"] - 0.3 - 1e-9
    assert panel.views[1].by == "deterministic"


def test_confidence_responds_to_history_and_fixture_data() -> None:
    items = evidence_from_findings(FINDINGS)
    debate = run_debate(items, "Apple")
    panel = run_risk_panel(debate, items)
    base = final_rating(debate, panel, items)
    good = final_rating(debate, panel, items, past_accuracy=0.9)
    bad = final_rating(debate, panel, items, past_accuracy=0.1)
    assert bad.confidence < base.confidence < good.confidence
    assert 0.05 <= bad.confidence <= 0.95


def test_deliberate_end_to_end_deterministic() -> None:
    fixed = datetime(2026, 10, 29, tzinfo=UTC)
    d = deliberate(FINDINGS, "Apple", rounds=1, now=lambda: fixed)
    assert d.generated_at.startswith("2026-10-29")
    assert d.rating.rating in set(Rating)
    assert d.models == {"analysts (quick)": "deterministic", "judge (deep)": "deterministic"}
    assert d.to_dict()["rating"]["confidence"] == d.rating.confidence
    again = deliberate(FINDINGS, "Apple", rounds=1, now=lambda: fixed)
    assert again == d  # deterministic path is reproducible


def test_deliberate_as_of_excludes_future_news() -> None:
    d = deliberate(FINDINGS, "Apple", as_of="2026-10-28")
    cited = {t for a in d.debate.arguments for p in a.points for t in p.tags}
    assert "N3" not in cited and "N1" not in cited
    assert d.as_of == "2026-10-28"
