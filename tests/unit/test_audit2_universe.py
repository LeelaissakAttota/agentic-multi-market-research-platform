from __future__ import annotations

from pathlib import Path

from amrp.config.settings import Settings
from amrp.domain.identity import (
    CompanyId,
    CountryCode,
    ExchangeCode,
    TickerSymbol,
    company_match_key,
)
from amrp.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from amrp.infrastructure.company.universe import (
    BseEquityUniverseSource,
    SecTickerUniverseSource,
    UniverseCompanyCatalog,
    stable_v4_uuid,
)
from amrp.infrastructure.financial.sec_cik_mapping import sec_cik_for_company
from amrp.infrastructure.http import HttpFailureKind, HttpTransportError

BSE_ROWS = [
    {"SCRIP_CD": "543320", "scrip_id": "ETERNAL", "Issuer_Name": "Eternal Limited"},
    {"SCRIP_CD": "500325", "scrip_id": "RELIANCE", "Issuer_Name": "Reliance Industries Limited"},
    {"SCRIP_CD": "500002", "scrip_id": "ABB", "Issuer_Name": "ABB India Limited"},
    {"SCRIP_CD": "x", "scrip_id": "bad ticker!", "Issuer_Name": "Bad"},
]
SEC = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
        [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        [999, "OTC THING", "OTCX", "OTC"],
    ],
}


class _Src:
    def __init__(self, inner: object, raw: object, *, fail: bool = False) -> None:
        self._inner = inner
        self.name = inner.name  # type: ignore[attr-defined]
        self._raw = raw
        self._fail = fail
        self.calls = 0

    def fetch_raw(self) -> object:
        self.calls += 1
        if self._fail:
            raise HttpTransportError(HttpFailureKind.NETWORK_ERROR, "down")
        return self._raw

    def build(self, raw: object):  # type: ignore[no-untyped-def]
        return self._inner.build(raw)  # type: ignore[attr-defined]


def _catalog(tmp: Path | None = None, *, fail: bool = False) -> tuple[UniverseCompanyCatalog, _Src]:
    bse = _Src(BseEquityUniverseSource(None), BSE_ROWS, fail=fail)  # type: ignore[arg-type]
    sec = _Src(SecTickerUniverseSource(None), SEC, fail=fail)  # type: ignore[arg-type]
    return UniverseCompanyCatalog(InMemoryCompanyCatalog(), [bse, sec], cache_dir=tmp), bse


def test_stable_ids_are_v4_and_deterministic() -> None:
    a = stable_v4_uuid("company:IN:ABB")
    assert a == stable_v4_uuid("company:IN:ABB")
    CompanyId.from_string(a)  # raises unless version 4


def test_unknown_indian_ticker_resolves_with_nse_and_bse_listings() -> None:
    catalog, _ = _catalog()
    found = catalog.find_by_ticker(TickerSymbol("ETERNAL"), exchange=ExchangeCode("NSE"))
    assert len(found) == 1
    exchanges = {lst.exchange.as_text() for lst in found[0].all_listings()}
    assert exchanges == {"NSE", "BSE"}


def test_former_name_zomato_resolves_to_eternal() -> None:
    catalog, _ = _catalog()
    found = catalog.find_by_alias(company_match_key("Zomato"), country=CountryCode("IN"))
    assert [c.display_name for c in found] == ["Eternal Limited"]


def test_reference_entries_win_and_are_not_duplicated() -> None:
    catalog, bse = _catalog()
    reliance = catalog.find_by_ticker(TickerSymbol("RELIANCE"), exchange=ExchangeCode("NSE"))
    assert reliance[0].company_id.as_text() == "11111111-1111-4111-8111-111111111001"
    assert bse.calls == 0  # base hit never loads the universe
    apple = catalog.find_by_ticker(TickerSymbol("AAPL"))
    assert len(apple) == 1


def test_us_universe_registers_cik_and_skips_otc() -> None:
    catalog, _ = _catalog()
    nvda = catalog.find_by_ticker(TickerSymbol("NVDA"))
    assert len(nvda) == 1
    assert sec_cik_for_company(nvda[0].company_id) == "0001045810"
    assert catalog.find_by_ticker(TickerSymbol("OTCX")) == ()


def test_disk_cache_and_outage_fallback(tmp_path: Path) -> None:
    catalog, _bse = _catalog(tmp_path)
    assert catalog.find_by_ticker(TickerSymbol("ABB"))
    assert (tmp_path / "india_bse_equity.json").exists()
    offline, offline_bse = _catalog(tmp_path, fail=True)
    assert offline.find_by_ticker(TickerSymbol("ABB"))  # served from disk cache
    assert offline_bse.calls == 0


def test_outage_without_cache_degrades_to_reference_only() -> None:
    catalog, _ = _catalog(fail=True)
    assert catalog.find_by_ticker(TickerSymbol("ABB")) == ()
    assert catalog.find_by_ticker(TickerSymbol("RELIANCE"))


def test_settings_follow_live_market_flag() -> None:
    assert Settings(_env_file=None, APP_ENV="test").company_universe_active() is False
    live = Settings(_env_file=None, APP_ENV="test", MARKET_DATA_LIVE_ENABLED=True)
    assert live.company_universe_active() is True
