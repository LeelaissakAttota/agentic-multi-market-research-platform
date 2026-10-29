"""Risk panel (aggressive / conservative / neutral), final rating and report sections.

Builds on :mod:`amrp.application.debate`: the judge score is adjusted by three risk
views anchored on deterministic risk flags, then mapped to Strong Buy .. Strong Sell
with a confidence score. Research only - never an order or advice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from amrp.application.debate import (
    DISCLAIMER,
    Debate,
    EvidenceItem,
    ask_llm,
    by_label,
    evidence_from_findings,
    filter_evidence,
    parse_json_object,
    pct_value,
    ratio_value,
    run_debate,
)
from amrp.application.ports import LlmRouterPort
from amrp.control.risk import RiskLevel, classify_step


# ----------------------------------------------------------------- risk panel
class Rating(StrEnum):
    STRONG_BUY = "Strong Buy"
    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"
    STRONG_SELL = "Strong Sell"

    @property
    def direction(self) -> int:
        return {"Strong Buy": 1, "Buy": 1, "Hold": 0, "Sell": -1, "Strong Sell": -1}[self.value]


def rating_for(score: float) -> Rating:
    if score >= 0.5:
        return Rating.STRONG_BUY
    if score >= 0.15:
        return Rating.BUY
    if score > -0.15:
        return Rating.HOLD
    if score > -0.5:
        return Rating.SELL
    return Rating.STRONG_SELL


@dataclass(frozen=True, slots=True)
class RiskView:
    stance: str
    score: float
    comment: str
    by: str


@dataclass(frozen=True, slots=True)
class RiskPanel:
    flags: tuple[str, ...]
    views: tuple[RiskView, ...]
    execution_risk: str


def risk_flags(items: Sequence[EvidenceItem]) -> list[str]:
    f = by_label(items)
    flags: list[str] = []
    de = f.get("debt to equity")
    if de and (v := ratio_value(de.value)) is not None and v >= 1.0:
        flags.append(f"Leverage: debt to equity {de.value} [{de.tag}]")
    cr = f.get("current ratio")
    if cr and (v := ratio_value(cr.value)) is not None and v < 1.0:
        flags.append(f"Liquidity: current ratio {cr.value} [{cr.tag}]")
    ret = f.get("simple return")
    if ret and (v := pct_value(ret.value)) is not None and abs(v) >= 5:
        flags.append(f"Volatility: recent return {ret.value} [{ret.tag}]")
    fixture = [i for i in items if i.origin == "fixture"]
    if fixture:
        flags.append(f"Data quality: {len(fixture)} item(s) are offline sample data")
    if not any(i.kind == "financial" for i in items):
        flags.append("Coverage: no financial statement evidence")
    if not any(i.kind == "news" for i in items):
        flags.append("Coverage: no recent news evidence")
    return flags


_STANCE = {
    "aggressive": ("favours upside capture and tolerates volatility", 0.15, 0.4),
    "neutral": ("balances upside against the risk flags", 0.0, 1.0),
    "conservative": ("prioritises capital preservation and penalises every risk flag", -0.1, 1.6),
}


def run_risk_panel(
    debate: Debate,
    items: Sequence[EvidenceItem],
    *,
    company: str = "",
    quick: LlmRouterPort | None = None,
    quick_label: str = "llm",
) -> RiskPanel:
    flags = risk_flags(items)
    penalty = 0.08 * len(flags)
    views: list[RiskView] = []
    for stance, (desc, bias, pen_mult) in _STANCE.items():
        anchor = max(-1.0, min(1.0, debate.verdict.score + bias - penalty * pen_mult))
        view = RiskView(
            stance,
            round(anchor, 3),
            f"The {stance} analyst {desc}: judge score {debate.verdict.score:+.2f}, "
            f"{len(flags)} risk flag(s) -> {anchor:+.2f}.",
            "deterministic",
        )
        text = ask_llm(
            quick,
            f"You are the {stance.upper()} risk analyst; you {desc}. Research only. JSON only.",
            f"Company: {company}\nJudge verdict: {debate.verdict.leaning} "
            f"({debate.verdict.score:+.2f}) - {debate.verdict.rationale}\nRisk flags:\n"
            + ("\n".join(flags) or "(none)")
            + '\n\nReturn JSON: {"score": <number -1..1>, "comment": "<1-2 sentences>"}',
            300,
        )
        data = parse_json_object(text or "")
        if data:
            try:
                raw = float(data.get("score", anchor))
            except (TypeError, ValueError):
                raw = anchor
            score = max(anchor - 0.3, min(anchor + 0.3, raw))  # bounded around the anchor
            view = RiskView(
                stance, round(score, 3), str(data.get("comment") or view.comment)[:400], quick_label
            )
        views.append(view)
    level = classify_step("analysis")
    execution = (
        "LOW - read-only analysis; no orders, messages or writes (control/risk.py)"
        if level is RiskLevel.LOW
        else "HIGH - requires human approval"
    )
    return RiskPanel(tuple(flags), tuple(views), execution)


# ----------------------------------------------------------------- final rating
@dataclass(frozen=True, slots=True)
class FinalRating:
    rating: Rating
    score: float
    confidence: float
    rationale: str


def final_rating(
    debate: Debate,
    risk: RiskPanel,
    items: Sequence[EvidenceItem],
    past_accuracy: float | None = None,
) -> FinalRating:
    scores = [v.score for v in risk.views] or [debate.verdict.score]
    score = round(sum(scores) / len(scores), 3)
    kinds = {i.kind for i in items} & {"market", "financial", "news", "regulatory"}
    coverage = len(kinds) / 4
    spread = max(scores) - min(scores) if scores else 0.0
    agreement = max(0.0, 1.0 - spread)
    conf = 0.2 + 0.3 * coverage + 0.25 * debate.citations.ratio + 0.2 * agreement
    if past_accuracy is not None:
        conf *= 1.0 + 0.5 * (past_accuracy - 0.5)
    if any(f.startswith("Data quality") for f in risk.flags):
        conf *= 0.8
    conf = round(max(0.05, min(0.95, conf)), 2)
    rating = rating_for(score)
    why = (
        f"Average of the three risk views {score:+.2f} -> {rating.value}. Judge leaned "
        f"{debate.verdict.leaning} ({debate.verdict.score:+.2f}); "
        f"{debate.citations.verified_points}/{debate.citations.total_points} debate points "
        f"verified; evidence coverage {len(kinds)}/4 categories."
    )
    if past_accuracy is not None:
        why += f" Past-call accuracy for this company {past_accuracy:.0%} adjusted confidence."
    return FinalRating(rating, score, conf, why)


# ----------------------------------------------------------------- orchestration
@dataclass(frozen=True, slots=True)
class Deliberation:
    company: str
    generated_at: str
    as_of: str | None
    debate: Debate
    risk: RiskPanel
    rating: FinalRating
    models: dict[str, str] = field(default_factory=dict)
    lessons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def deliberate(
    findings: Mapping[str, Any],
    company: str,
    *,
    rounds: int = 1,
    quick: LlmRouterPort | None = None,
    deep: LlmRouterPort | None = None,
    quick_label: str = "deterministic",
    deep_label: str = "deterministic",
    lessons: Sequence[str] = (),
    past_accuracy: float | None = None,
    as_of: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Deliberation:
    """Run debate -> judge -> risk panel -> rating on a report's findings."""

    items = evidence_from_findings(findings)
    if as_of:
        items = filter_evidence(items, as_of)
    debate = run_debate(
        items,
        company,
        rounds=rounds,
        quick=quick,
        deep=deep,
        quick_label=quick_label,
        deep_label=deep_label,
        lessons=lessons,
    )
    risk = run_risk_panel(debate, items, company=company, quick=quick, quick_label=quick_label)
    rating = final_rating(debate, risk, items, past_accuracy)
    return Deliberation(
        company,
        now().isoformat(timespec="seconds"),
        as_of,
        debate,
        risk,
        rating,
        {
            "analysts (quick)": quick_label if quick else "deterministic",
            "judge (deep)": deep_label if deep else "deterministic",
        },
        tuple(lessons),
    )


