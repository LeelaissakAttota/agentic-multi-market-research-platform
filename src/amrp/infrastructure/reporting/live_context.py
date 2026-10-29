"""Free, no-key live context for the Industry and Regulatory report sections.

* Industry: Yahoo Finance public search (sector / industry) and
  ``recommendationsbysymbol`` (peer symbols), plus Yahoo chart closes for a
  1-year peer comparison. SEC EDGAR ``submissions`` adds the SIC industry for
  US registrants when ``SEC_USER_AGENT`` is configured.
* Regulatory: SEC EDGAR recent filings (10-K, 10-Q, 8-K, proxy, ownership
  reports) for US registrants; NSE/BSE exchange disclosures for India when an
  announcements source is wired.

Everything is bounded, fail-soft and labelled ``live``. Third-party
classification (Yahoo) is Tier 2; SEC / exchange filings are Tier 1.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.reporting.live_context")

YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=1&newsCount=0"
YAHOO_PEERS = "https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{s}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=1y&interval=1mo"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
MATERIAL_FORMS = {
    "10-K": "Annual report",
    "10-Q": "Quarterly report",
    "8-K": "Current report (material event)",
    "DEF 14A": "Proxy statement",
    "20-F": "Annual report (foreign issuer)",
    "6-K": "Foreign issuer report",
    "SC 13D": "Beneficial ownership (activist)",
    "SC 13G": "Beneficial ownership (passive)",
    "SC 13G/A": "Beneficial ownership amendment",
    "S-3": "Registration statement",
    "S-8": "Employee share registration",
    "10-K/A": "Annual report amendment",
    "8-K/A": "Current report amendment",
}
MAX_PEERS = 5
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
)
_REG_WORDS = re.compile(
    r"sebi|regulat|penalt|court|\btax|probe|lawsuit|litigat|\border|complian|settle|"
    r"investigat|notice|\bban\b|\bsec\b|antitrust|\bgst\b|\bcci\b|\bfined?\b",
    re.I,
)
REG_TERMS = "(SEBI OR regulatory OR regulator OR penalty OR compliance OR tax OR court OR probe)"
LIVE_CONTEXT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
MAX_FILINGS = 8


class _Http(Protocol):
    def get_json(self, url: str, *, extra_headers: dict[str, str] | None = None) -> Any: ...


class _Announcements(Protocol):
    def fetch(self, company: Any) -> tuple[Any, ...]: ...


def yahoo_symbol(exchange: str | None, ticker: str) -> str:
    ex = (exchange or "").upper()
    if ex == "NSE" and "." not in ticker:
        return f"{ticker}.NS"
    if ex == "BSE" and "." not in ticker:
        return f"{ticker}.BO"
    return ticker


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


class LiveCompanyContext:
    """Fetch industry classification/peers and regulatory filings (fail-soft)."""

    def __init__(
        self,
        http: _Http,
        *,
        sec_http: _Http | None = None,
        india_announcements: _Announcements | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._sec = sec_http
        self._india = india_announcements
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cik: dict[str, str] | None = None
        self._memo: dict[str, tuple[datetime, Any]] = {}
        self._ttl_seconds = 600.0

    # ------------------------------------------------------------ helpers
    def _get(self, http: _Http, url: str) -> Any:
        now = self._clock()
        hit = self._memo.get(url)
        if hit is not None and (now - hit[0]).total_seconds() < self._ttl_seconds:
            return hit[1]
        try:
            data = http.get_json(url)
            if len(self._memo) > 256:
                self._memo.clear()
            self._memo[url] = (now, data)
            return data
        except Exception as exc:  # network / parse: one source never sinks a section
            logger.info("live_context_fetch_failed", extra={"error_type": type(exc).__name__})
            return None

    def _one_year_return(self, symbol: str) -> tuple[float | None, str | None, float | None]:
        data = self._get(self._http, YAHOO_CHART.format(s=quote(symbol)))
        try:
            result = data["chart"]["result"][0]
            closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
            meta = result.get("meta") or {}
            last = float(meta.get("regularMarketPrice") or closes[-1])
            first = float(closes[0])
            return (last / first - 1.0 if first else None), meta.get("currency"), last
        except (TypeError, KeyError, IndexError, ValueError):
            return None, None, None

    def _sec_cik(self, ticker: str) -> str | None:
        if self._sec is None:
            return None
        if self._cik is None:
            raw = self._get(self._sec, SEC_TICKERS)
            table: dict[str, str] = {}
            if isinstance(raw, Mapping):
                for row in raw.values():
                    if isinstance(row, Mapping) and row.get("ticker"):
                        table[str(row["ticker"]).upper()] = f"{int(row['cik_str']):010d}"
            self._cik = table
        return self._cik.get(ticker.upper())

    def _sec_submissions(self, ticker: str) -> tuple[str | None, Mapping[str, Any] | None]:
        cik = self._sec_cik(ticker)
        if cik is None or self._sec is None:
            return None, None
        data = self._get(self._sec, SEC_SUBMISSIONS.format(cik=cik))
        return cik, data if isinstance(data, Mapping) else None

    # ------------------------------------------------------------ industry
    def industry(self, *, exchange: str | None, ticker: str, name: str = "") -> dict[str, Any]:
        symbol = yahoo_symbol(exchange, ticker)
        now = self._clock().isoformat()
        search = self._get(self._http, YAHOO_SEARCH.format(q=quote(symbol)))
        quote_row: Mapping[str, Any] = {}
        if isinstance(search, Mapping):
            for row in search.get("quotes") or []:
                if isinstance(row, Mapping) and str(row.get("symbol", "")).upper() == symbol.upper():
                    quote_row = row
                    break
        figures: list[dict[str, str]] = []
        sector = quote_row.get("sectorDisp") or quote_row.get("sector")
        industry = quote_row.get("industryDisp") or quote_row.get("industry")
        if sector:
            figures.append({"label": "Sector", "value": str(sector), "detail": "Yahoo Finance"})
        if industry:
            figures.append({"label": "Industry", "value": str(industry), "detail": "Yahoo Finance"})
        if (exchange or "").upper() not in {"NSE", "BSE"}:
            _, sub = self._sec_submissions(ticker)
            if sub and sub.get("sicDescription"):
                figures.append(
                    {
                        "label": "SEC SIC industry",
                        "value": f"{sub.get('sicDescription')} (SIC {sub.get('sic')})",
                        "detail": "SEC EDGAR",
                    }
                )
        own_ret, currency, last = self._one_year_return(symbol)
        if own_ret is not None:
            figures.append(
                {
                    "label": f"{symbol} 1-year return",
                    "value": _pct(own_ret),
                    "detail": f"last {currency or ''} {last:,.2f}".strip() if last else "",
                }
            )
        peers_raw = self._get(self._http, YAHOO_PEERS.format(s=quote(symbol)))
        peers: list[str] = []
        try:
            for row in peers_raw["finance"]["result"][0]["recommendedSymbols"]:
                if row.get("symbol") and row["symbol"].upper() != symbol.upper():
                    peers.append(str(row["symbol"]))
        except (TypeError, KeyError, IndexError):
            peers = []
        items: list[dict[str, Any]] = []
        for peer in peers[:MAX_PEERS]:
            ret, cur, px = self._one_year_return(peer)
            rel = ""
            if ret is not None and own_ret is not None:
                rel = "outperformed" if own_ret > ret else "underperformed"
                rel = f"; {symbol} {rel} this peer over 1 year"
            items.append(
                {
                    "title": f"Peer: {peer}",
                    "date": now[:10],
                    "detail": (
                        f"1-year return {_pct(ret)}"
                        + (f", last {cur or ''} {px:,.2f}" if px else "")
                        + rel
                    ),
                    "url": f"https://finance.yahoo.com/quote/{quote(peer)}",
                }
            )
        if not figures and not items:
            return {
                "status": "unavailable",
                "data_origin": None,
                "provider_name": "yahoo_finance",
                "message": "no free industry/peer data found for this company",
                "figures": [],
                "items": [],
            }
        return {
            "status": "ok",
            "data_origin": "live",
            "provider_name": "yahoo_finance" + ("+sec_edgar" if self._sec else ""),
            "message": f"{len(items)} peer(s) compared for {name or symbol}",
            "as_of": now,
            "figures": figures,
            "items": items,
        }

    # ------------------------------------------------------------ regulatory
    def _regulatory_news(self, company: Any, ticker: str) -> list[dict[str, Any]]:
        """Regulatory-keyword headlines from Google News RSS (Tier 3, labelled)."""

        import xml.etree.ElementTree as ET  # noqa: S405 - RSS from a fixed host, no DTDs

        name = str(getattr(company, "display_name", "") or ticker)
        url = GOOGLE_NEWS_RSS.format(q=quote(f'"{name}" {REG_TERMS}'))
        get_response = getattr(self._http, "get_response", None)
        if get_response is None:
            return []
        try:
            body = get_response(url, accept="application/rss+xml, application/xml").body
            if b"<!DOCTYPE" in body[:500] or b"<!ENTITY" in body:
                return []
            root = ET.fromstring(body)  # noqa: S314
        except Exception as exc:
            logger.info("regulatory_news_failed", extra={"error_type": type(exc).__name__})
            return []
        out: list[dict[str, Any]] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            date = pub
            try:
                from email.utils import parsedate_to_datetime

                date = parsedate_to_datetime(pub).date().isoformat()
            except (TypeError, ValueError):
                pass
            if title and _REG_WORDS.search(title):
                out.append(
                    {
                        "title": title[:160],
                        "date": date or "n/a",
                        "detail": "news coverage (not an official filing)",
                        "url": link if link.startswith("https://") else None,
                    }
                )
        out.sort(key=lambda i: str(i["date"]), reverse=True)
        return out[:MAX_FILINGS]

    def regulatory(
        self, *, exchange: str | None, ticker: str, company: Any = None
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        provider = ""
        if (exchange or "").upper() in {"NSE", "BSE"}:
            provider = "nse_bse_announcements"
            if self._india is not None and company is not None:
                try:
                    events = self._india.fetch(company)
                except Exception as exc:
                    logger.info("india_disclosures_failed", extra={"error_type": type(exc).__name__})
                    events = ()
                for ev in list(events)[:MAX_FILINGS]:
                    d = ev.to_dict() if hasattr(ev, "to_dict") else {}
                    evidence = d.get("evidence") if isinstance(d.get("evidence"), Mapping) else {}
                    items.append(
                        {
                            "title": str(d.get("title") or "Exchange disclosure")[:160],
                            "date": str(d.get("event_date") or d.get("published_at") or "n/a")[:10],
                            "detail": "Exchange disclosure (SEBI LODR filing)",
                            "url": evidence.get("source_url") if evidence else None,
                        }
                    )
            if not items:
                provider = "google_news_regulatory"
                items = self._regulatory_news(company, ticker)
            message = (
                f"{len(items)} recent NSE/BSE disclosure(s)"
                if provider == "nse_bse_announcements"
                else f"{len(items)} regulatory news item(s) (secondary coverage, not filings)"
                if items
                else "no NSE/BSE disclosures or regulatory news found"
            )
        else:
            provider = "sec_edgar"
            cik, sub = self._sec_submissions(ticker)
            recent = (sub or {}).get("filings", {}).get("recent", {}) if sub else {}
            forms = recent.get("form") or []
            for i, form in enumerate(forms):
                if form not in MATERIAL_FORMS:
                    continue
                acc = str(recent["accessionNumber"][i])
                doc = str((recent.get("primaryDocument") or [""] * len(forms))[i] or "")
                desc = str((recent.get("primaryDocDescription") or [""] * len(forms))[i] or "")
                items.append(
                    {
                        "title": f"{form} — {MATERIAL_FORMS[form]}",
                        "date": str(recent["filingDate"][i]),
                        "detail": f"SEC filing{': ' + desc if desc and desc != form else ''}",
                        "url": SEC_ARCHIVE.format(
                            cik=int(cik or 0), acc=acc.replace("-", ""), doc=doc
                        )
                        if doc
                        else None,
                    }
                )
                if len(items) >= MAX_FILINGS:
                    break
            if self._sec is None:
                message = "SEC filings need SEC_USER_AGENT (free) in Settings"
            else:
                message = f"{len(items)} recent material SEC filing(s)"
        if not items:
            return {
                "status": "unavailable",
                "data_origin": None,
                "provider_name": provider,
                "message": message,
                "items": [],
            }
        return {
            "status": "ok",
            "data_origin": "live",
            "provider_name": provider,
            "message": message,
            "as_of": self._clock().isoformat(),
            "items": items,
        }


def dumps(obj: object) -> str:  # pragma: no cover - debug helper
    return json.dumps(obj, indent=2, default=str)
