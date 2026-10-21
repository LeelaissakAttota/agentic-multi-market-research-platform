from __future__ import annotations

from financial_intelligence.application.company_resolution import CompanyQuery
from financial_intelligence.application.resolve_company import ResolveCompany
from financial_intelligence.domain.identity import (
    CountryCode,
    ExchangeCode,
    TickerSymbol,
    company_match_key,
)
from financial_intelligence.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from financial_intelligence.infrastructure.company.universe import (
    BseEquityUniverseSource,
    UniverseCompanyCatalog,
)
from tests.unit.test_audit2_universe import _Src

ROWS = [
    {"SCRIP_CD": "500570", "scrip_id": "TMPV", "Issuer_Name": "Tata Motors Passenger Vehicles Ltd"},
    {"SCRIP_CD": "544569", "scrip_id": "TMCV", "Issuer_Name": "Tata Motors Limited"},
    {"SCRIP_CD": "543320", "scrip_id": "ETERNAL", "Issuer_Name": "Eternal Limited"},
]


def _catalog() -> UniverseCompanyCatalog:
    src = _Src(BseEquityUniverseSource(None), ROWS)  # type: ignore[arg-type]
    return UniverseCompanyCatalog(InMemoryCompanyCatalog(), [src])


def test_former_tickers_map_to_current_listing() -> None:
    resolver = ResolveCompany(_catalog())
    tm = resolver.execute(CompanyQuery(raw_query="TATAMOTORS", exchange=ExchangeCode("NSE")))
    assert tm.company is not None
    assert tm.company.display_name == "Tata Motors Passenger Vehicles Ltd"
    zo = resolver.execute(CompanyQuery(raw_query="ZOMATO"))
    assert zo.company is not None and zo.company.display_name == "Eternal Limited"
    assert resolver.execute(CompanyQuery(raw_query="NOPEX")).company is None


def test_tata_motors_name_returns_both_demerged_entities() -> None:
    found = _catalog().find_by_alias(company_match_key("Tata Motors"), country=CountryCode("IN"))
    assert {c.display_name for c in found} == {
        "Tata Motors Passenger Vehicles Ltd",
        "Tata Motors Limited",
    }


def test_resolver_resolves_old_ticker_query() -> None:
    result = ResolveCompany(_catalog()).execute(
        CompanyQuery(raw_query="TATAMOTORS", ticker=TickerSymbol("TATAMOTORS"))
    )
    assert result.company is not None
    assert result.company.display_name.startswith("Tata Motors Passenger")
