"""Bull vs Bear debate, citation verification and judge (deterministic + fake LLM)."""

from __future__ import annotations

from amrp.application.debate import (
    derive_signals,
    evidence_from_findings,
    filter_evidence,
    run_debate,
    verify_point,
)
from tests.unit.deliberation_fixtures import FINDINGS, FakeRouter


def _items():  # type: ignore[no-untyped-def]
    return evidence_from_findings(FINDINGS)


def test_evidence_is_tagged_by_kind() -> None:
    items = _items()
    kinds = {i.tag: i.kind for i in items}
    assert kinds["M1"] == "market" and kinds["F1"] == "financial" and kinds["N3"] == "news"
    assert next(i for i in items if i.tag == "M1").date == "2026-10-28"


def test_signals_cover_both_sides() -> None:
    sig = derive_signals(_items())
    assert any(s.side == "bull" and "F1" in s.tags for s in sig)  # revenue growth 6.43%
    assert any(s.side == "bear" and "F6" in s.tags for s in sig)  # current ratio < 1
    assert any(s.side == "bull" and "N3" in s.tags for s in sig)  # analyst consensus Buy


def test_verify_point_rules() -> None:
    ev = {i.tag: i for i in _items()}
    assert verify_point("Revenue grew 6.43% [F1]", ev).verified
    bad_num = verify_point("Revenue grew 12.5% [F1]", ev)
    assert not bad_num.verified and "number" in bad_num.issues[0]
    assert not verify_point("Great brand [X9]", ev).verified
    assert verify_point("Margins are wide [F5][F2]", ev).tags == ("F5", "F2")
    assert "unknown tag F99" in verify_point("Something [F99]", ev).issues


def test_deterministic_debate_rounds_and_verdict() -> None:
    d = run_debate(_items(), "Apple", rounds=2)
    assert [(a.side, a.round) for a in d.arguments] == [
        ("bull", 1),
        ("bear", 1),
        ("bull", 2),
        ("bear", 2),
    ]
    assert d.arguments[2].points[0].text.startswith("Rebuttal")
    assert d.citations.verified_points == d.citations.total_points
    assert d.verdict.by == "deterministic" and d.verdict.leaning == "bull"
    assert -1 <= d.verdict.score <= 1


def test_llm_debate_flags_bad_citations_and_judge_is_guardrailed() -> None:
    quick = FakeRouter(
        "- Revenue growth of 6.43% shows demand [F1]\n- Made-up fact [Z1]\n- Margin 99% [F5]",
        None,  # bear call fails -> deterministic fallback for that side
    )
    deep = FakeRouter('{"leaning": "bull", "score": 5, "rationale": "Growth [F1]"}')
    d = run_debate(_items(), "Apple", quick=quick, deep=deep, quick_label="q", deep_label="d")
    bull, bear = d.arguments
    assert bull.by == "q" and bear.by == "deterministic"
    assert [p.verified for p in bull.points] == [True, False, False]
    assert d.citations.numeric_mismatches == 1
    anchor = run_debate(_items(), "Apple").verdict.score
    assert d.verdict.by == "d" and d.verdict.score <= anchor + 0.5 + 1e-9
    assert "[F1]" in quick.prompts[0] or "F1" in quick.prompts[0]


def test_bad_judge_json_falls_back() -> None:
    d = run_debate(_items(), "Apple", deep=FakeRouter("not json"))
    assert d.verdict.by == "deterministic"


def test_empty_evidence_is_balanced() -> None:
    d = run_debate([], "Nobody")
    assert d.verdict.leaning == "balanced" and d.verdict.score == 0


def test_filter_evidence_drops_future_items() -> None:
    kept = {i.tag for i in filter_evidence(_items(), "2026-10-28")}
    assert "M1" in kept and "N1" not in kept  # news dated 10-29 is after as-of
    assert "F1" in kept
    assert "F1" not in {i.tag for i in filter_evidence(_items(), "2025-06-30")}


def test_consensus_side_uses_the_headline_only() -> None:
    from amrp.application.debate import EvidenceItem

    def sig(title: str) -> list[str]:
        it = EvidenceItem("N9", "news", title, "", "", "2026-10-29", "live")
        return [s.side for s in derive_signals([it])]

    assert sig("Analyst consensus: Buy - 11 strong buy, 3 sell, 1 strong sell") == ["bull"]
    assert sig("Analyst consensus: Strong Sell - 9 sell") == ["bear"]
    assert sig("Analyst consensus: Hold - 2 buy, 2 sell") == []
