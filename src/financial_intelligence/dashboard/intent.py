"""Turn one free-text question into (company phrase candidates, research objective)."""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Keyword stems -> objective. Order matters only for the explanation text.
_OBJECTIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "market_analysis": ("price", "stock", "share price", "market", "return", "volume", "trading"),
    "financial_analysis": (
        "revenue",
        "growth",
        "margin",
        "profit",
        "earning",
        "financ",
        "cash flow",
        "debt",
        "balance sheet",
        "fundamental",
    ),
    "news_and_events": ("news", "event", "announce", "headline", "latest", "demerger", "merger"),
    "industry_analysis": ("industry", "competitor", "peer", "sector", "rival"),
    "regulatory_analysis": ("regulat", "sebi", "sec ", "lawsuit", "litigation", "risk", "penalt"),
}
_COMPREHENSIVE_WORDS = ("comprehensive", "full research", "deep dive", "everything", "complete")

_STOPWORD_TEXT = (
    "a an and are as at be by can could do does for from give has have how i in is it its "
    "me my of on or please risk risks s show tell than that the their them there these "
    "this to up us vs was what whats when where which who why will with would you your "
    "about after any holding growth growing still doing news latest price stock stocks "
    "share shares market financials financial revenue margin margins profit research "
    "comprehensive full analysis analyse analyze regulatory industry competitors peers "
    "key main current company compare demerger merger after before deep dive overview "
    "outlook performance quarter results "
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@dataclass(frozen=True)
class Intent:
    objective: str
    reason: str
    candidates: tuple[str, ...]


def detect_objective(text: str) -> tuple[str, str]:
    """Return (objective, why). Two or more topics (or an explicit ask) -> comprehensive."""

    low = f" {text.lower()} "
    if any(w in low for w in _COMPREHENSIVE_WORDS):
        return "comprehensive_equity_research", "you asked for comprehensive research"
    hits = [obj for obj, words in _OBJECTIVE_KEYWORDS.items() if any(w in low for w in words)]
    if len(hits) >= 2:
        topics = ", ".join(h.split("_")[0] for h in hits)
        return "comprehensive_equity_research", f"question covers several topics ({topics})"
    if hits:
        return hits[0], f"question is about {hits[0].split('_')[0]}"
    return "company_overview", "no specific topic found, so a general overview"


def company_candidates(text: str, max_words: int = 4, limit: int = 12) -> tuple[str, ...]:
    """Phrases that may name a company, most specific first (longest, earliest)."""

    words = re.findall(r"[A-Za-z0-9&.\-]+", text.replace("'s ", " ").replace("\u2019s ", " "))
    runs: list[list[str]] = [[]]
    for word in words:
        clean = word.strip(".-")
        if not clean or clean.lower() in _STOPWORDS:
            runs.append([])
        else:
            runs[-1].append(clean)
    out: list[str] = []
    for run in (r for r in runs if r):
        for size in range(min(max_words, len(run)), 0, -1):
            for start in range(len(run) - size + 1):
                phrase = " ".join(run[start : start + size])
                if phrase.lower() not in (p.lower() for p in out):
                    out.append(phrase)
    return tuple(out[:limit])


def parse_intent(text: str) -> Intent:
    objective, reason = detect_objective(text)
    return Intent(objective=objective, reason=reason, candidates=company_candidates(text))


EXAMPLE_PROMPTS: tuple[tuple[str, str], ...] = (
    ("Apple", "How is Apple's stock price doing?"),
    ("Reliance", "Is Reliance's growth holding up, and what are the risks?"),
    ("TCS", "What are the latest news and events for TCS?"),
    ("Tata Motors demerger", "Tata Motors latest news after the demerger"),
    ("Comprehensive research", "Comprehensive research on Apple"),
)
