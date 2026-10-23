"""Commodity Market page helpers: Yahoo futures + INR conversion + related news.

Real data only. India (MCX) prices are *approximations* converted from the
international futures (COMEX/NYMEX) with the live USD/INR rate into the MCX
quoting unit; they are not MCX quotes (no free official MCX feed is used).
Every function returns ``None`` / ``[]`` on failure so the page can show
"not available".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from financial_intelligence.dashboard import market_live as ml

TROY_OZ_G = 31.1034768
LB_PER_KG = 2.20462262
FX_SYMBOL = "USDINR=X"
GOOGLE_RSS = "https://news.google.com/rss/search"
APPROX_NOTE = "approx. from international futures, not the MCX quote"


@dataclass(frozen=True, slots=True)
class Commodity:
    name: str
    symbol: str
    us_unit: str  # unit of the Yahoo futures quote (USD)
    mcx_unit: str
    to_mcx_unit: float  # multiply a USD/us_unit price to get USD per mcx_unit
    exchange: str  # US exchange of the futures contract


COMMODITIES: dict[str, Commodity] = {
    "Gold": Commodity("Gold", "GC=F", "troy oz", "10 g", 10 / TROY_OZ_G, "COMEX"),
    "Silver": Commodity("Silver", "SI=F", "troy oz", "1 kg", 1000 / TROY_OZ_G, "COMEX"),
    "Crude Oil": Commodity("Crude Oil", "CL=F", "barrel", "barrel", 1.0, "NYMEX"),
    "Natural Gas": Commodity("Natural Gas", "NG=F", "mmBtu", "mmBtu", 1.0, "NYMEX"),
    "Copper": Commodity("Copper", "HG=F", "lb", "kg", LB_PER_KG, "COMEX"),
}
MARKETS = ("India (MCX)", "US (COMEX/NYMEX)")


def to_inr(prices: ml.Daily, fx: ml.Daily, factor: float) -> ml.Daily | None:
    """Convert USD futures bars to INR per MCX unit using the same-day USD/INR close.

    Days without an FX close use the most recent earlier FX close; bars before the
    first FX close are dropped. Returns ``None`` if nothing can be converted.
    """

    rates = sorted((b.date, b.close) for b in fx.bars if b.close)
    if not rates:
        return None
    bars: list[ml.Bar] = []
    for bar in prices.bars:
        earlier = [r for d, r in rates if d <= bar.date]
        if not earlier:
            continue
        k = earlier[-1] * factor
        bars.append(
            ml.Bar(bar.date, bar.open * k, bar.high * k, bar.low * k, bar.close * k, bar.volume)
        )
    if not bars:
        return None
    return replace(prices, currency="INR", bars=tuple(bars))


def fetch_month(symbol: str, client: httpx.Client | None = None) -> ml.Daily | None:
    return ml.fetch_daily(symbol, client=client, range_="1mo")


def price_series(
    commodity: Commodity, india: bool, client: httpx.Client | None = None
) -> ml.Daily | None:
    c = client or ml._client(None)
    usd = fetch_month(commodity.symbol, c)
    if usd is None or not india:
        return usd
    fx = fetch_month(FX_SYMBOL, c)
    return to_inr(usd, fx, commodity.to_mcx_unit) if fx else None


def news_queries(commodity: Commodity, india: bool) -> list[str]:
    name = commodity.name.lower()
    return [f"MCX {name}", f"{name} price India"] if india else [f"{name} futures"]


def parse_google_rss(text: str) -> list[ml.NewsItem]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip() or "Google News"
        if not title or not link.startswith("http"):
            continue
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            published = pub.astimezone(UTC).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            published = ""
        out.append(ml.NewsItem("", title, link, source, published))
    return out


def related_news(
    commodity: Commodity, india: bool, n: int = 8, client: httpx.Client | None = None
) -> list[ml.NewsItem]:
    """Yahoo headlines for the futures symbol plus Google News RSS, newest first, deduped."""

    c = client or ml._client(None)
    # Yahoo's futures feed mixes in general market stories; keep only on-topic ones.
    word = commodity.name.split()[0].lower()
    items = [x for x in ml.fetch_news(commodity.symbol, client=c) if word in x.title.lower()]
    for q in news_queries(commodity, india):
        params = {"q": f"{q} when:7d", "hl": "en-IN" if india else "en-US"}
        params |= {"gl": "IN", "ceid": "IN:en"} if india else {"gl": "US", "ceid": "US:en"}
        try:
            r = c.get(GOOGLE_RSS, params=params)
            r.raise_for_status()
            items += parse_google_rss(r.text)
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
