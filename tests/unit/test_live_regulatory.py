"""Offline tests for SEC litigation RSS and SEBI orders feed adapters."""

from __future__ import annotations

from unittest import TestCase
from urllib.parse import urlsplit

from amrp.config.settings import Settings
from amrp.domain.data_origin import DataOrigin
from amrp.domain.regulatory import RegulatorCode, RegulatoryActionType
from amrp.domain.sources import SourceAuthorityTier
from amrp.infrastructure.http import HttpResponse
from amrp.infrastructure.regulatory import InMemoryRegulatoryAdapter
from amrp.infrastructure.regulatory.live_feeds import (
    FallbackRegulatoryAdapter,
    LiveRegulatoryAdapter,
    SebiOrdersFeedSource,
    SecLitigationFeedSource,
)
from tests.unit.live_fakes import (
    Clock,
    FakeTransport,
    catalog,
    company,
    http_client,
    json_response,
    no_wait_limiter,
    xml_response,
)

SEC_LIT = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>LR-26001: SEC Obtains Final Judgment Against Former Apple Inc. Employee</title>
<link>https://www.sec.gov/litigation/litreleases/lr-26001</link>
<pubDate>Fri, 09 Oct 2026 14:00:00 EST</pubDate>
<description>Insider trading case; civil penalty ordered.</description></item>
<item><title>LR-26002: SEC Charges Pineapple Farms LLC</title>
<link>https://www.sec.gov/litigation/litreleases/lr-26002</link>
<pubDate>Fri, 09 Oct 2026 15:00:00 EST</pubDate></item>
</channel></rss>"""
SEC_ADMIN = """<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>"""
SEBI = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Settlement Order in respect of Reliance Industries Limited</title>
<link>https://www.sebi.gov.in/enforcement/orders/oct-2026/settlement-order_1.html</link>
<pubDate>Mon, 12 Oct 2026 10:00:00 +0530</pubDate></item>
<item><title>Consultation paper on Reliance on credit ratings</title>
<link>https://www.sebi.gov.in/reports-and-statistics/reports/oct-2026/cp.html</link>
<pubDate>Mon, 12 Oct 2026 11:00:00 +0530</pubDate></item>
<item><title>Adjudication order in the matter of Infosys insider trading</title>
<link>https://www.sebi.gov.in/enforcement/orders/oct-2026/ao_2.html</link>
<pubDate>Sun, 11 Oct 2026 10:00:00 +0530</pubDate></item>
</channel></rss>"""


def _handler(url: str, headers: dict[str, str]) -> HttpResponse:
    if url.endswith("litreleases.xml"):
        return xml_response(SEC_LIT)
    if url.endswith("admin.xml"):
        return xml_response(SEC_ADMIN)
    if url.endswith("sebirss.xml"):
        return xml_response(SEBI)
    raise AssertionError(url)


def _adapter(transport: FakeTransport) -> LiveRegulatoryAdapter:
    clock = Clock()
    return LiveRegulatoryAdapter(
        catalog(),
        [
            SecLitigationFeedSource(
                http_client(transport), rate_limiter=no_wait_limiter(), clock=clock
            ),
            SebiOrdersFeedSource(
                http_client(transport), rate_limiter=no_wait_limiter(), clock=clock
            ),
        ],
        clock=clock,
    )


class RegulatoryFeedTests(TestCase):
    def test_sec_release_matched_to_company_by_name(self) -> None:
        transport = FakeTransport(_handler)
        package = _adapter(transport).get_regulatory_package(company("AAPL").company_id)
        assert package is not None
        self.assertEqual(package.data_origin, DataOrigin.LIVE)
        self.assertEqual(len(package.events), 1)  # "Pineapple" must not match "Apple"
        event = package.events[0]
        self.assertEqual(event.regulator, RegulatorCode.SEC)
        self.assertEqual(event.case_reference, "LR-26001")
        self.assertEqual(event.action_type, RegulatoryActionType.PENALTY)
        self.assertEqual(event.evidence.authority_tier, SourceAuthorityTier.TIER_1_AUTHORITATIVE)
        self.assertTrue(
            {urlsplit(u).hostname for u in transport.urls} <= {"www.sec.gov", "www.sebi.gov.in"}
        )

    def test_sebi_enforcement_only_and_feeds_cached(self) -> None:
        transport = FakeTransport(_handler)
        adapter = _adapter(transport)
        package = adapter.get_regulatory_package(company("RELIANCE").company_id)
        assert package is not None
        self.assertEqual([e.regulator for e in package.events], [RegulatorCode.SEBI])
        self.assertEqual(package.events[0].action_type, RegulatoryActionType.ORDER)
        calls = len(transport.urls)
        infy = adapter.get_regulatory_package(company("INFY").company_id)
        assert infy is not None
        self.assertEqual(len(transport.urls), calls)  # market-wide feeds cached

    def test_unavailable_feeds_fall_back_to_sample(self) -> None:
        transport = FakeTransport(lambda _u, _h: json_response({}, status=503))
        reliance = company("RELIANCE").company_id
        fallback = FallbackRegulatoryAdapter(_adapter(transport), InMemoryRegulatoryAdapter())
        self.assertEqual(
            fallback.get_regulatory_package(reliance),
            InMemoryRegulatoryAdapter().get_regulatory_package(reliance),
        )

    def test_no_match_returns_none(self) -> None:
        transport = FakeTransport(_handler)
        self.assertIsNone(_adapter(transport).get_regulatory_package(company("TCS").company_id))

    def test_setting_default_off_and_container_builds(self) -> None:
        from amrp.composition import build_container

        self.assertFalse(Settings(_env_file=None, APP_ENV="test").live_regulatory_enabled)  # type: ignore[call-arg]
        container = build_container(
            Settings(_env_file=None, APP_ENV="test", LIVE_REGULATORY_ENABLED="true")  # type: ignore[call-arg]
        )
        self.assertTrue(container.settings.live_regulatory_enabled)