# ----------------------------------------------------------------- report markdown
def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def deliberation_markdown(d: Deliberation) -> str:
    """Debate, Risk and Rating sections appended to the report Markdown (PDF/HTML render it)."""

    r = d.rating
    lines = [
        "## Rating",
        "",
        f"**{r.rating.value}** - confidence {r.confidence:.0%} (score {r.score:+.2f})",
        "",
        r.rationale,
        "",
        f"> {DISCLAIMER}",
        "",
        "## Debate",
        "",
        "Models: " + "; ".join(f"{k}: {v}" for k, v in d.models.items()),
        "",
    ]
    for a in d.debate.arguments:
        lines += [f"### {a.side.capitalize()} - round {a.round} ({a.by})", ""]
        for p in a.points:
            tags = "".join(f"[{t}]" for t in p.tags)
            mark = "" if p.verified else f" _(unverified: {'; '.join(p.issues)})_"
            lines.append(f"- {p.text} {tags}{mark}".rstrip())
        lines.append("")
    c = d.debate.citations
    lines += [
        f"**Citation check:** {c.verified_points}/{c.total_points} points verified"
        + (f"; unknown tags: {', '.join(c.unknown_tags)}" if c.unknown_tags else "")
        + (f"; {c.numeric_mismatches} number mismatch(es)" if c.numeric_mismatches else ""),
        "",
        f"**Judge ({d.debate.verdict.by}):** leans {d.debate.verdict.leaning} "
        f"({d.debate.verdict.score:+.2f}). {d.debate.verdict.rationale}",
        "",
        "## Risk",
        "",
        "| View | Score | Comment |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {v.stance.capitalize()} | {v.score:+.2f} | {_cell(v.comment)} |" for v in d.risk.views
    ]
    lines += ["", "**Risk flags:**", ""]
    lines += [f"- {f}" for f in d.risk.flags] or ["- none"]
    lines += ["", f"**Execution risk:** {d.risk.execution_risk}", ""]
    if d.lessons:
        lines += ["### Lessons from past calls", ""] + [f"- {x}" for x in d.lessons] + [""]
    if d.as_of:
        lines += [f"_Point-in-time analysis as of {d.as_of}: later evidence was excluded._", ""]
    return "\n".join(lines)


def append_to_report(markdown: str, d: Deliberation) -> str:
    """Insert (or replace) the deliberation sections in a report's Markdown."""

    marker = "\n\n<!-- amrp:deliberation -->\n"
    base = markdown.split(marker)[0].rstrip()
    return base + marker + deliberation_markdown(d)
