"""Small public-market fetch helpers for the dashboard home and research pages.

Uses Yahoo Finance's public chart, trending and RSS endpoints over httpx (no API key).
Every function returns ``None`` / ``[]`` on any failure so callers can show
"not available"; nothing here ever substitutes sample values.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TRENDING_URL = "https://query1.finance.yahoo.com/v1/finance/trending/{region}"
RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
HEADERS = {"User-Agent": "Mozilla/5.0 (AERP dashboard)"}

US_INDICES = (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"))
IN_INDICES = (("^NSEI", "NIFTY 50"), ("^BSESN", "SENSEX"))
# Universe ranked by traded value when Yahoo has no India trending list.
IN_LARGE_CAPS = (
    "RELIANCE.NS",
    "TCS.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "BHARTIARTL.NS",
    "SBIN.NS",
    "ITC.NS",
    "LT.NS",
    "HINDUNILVR.NS",
    "AXISBANK.NS",
    "KOTAKBANK.NS",
    "TATAMOTORS.NS",
    "MARUTI.NS",
    "SUNPHARMA.NS",
)


#: Used for Top News only when the watchlist and trending list give too few symbols.
NEWS_FALLBACK = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA", "META")


@dataclass(frozen=True, slots=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Daily:
    symbol: str
    name: str
    currency: str
    bars: tuple[Bar, ...]
    as_of: str  # ISO UTC of the last market time

    @property
    def change(self) -> float | None:
        if len(self.bars) < 2 or not self.bars[0].close:
            return None
        return (self.bars[-1].close - self.bars[0].close) / self.bars[0].close


@dataclass(frozen=True, slots=True)
class NewsItem:
    symbol: str
    title: str
    link: str
    source: str
    published: str  # ISO UTC


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=8.0, headers=HEADERS, follow_redirects=True)


def parse_chart(payload: dict[str, Any], symbol: str, name: str = "") -> Daily | None:
    """Daily bars from a chart response; ``None`` unless at least one full bar is present."""

    try:
        res = payload["chart"]["result"][0]
        meta = res.get("meta") or {}
        quote = res["indicators"]["quote"][0]
        stamps = res.get("timestamp") or []
    except (KeyError, IndexError, TypeError):
        return None
    bars = []
    for i, ts in enumerate(stamps):
        try:
            vals = [quote[k][i] for k in ("open", "high", "low", "close")]
            vol = quote.get("volume", [0] * len(stamps))[i] or 0
        except (KeyError, IndexError, TypeError):
            continue
        if any(v is None for v in vals):
            continue
        day = datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")
        o, h, lo, cl = (float(v) for v in vals)
        bars.append(Bar(day, o, h, lo, cl, float(vol)))
    if not bars:
        return None
    t = meta.get("regularMarketTime") or stamps[-1]
    return Daily(
        symbol=symbol,
        name=name or str(meta.get("shortName") or meta.get("longName") or symbol),
        currency=str(meta.get("currency") or ""),
        bars=tuple(bars[-5:]),
        as_of=datetime.fromtimestamp(int(t), UTC).isoformat(timespec="minutes"),
    )


def fetch_daily(symbol: str, name: str = "", client: httpx.Client | None = None) -> Daily | None:
    c = _client(client)
    try:
        r = c.get(CHART_URL.format(symbol=symbol), params={"range": "5d", "interval": "1d"})
        r.raise_for_status()
        return parse_chart(r.json(), symbol, name)
    except (httpx.HTTPError, ValueError):
        return None


def parse_trending(payload: dict[str, Any]) -> list[str]:
    try:
        quotes = payload["finance"]["result"][0]["quotes"]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for q in quotes:
        sym = str((q or {}).get("symbol") or "")
        if sym and not sym.startswith("^") and "=" not in sym and "-" not in sym:
            out.append(sym)
    return out


def trending_us(n: int = 5, client: httpx.Client | None = None) -> list[Daily]:
    """Yahoo's US trending tickers (equities only), with 5-day bars."""

    c = _client(client)
    try:
        r = c.get(TRENDING_URL.format(region="US"), params={"count": 25})
        r.raise_for_status()
        syms = parse_trending(r.json())
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for s in syms:
        d = fetch_daily(s, client=c)
        if d:
            out.append(d)
        if len(out) == n:
            break
    return out


def rank_most_active(items: list[Daily], n: int = 5) -> list[Daily]:
    """Sort by last session traded value (close x volume), highest first."""

    def value(d: Daily) -> float:
        b = d.bars[-1]
        return b.close * b.volume

    return sorted((d for d in items if d.bars[-1].volume), key=value, reverse=True)[:n]


def most_active_india(n: int = 5, client: httpx.Client | None = None) -> list[Daily]:
    c = _client(client)
    items = [d for s in IN_LARGE_CAPS if (d := fetch_daily(s, client=c))]
    return rank_most_active(items, n)


def parse_rss(text: str, symbol: str) -> list[NewsItem]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
            pub = when.astimezone(UTC).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            pub = ""
        out.append(NewsItem(symbol, title, link, "Yahoo Finance", pub))
    return out


def fetch_news(symbol: str, client: httpx.Client | None = None) -> list[NewsItem]:
    c = _client(client)
    try:
        r = c.get(RSS_URL, params={"s": symbol, "region": "US", "lang": "en-US"})
        r.raise_for_status()
        return parse_rss(r.text, symbol)
    except httpx.HTTPError:
        return []


def news_one_per_company(
    symbols: list[str], n: int = 5, client: httpx.Client | None = None
) -> list[NewsItem]:
    """Newest headline for each of the first ``n`` symbols that have any news."""

    c = _client(client)
    out: list[NewsItem] = []
    seen: set[str] = set()
    for s in symbols:
        if s in seen:
            continue
        seen.add(s)
        titles = {x.title for x in out}
        items = sorted(fetch_news(s, client=c), key=lambda x: x.published, reverse=True)
        fresh = [x for x in items if x.title not in titles]
        if fresh:
            out.append(fresh[0])
        if len(out) == n:
            break
    return out
