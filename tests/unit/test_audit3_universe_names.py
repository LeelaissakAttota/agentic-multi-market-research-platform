from __future__ import annotations

from amrp.domain.identity import CountryCode, company_match_key
from amrp.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from amrp.infrastructure.company.universe import (
    BseEquityUniverseSource,
    SecTickerUniverseSource,
    UniverseCompanyCatalog,
    display_case,
    short_name,
)
from tests.unit.test_audit2_universe import _Src

BSE = [
    {
        "SCRIP_CD": "500570",
        "scrip_id": "TMPV",
        "Scrip_Name": "Tata Motors Passenger Vehicles Ltd",
        "Issuer_Name": "TATA MOTORS PASSENGER VEHICLES LIMITED",
        "INDUSTRY": "Automobiles",
    },
    {"SCRIP_CD": "999001", "scrip_id": "KXYZBNK", "Issuer_Name": "KXYZ BANK LTD."},
]
SEC = {"data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"]]}


def test_display_case_and_short_name() -> None:
    assert display_case("TATA MOTORS LIMITED") == "Tata Motors Limited"
    assert display_case("HDFC BANK LTD") == "HDFC Bank Ltd"
    assert display_case("Apple Inc.") == "Apple Inc."
    assert short_name("HDFC Bank Ltd.") == "HDFC Bank"
    assert short_name("NVIDIA CORP") == "NVIDIA"


def test_universe_names_readable_with_industry_and_short_aliases() -> None:
    catalog = UniverseCompanyCatalog(
        InMemoryCompanyCatalog(),
        [
            _Src(BseEquityUniverseSource(None), BSE),  # type: ignore[arg-type]
            _Src(SecTickerUniverseSource(None), SEC),  # type: ignore[arg-type]
        ],
    )
    tmpv = catalog.find_by_alias(company_match_key("Tata Motors"), country=CountryCode("IN"))
    assert tmpv[0].display_name == "Tata Motors Passenger Vehicles Ltd"
    assert tmpv[0].industry == "Automobiles"
    bank = catalog.find_by_alias(company_match_key("KXYZ Bank"), country=CountryCode("IN"))
    assert [c.display_name for c in bank] == ["KXYZ Bank Ltd."]
    nv = catalog.find_by_alias(company_match_key("Nvidia"), country=CountryCode("US"))
    assert [c.display_name for c in nv] == ["Nvidia Corp"]
