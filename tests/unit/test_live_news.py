"""Offline tests for GDELT / Google News RSS / IR RSS live news (mocked transport)."""

from __future__ import annotations

from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from financial_intelligence.config.settings import Settings
from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.news import InformationClass
from financial_intelligence.domain.sources import SourceAuthorityTier
from financial_intelligence.infrastructure.http import HttpResponse
from financial_intelligence.infrastructure.http.feeds import parse_feed
from financial_intelligence.infrastructure.news import InMemoryNewsEventAdapter
from financial_intelligence.infrastructure.news.live_events import (
    FallbackNewsEventAdapter,
    LiveCompanyEventAdapter,
    headline_key,
    rank_news_source,
)
from financial_intelligence.infrastructure.news.live_feeds import (
    GdeltDocSource,
    GoogleNewsRssSource,
    RssFeedSource,
    parse_ir_feed_setting,
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

GDELT = {
    "articles": [
        {
            "url": "https://www.reuters.com/markets/reliance-results-2026-10-13/",
            "title": "Reliance Industries quarterly profit rises",
            "seendate": "20261013T120000Z",
            "domain": "reuters.com",
        },
        {
            "url": "https://randomblog.example/reliance",
            "title": "Reliance stock: what next?",
            "seendate": "20261012T080000Z",
            "domain": "randomblog.example",
        },
        {"url": "javascript:alert(1)", "title": "bad", "seendate": "20261012T080000Z"},
        {"url": "https://old.example/x", "title": "Old", "seendate": "20200101T000000Z"},
    ]
}
GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Reliance Industries quarterly profit rises - Reuters</title>
<link>https://news.google.com/rss/articles/abc</link>
<pubDate>Tue, 13 Oct 2026 12:30:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Reliance profit&lt;/a&gt;</description>
<source url="https://www.reuters.com">Reuters</source></item>
<item><title>Reliance launches new product - Some Site</title>
<link>https://news.google.com/rss/articles/def</link>
<pubDate>Mon, 12 Oct 2026 09:00:00 GMT</pubDate>
<source url="https://some.example">Some Site</source></item>
</channel></rss>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>IR</title>
<entry><title>Apple announces dividend</title>
<link href="https://investor.apple.com/news/1"/>
<updated>2026-10-10T13:00:00Z</updated><summary>Board declared dividend.</summary></entry>
</feed>"""


def _handler(url: str, _h: dict[str, str]) -> HttpResponse:
    host = urlsplit(url).hostname
    if host == "api.gdeltproject.org":
        return json_response(GDELT)
    if host == "news.google.com":
        return xml_response(GOOGLE_RSS)
    if host == "investor.apple.com":
        return xml_response(ATOM)
    raise AssertionError(url)


def _kwargs() -> dict[str, object]:
    return {
        "rate_limiter": no_wait_limiter(),
        "cache_ttl_seconds": 900.0,
        "lookback_days": 7,
        "clock": Clock(),
    }


class GdeltTests(TestCase):
    def test_articles_normalized_tiered_and_windowed(self) -> None:
        transport = FakeTransport(_handler)
        events = GdeltDocSource(http_client(transport), **_kwargs()).fetch(company("RELIANCE"))  # type: ignore[arg-type]
        self.assertEqual(len(events), 2)
        reuters, blog = events
        self.assertEqual(reuters.evidence.authority_tier, SourceAuthorityTier.TIER_3_REPUTABLE_NEWS)
        self.assertEqual(blog.evidence.authority_tier, SourceAuthorityTier.TIER_4_GENERAL_WEB)
        self.assertTrue(all(e.information_class is not InformationClass.FACT for e in events))
        query = parse_qs(urlsplit(transport.urls[0]).query)
        self.assertIn("sourcecountry:IN", query["query"][0])
        self.assertEqual(query["format"], ["json"])

    def test_empty_or_html_body_is_unavailable(self) -> None:
        transport = FakeTransport(
            lambda _u, _h: HttpResponse(200, b"<html>rate limited</html>", "text/html", {})
        )
        source = GdeltDocSource(http_client(transport), **_kwargs())  # type: ignore[arg-type]
        self.assertEqual(source.fetch(company("RELIANCE")), ())


class RssTests(TestCase):
    def test_google_news_india_edition_and_publisher_tier(self) -> None:
        transport = FakeTransport(_handler)
        source = GoogleNewsRssSource(http_client(transport), **_kwargs())  # type: ignore[arg-type]
        events = source.fetch(company("RELIANCE"))
        self.assertEqual(len(events), 2)
        query = parse_qs(urlsplit(transport.urls[0]).query)
        self.assertEqual(query["ceid"], ["IN:en"])
        self.assertEqual(
            events[0].evidence.authority_tier, SourceAuthorityTier.TIER_3_REPUTABLE_NEWS
        )
        self.assertEqual(events[0].summary, "Reliance profit")
        self.assertEqual(events[1].event_type.value, "product")

    def test_us_edition_for_us_company(self) -> None:
        transport = FakeTransport(_handler)
        GoogleNewsRssSource(http_client(transport), **_kwargs()).fetch(company("AAPL"))  # type: ignore[arg-type]
        self.assertEqual(parse_qs(urlsplit(transport.urls[0]).query)["gl"], ["US"])

    def test_ir_atom_feed_and_allowlist(self) -> None:
        feeds = parse_ir_feed_setting(
            "AAPL=https://investor.apple.com/rss,BAD=http://insecure.example/x,junk"
        )
        self.assertEqual(feeds, {"AAPL": "https://investor.apple.com/rss"})
        transport = FakeTransport(_handler)
        events = RssFeedSource(http_client(transport), feeds, **_kwargs()).fetch(company("AAPL"))  # type: ignore[arg-type]
        self.assertEqual([e.event_type.value for e in events], ["earnings"])
        self.assertEqual(transport.urls, ["https://investor.apple.com/rss"])

    def test_doctype_and_entities_rejected(self) -> None:
        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            b"<rss><item><title>&a;</title></item></rss>"
        )
        with self.assertRaises(ValueError):
            parse_feed(bomb)
        with self.assertRaises(ValueError):
            parse_feed(b"<rss><item><title>unclosed")


class AggregationTests(TestCase):
    def test_cross_source_dedupe_ranking_and_fallback(self) -> None:
        transport = FakeTransport(_handler)
        kwargs = _kwargs()
        live = LiveCompanyEventAdapter(
            catalog(),
            [
                GoogleNewsRssSource(http_client(transport), **kwargs),  # type: ignore[arg-type]
                GdeltDocSource(http_client(transport), **kwargs),  # type: ignore[arg-type]
            ],
            source_rank=rank_news_source,
            clock=Clock(),
        )
        reliance = company("RELIANCE")
        package = live.get_event_package(reliance.company_id)
        assert package is not None
        self.assertEqual(package.data_origin, DataOrigin.LIVE)
        keys = [headline_key(e.title) for e in package.events]
        self.assertEqual(len(keys), len(set(keys)))  # Reuters story appears once
        self.assertEqual(len(package.events), 3)

        dead = LiveCompanyEventAdapter(catalog(), [], clock=Clock())
        fallback = FallbackNewsEventAdapter(dead, InMemoryNewsEventAdapter())
        self.assertEqual(
            fallback.get_event_package(reliance.company_id),
            InMemoryNewsEventAdapter().get_event_package(reliance.company_id),
        )

    def test_headline_key_strips_publisher(self) -> None:
        self.assertEqual(headline_key("Big News - Reuters"), headline_key("big news!"))

    def test_settings_default_off_and_container_builds(self) -> None:
        from financial_intelligence.composition import build_container

        self.assertFalse(Settings(_env_file=None, APP_ENV="test").live_news_enabled)  # type: ignore[call-arg]
        container = build_container(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                APP_ENV="test",
                LIVE_NEWS_ENABLED="true",
                NEWS_IR_RSS_FEEDS="AAPL=https://investor.apple.com/rss",
            )
        )
        self.assertTrue(container.settings.live_news_enabled)
