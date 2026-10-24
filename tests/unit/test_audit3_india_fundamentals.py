from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from amrp.application.company_resolution import CompanyQuery
from amrp.application.financial_contracts import FinancialSnapshotQuery
from amrp.composition import build_container
from amrp.config.settings import Settings
from amrp.domain.data_origin import DataOrigin
from amrp.domain.financial.calculations import compute_financial_metrics_result
from amrp.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from amrp.infrastructure.financial.yahoo_fundamentals import (
    YahooFundamentalsFinancialDataAdapter,
)

VALUES = {
    "annualTotalRevenue": (2553.24, 2670.21),
    "annualNetIncome": (485.53, 492.10),
    "annualGrossProfit": (1096.41, 1204.18),
    "annualOperatingIncome": (622.93, 670.22),
    "annualOperatingCashFlow": (489.08, 520.94),
    "annualCapitalExpenditure": (-39.37, -41.46),
    "annualCurrentAssets": (1230.11, 1357.05),
    "annualCurrentLiabilities": (530.01, 609.14),
    "annualStockholdersEquity": (947.56, 1072.40),
    "annualTotalDebt": (93.92, 112.83),
}


def _payload() -> dict[str, Any]:
    result = []
    for name, (prior, cur) in VALUES.items():
        result.append(
            {
                "meta": {"type": [name]},
                name: [
                    {
                        "asOfDate": "2025-03-31",
                        "currencyCode": "INR",
                        "reportedValue": {"raw": prior},
                    },
                    {
                        "asOfDate": "2026-03-31",
                        "currencyCode": "INR",
                        "reportedValue": {"raw": cur},
                    },
                ],
            }
        )
    return {"timeseries": {"result": result}}


class _Http:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_json(self, url: str, **_: object) -> dict[str, Any]:
        self.urls.append(url)
        return _payload()


def _reliance():  # type: ignore[no-untyped-def]
    catalog = InMemoryCompanyCatalog()
    for company in catalog._companies:
        if any(lst.ticker.as_text() == "RELIANCE" for lst in company.all_listings()):
            return catalog, company.company_id
    raise AssertionError("reference catalog has Reliance")


def test_yahoo_fundamentals_parse_live_inr_package_with_prior_year() -> None:
    catalog, company_id = _reliance()
    http = _Http()
    adapter = YahooFundamentalsFinancialDataAdapter(
        http,  # type: ignore[arg-type]
        catalog,
        clock=lambda: datetime(2026, 10, 15, tzinfo=UTC),
    )
    pkg = adapter.get_financial_package(company_id)
    assert pkg is not None
    assert "RELIANCE.NS" in http.urls[0]
    assert pkg.data_origin is DataOrigin.LIVE and pkg.provider_name == "yahoo_fundamentals"
    assert pkg.currency.as_text() == "INR" and pkg.reporting_period.label == "FY2026"
    assert pkg.prior_period_package is not None
    metrics = {m.name.value: m.value for m in compute_financial_metrics_result(pkg).metrics}
    assert len(metrics) == 10  # growth, margins, ratios, FCF all computable
    assert metrics["free_cash_flow"] > 0  # capex sign normalised


def test_fixture_fundamentals_are_labelled_not_ok_when_live_expected() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    container = build_container(settings)
    result = container.get_financial_snapshot.execute(
        FinancialSnapshotQuery(company_query=CompanyQuery(raw_query="Reliance"))
    )
    assert result.status.value == "ok"
    assert "FIXTURE/DEMO SAMPLE" in result.message

    from amrp.application.financial_snapshot import GetFinancialSnapshot

    live = GetFinancialSnapshot(
        container.resolve_company, container.financial_data, live_expected=True
    )
    degraded = live.execute(
        FinancialSnapshotQuery(company_query=CompanyQuery(raw_query="Reliance"))
    )
    assert degraded.status.value == "degraded"
    assert "FIXTURE/DEMO SAMPLE" in degraded.message
