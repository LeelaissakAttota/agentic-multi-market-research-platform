"""Forex / crypto / commodity intraday helpers: session high-low, instruments, news, evidence.

Real data only (Yahoo Finance public chart + RSS, Google News RSS). Every fetch
returns ``None`` / ``[]`` on failure so pages can show "not available".

Sessions (UTC, overlapping): Asia/Tokyo 00-09, London 07-16, New York 12-21.
The same windows are used for 24/7 crypto. 21-24 UTC is "Off-session".
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from financial_intelligence.dashboard import commodity as cm
from financial_intelligence.dashboard import market_live as ml

IST = timedelta(hours=5, minutes=30)
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("Asia/Tokyo", 0, 9),
    ("London", 7, 16),
    ("New York", 12, 21),
)
OFF_SESSION = "Off-session"


@dataclass(frozen=True, slots=True)
class IBar:
    ts: datetime  # UTC bar start
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class Extreme:
    day: str  # UTC date YYYY-MM-DD
    high: float
    high_ts: datetime
    high_session: str
    low: float
    low_ts: datetime
    low_session: str


@dataclass(frozen=True, slots=True)
class Instrument:
    name: str
    symbol: str
    keyword: str  # for filtering headlines
    queries: tuple[str, ...]
    decimals: int = 4


FOREX: dict[str, Instrument] = {
    "USD/INR": Instrument("USD/INR", "USDINR=X", "rupee", ("rupee dollar", "USD INR"), 3),
    "EUR/USD": Instrument("EUR/USD", "EURUSD=X", "euro", ("EUR/USD", "euro dollar")),
    "GBP/USD": Instrument("GBP/USD", "GBPUSD=X", "sterling", ("GBP/USD", "sterling pound")),
    "USD/JPY": Instrument("USD/JPY", "JPY=X", "yen", ("USD/JPY", "yen dollar"), 3),
    "EUR/INR": Instrument("EUR/INR", "EURINR=X", "rupee", ("euro rupee", "EUR INR"), 3),
    "GBP/INR": Instrument("GBP/INR", "GBPINR=X", "rupee", ("pound rupee", "GBP INR"), 3),
    "AUD/USD": Instrument("AUD/USD", "AUDUSD=X", "aussie", ("AUD/USD", "Australian dollar")),
    "USD/CHF": Instrument("USD/CHF", "CHF=X", "franc", ("USD/CHF", "Swiss franc")),
}
CRYPTO: dict[str, Instrument] = {
    "BTC": Instrument("Bitcoin", "BTC-USD", "bitcoin", ("bitcoin price",), 2),
    "ETH": Instrument("Ethereum", "ETH-USD", "ether", ("ethereum price",), 2),
    "SOL": Instrument("Solana", "SOL-USD", "solana", ("solana crypto",), 2),
    "BNB": Instrument("BNB", "BNB-USD", "bnb", ("BNB binance coin",), 2),
    "XRP": Instrument("XRP", "XRP-USD", "xrp", ("XRP ripple",), 4),
    "DOGE": Instrument("Dogecoin", "DOGE-USD", "doge", ("dogecoin",), 5),
}


def sessions_at(ts: datetime) -> str:
    """Session label(s) for a UTC timestamp; overlaps join with ' + '."""

    h = ts.astimezone(UTC).hour
    names = [n for n, a, b in SESSIONS if a <= h < b]
    return " + ".join(names) if names else OFF_SESSION


def ist(ts: datetime) -> str:
    return (ts.astimezone(UTC) + IST).strftime("%d %b %H:%M IST")


def parse_intraday(payload: dict[str, Any]) -> list[IBar]:
    try:
        res = payload["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        stamps = res.get("timestamp") or []
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for i, t in enumerate(stamps):
        try:
            vals = [q[k][i] for k in ("open", "high", "low", "close")]
        except (KeyError, IndexError, TypeError):
            continue
        if any(v is None for v in vals):
            continue
        o, h, lo, c = (float(v) for v in vals)
        out.append(IBar(datetime.fromtimestamp(int(t), UTC), o, h, lo, c))
    return out


def fetch_intraday(
    symbol: str, range_: str, interval: str, client: httpx.Client | None = None
) -> list[IBar]:
    c = ml._client(client)
    try:
        r = c.get(
            ml.CHART_URL.format(symbol=symbol), params={"range": range_, "interval": interval}
        )
        r.raise_for_status()
        return parse_intraday(r.json())
    except (httpx.HTTPError, ValueError):
        return []


def by_day(bars: list[IBar]) -> OrderedDict[str, list[IBar]]:
    days: OrderedDict[str, list[IBar]] = OrderedDict()
    for b in sorted(bars, key=lambda x: x.ts):
        days.setdefault(b.ts.strftime("%Y-%m-%d"), []).append(b)
    return days


def extremes(bars: list[IBar]) -> Extreme | None:
    if not bars:
        return None
    hi = max(bars, key=lambda b: b.high)
    lo = min(bars, key=lambda b: b.low)
    return Extreme(
        hi.ts.strftime("%Y-%m-%d"),
        hi.high,
        hi.ts,
        sessions_at(hi.ts),
        lo.low,
        lo.ts,
        sessions_at(lo.ts),
    )


def daily_extremes(bars: list[IBar], days: int = 7) -> list[Extreme]:
    out = [e for e in (extremes(v) for v in by_day(bars).values()) if e]
    return out[-days:]


def last_day(bars: list[IBar]) -> list[IBar]:
    days = by_day(bars)
    return list(days.values())[-1] if days else []


def scale(bars: list[IBar], k: float) -> list[IBar]:
    return [IBar(b.ts, b.open * k, b.high * k, b.low * k, b.close * k) for b in bars]


def usd_inr(client: httpx.Client | None = None) -> float | None:
    d = ml.fetch_daily(cm.FX_SYMBOL, client=client)
    return d.bars[-1].close if d and d.bars else None


def news_for(inst: Instrument, n: int = 8, client: httpx.Client | None = None) -> list[ml.NewsItem]:
    """Yahoo headlines (keyword-filtered) + Google News RSS, newest first, deduped."""

    c = ml._client(client)
    items = [x for x in ml.fetch_news(inst.symbol, client=c) if inst.keyword in x.title.lower()]
    for q in inst.queries:
        params = {"q": f"{q} when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        try:
            r = c.get(cm.GOOGLE_RSS, params=params)
            r.raise_for_status()
            items += cm.parse_google_rss(r.text)
        except httpx.HTTPError:
            continue
    seen: set[str] = set()
    out = []
    for it in sorted(items, key=lambda x: x.published, reverse=True):
        key = it.title.lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out[:n]


def build_evidence(
    name: str,
    unit: str,
    day: list[IBar],
    week: list[Extreme],
    news: list[ml.NewsItem],
    decimals: int = 4,
) -> list[dict[str, str]]:
    """Evidence items ``{tag, text, source}`` from real data only (P = price, N = news)."""

    f = f",.{decimals}f"
    ev: list[dict[str, str]] = []
    if day:
        e = extremes(day)
        first, last = day[0], day[-1]
        chg = (last.close / first.open - 1) if first.open else 0.0
        if e:
            ev.append(
                {
                    "tag": "P1",
                    "source": "Yahoo Finance intraday",
                    "text": (
                        f"{name} last {format(last.close, f)} {unit} at {ist(last.ts)}; "
                        f"day open {format(first.open, f)}, change {chg:+.2%}; day high "
                        f"{format(e.high, f)} ({e.high_session}, {ist(e.high_ts)}), day low "
                        f"{format(e.low, f)} ({e.low_session}, {ist(e.low_ts)})."
                    ),
                }
            )
    for i, w in enumerate(week, start=2):
        ev.append(
            {
                "tag": f"P{i}",
                "source": "Yahoo Finance hourly",
                "text": (
                    f"{w.day}: high {format(w.high, f)} ({w.high_session}), "
                    f"low {format(w.low, f)} ({w.low_session})."
                ),
            }
        )
    for i, n in enumerate(news, start=1):
        ev.append(
            {
                "tag": f"N{i}",
                "source": f"{n.source} {n.published}",
                "text": n.title,
                "url": n.link,
            }
        )
    return ev
