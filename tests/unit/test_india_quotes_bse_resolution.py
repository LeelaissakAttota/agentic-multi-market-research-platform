"""Offline tests: dynamic BSE scrip resolution and BSE volume (mocked transport)."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest import TestCase

from financial_intelligence.infrastructure.http import HttpResponse
from tests.unit.test_india_quotes import FakeTransport, _bse_payload, _client, _home, _json

_SCRIP_LIST = [
    {"SCRIP_CD": "543320", "scrip_id": "ZOMATO", "ISIN_NUMBER": "INE758T01015"},
    {"SCRIP_CD": "500570", "scrip_id": "TATAMOTORS", "ISIN_NUMBER": "INE155A01022"},
    {"SCRIP_CD": "bad", "scrip_id": "BROKEN"},
]


def _raw_json(payload: object) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=json.dumps(payload).encode(),
        content_type="application/json",
        headers={},
    )


class DynamicScripTests(TestCase):
    def test_resolves_unknown_ticker_from_scrip_list_and_reads_volume(self) -> None:
        seen: list[str] = []

        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            seen.append(url)
            if "nseindia" in url:
                return _json({}, status=503)
            if "ListofScripData" in url:
                return _raw_json(_SCRIP_LIST)
            if "StockTrading" in url:
                self.assertIn("scripcode=500570", url)
                return _json({"TTQ": "12.34", "TTQin": "Lakh"})
            self.assertIn("scripcode=500570", url)
            return _json(_bse_payload())

        transport = FakeTransport(handler)
        client = _client(transport)
        quote = client.get_quote("TATAMOTORS")
        assert quote is not None
        self.assertEqual(quote.provider_name, "bse_scrip_header")
        self.assertEqual(quote.volume, Decimal("1234000"))
        self.assertIsNone(client.get_quote("NOSUCHCO"))
        self.assertEqual(sum("ListofScripData" in u for u in seen), 1)  # list cached

    def test_isin_from_nse_is_preferred_for_bse_lookup(self) -> None:
        state = {"nse": 0}

        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if url.endswith("nseindia.com/"):
                return _home()
            if "quote-equity" in url:
                state["nse"] += 1
                if state["nse"] == 1:
                    payload = {
                        "info": {"symbol": "ETERNAL", "isin": "ine758t01015"},
                        "priceInfo": {
                            "lastPrice": 250,
                            "open": 248,
                            "intraDayHighLow": {"min": 245, "max": 252},
                        },
                    }
                    return _json(payload)
                return _json({}, status=503)
            if "ListofScripData" in url:
                return _raw_json(_SCRIP_LIST)
            if "StockTrading" in url:
                return _json({"TTQ": "1.5", "TTQin": "Cr"})
            self.assertIn("scripcode=543320", url)
            return _json(_bse_payload())

        clock_client = _client(FakeTransport(handler))
        self.assertIsNotNone(clock_client.get_quote("ETERNAL"))
        clock_client._cache.clear()  # force refetch; NSE now fails
        quote = clock_client.get_quote("ETERNAL")
        assert quote is not None
        self.assertEqual(quote.provider_name, "bse_scrip_header")
        self.assertEqual(quote.volume, Decimal("15000000"))

    def test_unknown_volume_unit_yields_zero(self) -> None:
        def handler(url: str, _h: dict[str, str]) -> HttpResponse:
            if "nseindia" in url:
                return _json({}, status=503)
            if "StockTrading" in url:
                return _json({"TTQ": "9", "TTQin": "Gazillion"})
            return _json(_bse_payload())

        quote = _client(FakeTransport(handler)).get_quote("RELIANCE")
        assert quote is not None
        self.assertEqual(quote.volume, Decimal("0"))
