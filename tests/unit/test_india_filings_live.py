"""Offline tests for live NSE/BSE corporate announcements (mocked transport)."""

from __future__ import annotations

from unittest import TestCase

from financial_intelligence.config.settings import Settings
from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.news import InformationClass
from financial_intelligence.domain.sources import SourceAuthorityTier
from financial_intelligence.infrastructure.financial.india_filings import (
    INDIA_FILINGS_ALLOWED_HOSTS,
    IndiaCorporateAnnouncementsSource,
    IndiaFilingAuthority,
    india_authority_rank,
)
from financial_intelligence.infrastructure.http import HttpResponse
from financial_intelligence.infrastructure.news import InMemoryNewsEventAdapter
from financial_intelligence.infrastructure.news.live_events import (
    FallbackNewsEventAdapter,
    LiveCompanyEventAdapter,
)
from tests.unit.live_fakes import (
    Clock,
    FakeTransport,
    catalog,
    company,
    html_home,
    http_client,
    json_response,
    no_wait_limiter,
)

NSE_ROWS = [
    {
        "symbol": "RELIANCE",
        "desc": "Outcome of Board Meeting - Financial Results",
        "attchmntText": "Board approved quarterly results.",
        "an_dt": "13-Oct-2026 18:05:11",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/RIL_13102026.pdf",
        "seq_id": "101",
    },
    {"symbol": "RELIANCE", "desc": "", "an_dt": "12-Oct-2026 10:00:00"},
    {"symbol": "RELIANCE", "desc": "Very old item", "an_dt": "01-Jan-2020 10:00:00"},
]
BSE_TABLE = {
    "Table": [
        {
            "NEWSID": "abc-1",
            "NEWSSUB": "Appointment of Director",
            "HEADLINE": "Company appoints an independent director.",
            "NEWS_DT": "2026-10-12T16:30:00.123",
            "ATTACHMENTNAME": "file one.pdf",
        }
    ]
}


def _handler(nse_status: int = 200, bse_status: int = 200):  # type: ignore[no-untyped-def]
    def handler(url: str, headers: dict[str, str]) -> HttpResponse:
        if url == "https://www.nseindia.com/":
            return html_home()
        if "corporate-announcements" in url:
            assert "symbol=RELIANCE" in url
            assert headers.get("Cookie") == "nsit=abc"
            return json_response(NSE_ROWS, status=nse_status)
        if "AnnSubCategoryGetData" in url:
            assert "strScrip=500325" in url
            return json_response(BSE_TABLE, status=bse_status)
        raise AssertionError(url)

    return handler


def _source(transport: FakeTransport) -> IndiaCorporateAnnouncementsSource:
    return IndiaCorporateAnnouncementsSource(
        http_client(transport), rate_limiter=no_wait_limiter(), clock=Clock()
    )


class AnnouncementTests(TestCase):
    def test_nse_and_bse_announcements_normalized(self) -> None:
        transport = FakeTransport(_handler())
        events = _source(transport).fetch(company("RELIANCE"))
        self.assertEqual(len(events), 2)
        nse, bse = events
        self.assertEqual(nse.evidence.provider_name, "nse_corporate_announcements")
        self.assertEqual(nse.event_type.value, "earnings")
        self.assertEqual(nse.information_class, InformationClass.FACT)
        self.assertEqual(nse.evidence.authority_tier, SourceAuthorityTier.TIER_1_AUTHORITATIVE)
        self.assertEqual(nse.event_date.isoformat(), "2026-10-13")
        self.assertEqual(bse.event_type.value, "management")
        self.assertTrue((bse.evidence.source_url or "").endswith("file%20one.pdf"))
        hosts = {u.split("/")[2] for u in transport.urls}
        self.assertTrue(hosts <= INDIA_FILINGS_ALLOWED_HOSTS)

    def test_cache_prevents_refetch(self) -> None:
        transport = FakeTransport(_handler())
        source = _source(transport)
        source.fetch(company("RELIANCE"))
        calls = len(transport.urls)
        source.fetch(company("RELIANCE"))
        self.assertEqual(len(transport.urls), calls)

    def test_nse_failure_keeps_bse(self) -> None:
        events = _source(FakeTransport(_handler(nse_status=503))).fetch(company("RELIANCE"))
        self.assertEqual(
            [e.evidence.provider_name for e in events], ["bse_corporate_announcements"]
        )

    def test_non_indian_company_makes_no_calls(self) -> None:
        transport = FakeTransport(_handler())
        self.assertEqual(_source(transport).fetch(company("AAPL")), ())
        self.assertEqual(transport.urls, [])

    def test_authority_rank_unchanged(self) -> None:
        self.assertEqual(india_authority_rank(IndiaFilingAuthority.NSE), 0)


class AdapterFallbackTests(TestCase):
    def test_live_package_and_fixture_fallback(self) -> None:
        reliance = company("RELIANCE")
        live = LiveCompanyEventAdapter(
            catalog(), [_source(FakeTransport(_handler()))], clock=Clock()
        )
        adapter = FallbackNewsEventAdapter(live, InMemoryNewsEventAdapter())
        package = adapter.get_event_package(reliance.company_id)
        assert package is not None
        self.assertEqual(package.data_origin, DataOrigin.LIVE)
        self.assertEqual(len(package.events), 2)

        dead = LiveCompanyEventAdapter(
            catalog(), [_source(FakeTransport(_handler(503, 503)))], clock=Clock()
        )
        fallback = FallbackNewsEventAdapter(dead, InMemoryNewsEventAdapter())
        self.assertEqual(
            fallback.get_event_package(reliance.company_id),
            InMemoryNewsEventAdapter().get_event_package(reliance.company_id),
        )

    def test_setting_default_off_and_container_builds(self) -> None:
        from financial_intelligence.composition import build_container

        self.assertFalse(
            Settings(_env_file=None, APP_ENV="test").india_filings_live_enabled  # type: ignore[call-arg]
        )
        container = build_container(
            Settings(_env_file=None, APP_ENV="test", INDIA_FILINGS_LIVE_ENABLED="true")  # type: ignore[call-arg]
        )
        self.assertTrue(container.settings.india_filings_live_enabled)
