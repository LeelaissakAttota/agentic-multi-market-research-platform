"""Bull vs Bear debate with citation verification and a judge, over finished research evidence.

Pipeline (ideas inspired by TradingAgents, arXiv 2412.20138; implementation is AMRP's own):

1. **Bull vs Bear debate** - two analyst agents argue from the report's tagged evidence
   (``[M1]`` market, ``[F1]`` financial, ``[N1]`` news, ``[R1]`` regulatory) for a
   configurable number of rounds. Every point must cite evidence tags.
2. **Citation verification** - unknown tags and numbers that do not appear in the cited
   evidence are flagged; unverified points do not count for the verdict.
3. **Judge** - a stronger model (deep tier) weighs the verified points.
4. **Risk panel** - aggressive / conservative / neutral views adjust the judge score
   using deterministic risk flags (leverage, liquidity, momentum, data quality).
5. **Final rating** - Strong Buy .. Strong Sell with a confidence score.

Every LLM step has a deterministic, rule-based fallback, so the whole pipeline works
with no API key ("LLM off"). Output is research context only - never an order or advice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from amrp.application.ports import LlmRouterPort
from amrp.domain.llm import ModelCallId, ModelCallStatus, ModelMessage, ModelRequest

DISCLAIMER = (
    "Research-only output generated from stored evidence by automated agents. It is not "
    "investment advice, not a recommendation to trade, and no order is ever placed."
)
PROMPT_VERSION = "deliberation-v1"
_TAG = re.compile(r"\[([MFNR]\d{1,2})\]")
_NUM = re.compile(r"(?<![A-Za-z\[])[-+]?\d[\d,]*\.?\d*")
_KINDS = {"M": "market", "F": "financial", "N": "news", "R": "regulatory"}


# ----------------------------------------------------------------- evidence
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    tag: str
    kind: str
    label: str
    value: str
    detail: str = ""
    date: str = ""
    origin: str = ""

    def line(self) -> str:
        extra = f" ({self.detail})" if self.detail else ""
        return f"[{self.tag}] {self.label}: {self.value}{extra}"


def evidence_from_findings(findings: Mapping[str, Any]) -> list[EvidenceItem]:
    """Flatten the report ``findings`` block into tagged evidence items."""

    out: list[EvidenceItem] = []
    for key in ("market_figures", "financial_figures"):
        for row in findings.get(key) or []:
            if isinstance(row, Mapping) and row.get("tag"):
                tag = str(row["tag"])
                out.append(
                    EvidenceItem(
                        tag,
                        _KINDS.get(tag[0], "other"),
                        str(row.get("label") or ""),
                        str(row.get("value") or ""),
                        str(row.get("detail") or ""),
                        _as_of_in(str(row.get("detail") or "")),
                    )
                )
    for key in ("news_items", "regulatory_items"):
        for row in findings.get(key) or []:
            if isinstance(row, Mapping) and row.get("tag"):
                tag = str(row["tag"])
                out.append(
                    EvidenceItem(
                        tag,
                        _KINDS.get(tag[0], "other"),
                        str(row.get("title") or ""),
                        str(row.get("detail") or "")[:240],
                        "",
                        str(row.get("date") or "")[:10],
                        str(row.get("data_origin") or ""),
                    )
                )
    return out


def filter_evidence(items: Sequence[EvidenceItem], as_of: str) -> list[EvidenceItem]:
    """Point-in-time view: drop evidence dated after ``as_of`` (YYYY-MM-DD).

    Dated items (prices, news) must be on or before ``as_of``. Fiscal-year figures
    (``FY2025``) are only known after the year closes, so FY >= the as-of year is dropped.
    """

    day = as_of[:10]
    out: list[EvidenceItem] = []
    for it in items:
        if it.date and it.date[:10] > day:
            continue
        fy = re.search(r"FY(\d{4})", it.detail)
        if fy and int(fy.group(1)) >= int(day[:4]):
            continue
        out.append(it)
    return out


def _as_of_in(detail: str) -> str:
    m = re.search(r"as of (\d{4}-\d{2}-\d{2})", detail)
    return m.group(1) if m else ""


def pct_value(value: str) -> float | None:
    m = re.search(r"([-+]?\d[\d,]*\.?\d*)\s*%", value)
    return float(m.group(1).replace(",", "")) if m else None


def ratio_value(value: str) -> float | None:
    m = re.search(r"([-+]?\d[\d,]*\.?\d*)\s*x\b", value)
    return float(m.group(1).replace(",", "")) if m else None


def _money(value: str) -> float | None:
    m = re.search(r"([-+]?\d[\d,]*\.?\d*)\s*([TBM]?)\b", value.split(" ", 1)[-1])
    if not m:
        return None
    scale = {"T": 1e12, "B": 1e9, "M": 1e6, "": 1.0}[m.group(2)]
    return float(m.group(1).replace(",", "")) * scale


# ----------------------------------------------------------------- deterministic signals
@dataclass(frozen=True, slots=True)
class Signal:
    side: str  # bull | bear
    weight: float
    text: str
    tags: tuple[str, ...]


def by_label(items: Sequence[EvidenceItem]) -> dict[str, EvidenceItem]:
    return {i.label.lower(): i for i in items if i.kind in ("market", "financial")}


def derive_signals(items: Sequence[EvidenceItem]) -> list[Signal]:
    """Rule-based bull/bear signals from tagged figures and analyst consensus."""

    s: list[Signal] = []
    f = by_label(items)

    def pct_rule(label: str, good: float, bad: float, weight: float, what: str) -> None:
        it = f.get(label)
        v = pct_value(it.value) if it else None
        if it is None or v is None:
            return
        if v >= good:
            s.append(Signal("bull", weight, f"{what} is strong at {it.value}", (it.tag,)))
        elif v <= bad:
            s.append(Signal("bear", weight, f"{what} is weak at {it.value}", (it.tag,)))

    pct_rule("revenue growth", 5.0, 0.0, 1.0, "Revenue growth")
    pct_rule("net income growth", 8.0, -5.0, 0.8, "Net income growth")
    pct_rule("net margin", 15.0, 3.0, 0.8, "Net margin")
    pct_rule("operating margin", 20.0, 5.0, 0.6, "Operating margin")
    pct_rule("free cash flow margin", 10.0, 0.0, 0.8, "Free cash flow margin")
    pct_rule("simple return", 2.0, -2.0, 0.5, "Recent price return")
    de = f.get("debt to equity")
    if de and (v := ratio_value(de.value)) is not None:
        if v >= 1.5:
            s.append(Signal("bear", 0.8, f"Leverage is high: debt to equity {de.value}", (de.tag,)))
        elif v <= 0.5:
            s.append(
                Signal("bull", 0.5, f"Balance sheet is light: debt to equity {de.value}", (de.tag,))
            )
    cr = f.get("current ratio")
    if cr and (v := ratio_value(cr.value)) is not None:
        if v < 1.0:
            s.append(
                Signal(
                    "bear", 0.5, f"Current ratio below 1 ({cr.value}) - tight liquidity", (cr.tag,)
                )
            )
        elif v >= 1.5:
            s.append(
                Signal("bull", 0.3, f"Comfortable liquidity: current ratio {cr.value}", (cr.tag,))
            )
    last, sma = f.get("last close"), f.get("sma (simple moving average)")
    if last and sma and (a := _money(last.value)) and (b := _money(sma.value)):
        if a > b * 1.01:
            s.append(
                Signal(
                    "bull",
                    0.4,
                    f"Price {last.value} trades above its SMA {sma.value}",
                    (last.tag, sma.tag),
                )
            )
        elif a < b * 0.99:
            s.append(
                Signal(
                    "bear",
                    0.4,
                    f"Price {last.value} trades below its SMA {sma.value}",
                    (last.tag, sma.tag),
                )
            )
    fcf = f.get("free cash flow")
    if fcf and fcf.value.strip().startswith("-"):
        s.append(Signal("bear", 0.6, f"Free cash flow is negative ({fcf.value})", (fcf.tag,)))
    for it in items:
        title = it.label
        if title.startswith("Analyst consensus:"):
            word = title.split(":", 1)[1].strip().split(" ")[0].lower()
            side = {"buy": "bull", "strong": "bull", "sell": "bear"}.get(word)
            if "strong sell" in title.lower():
                side = "bear"
            if side:
                s.append(Signal(side, 0.7, f"Third-party {title.split(' (')[0]}", (it.tag,)))
    return s


# ----------------------------------------------------------------- debate model
@dataclass(frozen=True, slots=True)
class Point:
    text: str
    tags: tuple[str, ...]
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Argument:
    side: str
    round: int
    points: tuple[Point, ...]
    by: str


@dataclass(frozen=True, slots=True)
class CitationCheck:
    total_points: int
    verified_points: int
    unknown_tags: tuple[str, ...]
    numeric_mismatches: int

    @property
    def ratio(self) -> float:
        return self.verified_points / self.total_points if self.total_points else 0.0


@dataclass(frozen=True, slots=True)
class Verdict:
    leaning: str  # bull | bear | balanced
    score: float  # -1 (bear) .. +1 (bull)
    rationale: str
    by: str


@dataclass(frozen=True, slots=True)
class Debate:
    arguments: tuple[Argument, ...]
    citations: CitationCheck
    verdict: Verdict


def verify_point(text: str, evidence: Mapping[str, EvidenceItem]) -> Point:
    """Tags must exist; numbers quoted in the point must appear in the cited evidence."""

    tags = tuple(dict.fromkeys(_TAG.findall(text)))
    clean = _TAG.sub("", text).strip(" -.;") or text
    issues: list[str] = []
    unknown = [t for t in tags if t not in evidence]
    if not tags:
        issues.append("no evidence cited")
    if unknown:
        issues.append("unknown tag " + ", ".join(unknown))
    cited = " ".join(
        f"{evidence[t].value} {evidence[t].detail} {evidence[t].label}"
        for t in tags
        if t in evidence
    )
    cited_nums = {_norm(n) for n in _NUM.findall(cited)}
    for n in _NUM.findall(clean):
        if len(n.strip("+-,.")) < 2 or re.fullmatch(r"(19|20)\d\d", n):
            continue  # skip tiny numbers and years
        if _norm(n) not in cited_nums:
            issues.append(f"number {n} not in cited evidence")
            break
    return Point(clean, tags, verified=not issues, issues=tuple(issues))


def _norm(n: str) -> str:
    try:
        return f"{float(n.replace(',', '').strip('+')):.2f}"
    except ValueError:
        return n


def ask_llm(router: LlmRouterPort | None, system: str, user: str, max_tokens: int) -> str | None:
    if router is None:
        return None
    try:
        res = router.complete(
            ModelRequest(
                call_id=ModelCallId.new(),
                prompt_version=PROMPT_VERSION,
                messages=(ModelMessage("system", system), ModelMessage("user", user[:30000])),
                max_output_tokens=max_tokens,
            )
        )
    except Exception:
        return None
    if res.status is not ModelCallStatus.SUCCEEDED or not res.content:
        return None
    return res.content


def _bullets(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    pts = [
        re.sub(r"^([-*\u2022]|\d+[.)])\s*", "", ln)
        for ln in lines
        if re.match(r"^([-*\u2022]|\d+[.)])\s+", ln)
    ]
    return [p for p in pts if len(p) > 8][:6]


_ROLE = {
    "bull": "You are the BULL analyst. Argue the strongest evidence-based case FOR the stock.",
    "bear": "You are the BEAR analyst. Argue the strongest evidence-based case AGAINST the stock.",
}


def _det_argument(
    side: str,
    rnd: int,
    signals: Sequence[Signal],
    other: Argument | None,
    ev: Mapping[str, EvidenceItem],
) -> Argument:
    mine = sorted((x for x in signals if x.side == side), key=lambda x: -x.weight)
    texts = [f"{x.text} {''.join(f'[{t}]' for t in x.tags)}" for x in mine]
    if not texts:
        texts = [f"No tagged evidence supports the {side} case; the report data is thin."]
    if rnd > 1 and other and other.points:
        target = other.points[0]
        tag = f"[{target.tags[0]}]" if target.tags else ""
        texts = [
            f"Rebuttal: the {other.side} point '{target.text[:80]}' is outweighed by the "
            f"{side} evidence above {tag}",
            *texts[:3],
        ]
    pts = tuple(verify_point(t, ev) for t in texts[:5])
    return Argument(side, rnd, pts, "deterministic")


def _llm_argument(
    router: LlmRouterPort | None,
    label: str,
    side: str,
    rnd: int,
    company: str,
    ev: Mapping[str, EvidenceItem],
    history: Sequence[Argument],
    lessons: Sequence[str],
) -> Argument | None:
    evidence = "\n".join(e.line() for e in ev.values())
    past = "\n".join(
        f"{a.side.upper()} round {a.round}: " + " | ".join(p.text for p in a.points)
        for a in history
    )
    lesson_txt = (
        ("\nLessons from past calls on this company:\n" + "\n".join(lessons)) if lessons else ""
    )
    user = (
        f"Company: {company}\nEvidence (cite tags exactly, e.g. [F1]):\n{evidence}\n"
        f"{lesson_txt}\nDebate so far:\n{past or '(none)'}\n\nRound {rnd}: write 3-5 bullet "
        "points ('- ...'). Each bullet MUST cite at least one evidence tag in square "
        "brackets and may only quote numbers that appear in the cited evidence. "
        + ("Rebut the other side's strongest point in your first bullet." if rnd > 1 else "")
    )
    text = ask_llm(router, _ROLE[side] + " Research only, no advice.", user, 700)
    if not text:
        return None
    pts = tuple(verify_point(b, ev) for b in _bullets(text))
    return Argument(side, rnd, pts, label) if pts else None


def _det_verdict(signals: Sequence[Signal]) -> Verdict:
    bull = sum(x.weight for x in signals if x.side == "bull")
    bear = sum(x.weight for x in signals if x.side == "bear")
    total = bull + bear
    score = 0.0 if total == 0 else (bull - bear) / total
    score = round(score * min(1.0, total / 3.0), 3)  # thin evidence -> closer to neutral
    leaning = "bull" if score > 0.15 else "bear" if score < -0.15 else "balanced"
    return Verdict(
        leaning,
        score,
        f"Weighted verified evidence: bull {bull:.1f} vs bear {bear:.1f}.",
        "deterministic",
    )


def parse_json_object(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _llm_verdict(
    router: LlmRouterPort | None,
    label: str,
    company: str,
    args: Sequence[Argument],
    anchor: Verdict,
) -> Verdict | None:
    body = "\n".join(
        f"{a.side.upper()} r{a.round}: "
        + " | ".join(p.text + "".join(f"[{t}]" for t in p.tags) for p in a.points if p.verified)
        for a in args
    )
    text = ask_llm(
        router,
        "You are an impartial JUDGE of an equity research debate. Use only the verified "
        "points given. Reply with JSON only.",
        f"Company: {company}\nVerified points:\n{body}\n\nReturn JSON: "
        '{"leaning": "bull|bear|balanced", "score": <number -1..1>, "rationale": "<2-3 '
        'sentences citing tags>"}',
        500,
    )
    data = parse_json_object(text or "")
    if not data:
        return None
    try:
        score = max(-1.0, min(1.0, float(data.get("score", 0))))
    except (TypeError, ValueError):
        return None
    # guardrail: the judge may not stray more than 0.5 from the evidence-weighted anchor
    score = round(max(anchor.score - 0.5, min(anchor.score + 0.5, score)), 3)
    leaning = "bull" if score > 0.15 else "bear" if score < -0.15 else "balanced"
    return Verdict(leaning, score, str(data.get("rationale") or "")[:800], label)


def run_debate(
    items: Sequence[EvidenceItem],
    company: str,
    *,
    rounds: int = 1,
    quick: LlmRouterPort | None = None,
    deep: LlmRouterPort | None = None,
    quick_label: str = "llm",
    deep_label: str = "llm",
    lessons: Sequence[str] = (),
) -> Debate:
    ev = {i.tag: i for i in items}
    signals = derive_signals(items)
    args: list[Argument] = []
    for rnd in range(1, max(1, min(3, rounds)) + 1):
        for side in ("bull", "bear"):
            other = next((a for a in reversed(args) if a.side != side), None)
            arg = _llm_argument(
                quick, quick_label, side, rnd, company, ev, args, lessons
            ) or _det_argument(side, rnd, signals, other, ev)
            args.append(arg)
    pts = [p for a in args for p in a.points]
    unknown = tuple(
        sorted({i.split(" ", 2)[-1] for p in pts for i in p.issues if i.startswith("unknown tag")})
    )
    check = CitationCheck(
        len(pts),
        sum(p.verified for p in pts),
        unknown,
        sum(any(i.startswith("number") for i in p.issues) for p in pts),
    )
    anchor = _det_verdict(signals)
    verdict = _llm_verdict(deep, deep_label, company, args, anchor) or anchor
    return Debate(tuple(args), check, verdict)
