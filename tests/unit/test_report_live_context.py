"""LiveCompanyContext: free industry/peer and regulatory sections (fake HTTP)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from amrp.infrastructure.reporting.live_context import LiveCompanyContext, yahoo_symbol

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>SEBI issues notice to Acme Ltd</title><link>https://example.com/a</link>
<pubDate>Tue, 27 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Acme wins award</title><link>https://example.com/b</link>
<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def _chart(first: float, last: float) -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"regularMarketPrice": last, "currency": "USD"},
                    "indicators": {"quote": [{"close": [first, None, last]}]},
                }
            ]
        }
    }


class _Http:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_json(self, url: str, *, extra_headers: dict[str, str] | None = None) -> Any:
        self.urls.append(url)
        if "finance/search" in url:
            sym = "ACME.NS" if "ACME.NS" in url else "AAPL"
            return {"quotes": [{"symbol": sym, "sectorDisp": "Technology", "industryDisp": "IT"}]}
        if "recommendationsbysymbol" in url:
            return {"finance": {"result": [{"recommendedSymbols": [{"symbol": "MSFT"}]}]}}
        if "chart/MSFT" in url:
            return _chart(100, 110)
        if "/chart/" in url:
            return _chart(100, 125)
        if "company_tickers" in url:
            return {"0": {"ticker": "AAPL", "cik_str": 320193}}
        if "submissions" in url:
            return {
                "sicDescription": "Electronic Computers",
                "sic": "3571",
                "filings": {
                    "recent": {
                        "form": ["4", "10-Q", "8-K"],
                        "filingDate": ["2026-10-01", "2026-07-31", "2026-07-30"],
                        "accessionNumber": ["a", "0000320193-26-000001", "0000320193-26-000002"],
                        "primaryDocument": ["x.xml", "q.htm", "k.htm"],
                    }
                },
            }
        raise OSError("unexpected url")

    def get_response(self, url: str, **_: Any) -> Any:
        return SimpleNamespace(body=RSS)


def test_yahoo_symbol_maps_indian_exchanges() -> None:
    assert yahoo_symbol("NSE", "TCS") == "TCS.NS"
    assert yahoo_symbol("BSE", "TCS") == "TCS.BO"
    assert yahoo_symbol("NASDAQ", "AAPL") == "AAPL"


def test_industry_has_sector_sic_and_peer_comparison() -> None:
    ctx = LiveCompanyContext(_Http(), sec_http=_Http())
    out = ctx.industry(exchange="NASDAQ", ticker="AAPL", name="Apple")
    assert out["status"] == "ok" and out["data_origin"] == "live"
    labels = {f["label"]: f["value"] for f in out["figures"]}
    assert labels["Sector"] == "Technology"
    assert "Electronic Computers" in labels["SEC SIC industry"]
    assert labels["AAPL 1-year return"] == "+25.00%"
    assert out["items"][0]["title"] == "Peer: MSFT"
    assert "outperformed" in out["items"][0]["detail"]


def test_us_regulatory_lists_material_sec_filings_only() -> None:
    ctx = LiveCompanyContext(_Http(), sec_http=_Http())
    out = ctx.regulatory(exchange="NASDAQ", ticker="AAPL")
    titles = [i["title"] for i in out["items"]]
    assert out["status"] == "ok" and len(titles) == 2  # Form 4 skipped
    assert titles[0].startswith("10-Q")
    assert out["items"][0]["url"].startswith("https://www.sec.gov/Archives/")


def test_us_regulatory_without_sec_agent_is_unavailable_with_hint() -> None:
    out = LiveCompanyContext(_Http()).regulatory(exchange="NASDAQ", ticker="AAPL")
    assert out["status"] == "unavailable" and "SEC_USER_AGENT" in out["message"]


def test_india_regulatory_falls_back_to_keyword_filtered_news() -> None:
    company = SimpleNamespace(display_name="Acme")
    out = LiveCompanyContext(_Http()).regulatory(exchange="NSE", ticker="ACME", company=company)
    assert out["provider_name"] == "google_news_regulatory"
    assert [i["title"] for i in out["items"]] == ["SEBI issues notice to Acme Ltd"]
    assert out["items"][0]["date"] == "2026-10-27"


def test_failures_are_soft() -> None:
    class _Down:
        def get_json(self, url: str, **_: Any) -> Any:
            raise OSError("down")

    out = LiveCompanyContext(_Down()).industry(exchange="NYSE", ticker="X")
    assert out["status"] == "unavailable"
