"""Commodity Market page helpers: US futures (COMEX/NYMEX, USD) + related news.

Real data only. Every function returns ``None`` / ``[]`` on failure so the page can show
"not available".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from amrp.dashboard import market_live as ml

FX_SYMBOL = "USDINR=X"  # used by the forex/crypto pages
GOOGLE_RSS = "https://news.google.com/rss/search"


@dataclass(frozen=True, slots=True)
class Commodity:
    name: str
    symbol: str
    us_unit: str  # unit of the Yahoo futures quote (USD)
    exchange: str  # US exchange of the futures contract


COMMODITIES: dict[str, Commodity] = {
    "Gold": Commodity("Gold", "GC=F", "troy oz", "COMEX"),
    "Silver": Commodity("Silver", "SI=F", "troy oz", "COMEX"),
    "Crude Oil": Commodity("Crude Oil", "CL=F", "barrel", "NYMEX"),
    "Natural Gas": Commodity("Natural Gas", "NG=F", "mmBtu", "NYMEX"),
    "Copper": Commodity("Copper", "HG=F", "lb", "COMEX"),
}


def fetch_month(symbol: str, client: httpx.Client | None = None) -> ml.Daily | None:
    return ml.fetch_daily(symbol, client=client, range_="1mo")


def price_series(commodity: Commodity, client: httpx.Client | None = None) -> ml.Daily | None:
    return fetch_month(commodity.symbol, client or ml._client(None))


def news_queries(commodity: Commodity) -> list[str]:
    return [f"{commodity.name.lower()} futures"]


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
    commodity: Commodity, n: int = 8, client: httpx.Client | None = None
) -> list[ml.NewsItem]:
    """Yahoo headlines for the futures symbol plus Google News RSS, newest first, deduped."""

    c = client or ml._client(None)
    # Yahoo's futures feed mixes in general market stories; keep only on-topic ones.
    word = commodity.name.split()[0].lower()
    items = [x for x in ml.fetch_news(commodity.symbol, client=c) if word in x.title.lower()]
    for q in news_queries(commodity):
        params = {"q": f"{q} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
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
