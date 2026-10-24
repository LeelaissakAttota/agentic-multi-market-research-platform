from __future__ import annotations

from typing import Any

from amrp.domain.financial.calculations import compute_financial_metrics_result
from amrp.infrastructure.financial.sec_company_facts import (
    SecCompanyFactsFinancialDataAdapter,
)
from tests.unit.market_fixtures import APPLE_COMPANY_ID


def _row(val: float, start: str | None, end: str, fy: int, filed: str) -> dict[str, Any]:
    row: dict[str, Any] = {"val": val, "end": end, "fy": fy, "fp": "FY", "form": "10-K"}
    row["filed"] = filed
    row["accn"] = f"0000320193-{fy % 100:02d}-000001"
    if start:
        row["start"] = start
    return row


def _dur(cur: float, prior: float, *, quarter: float | None = None) -> list[dict[str, Any]]:
    rows = [
        _row(cur, "2024-09-29", "2025-09-27", 2025, "2025-10-31"),
        _row(prior, "2023-10-01", "2024-09-28", 2025, "2025-10-31"),
        _row(prior, "2023-10-01", "2024-09-28", 2024, "2024-11-01"),
    ]
    if quarter is not None:  # a 3-month row that must never be chosen as annual
        rows.append(_row(quarter, "2025-06-29", "2025-09-27", 2025, "2025-11-01"))
    return rows


def _inst(cur: float) -> list[dict[str, Any]]:
    return [_row(cur, None, "2025-09-27", 2025, "2025-10-31")]


def _payload() -> dict[str, Any]:
    def usd(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"units": {"USD": rows}}

    return {
        "facts": {
            "us-gaap": {
                # deprecated alias with only stale FY2018 data
                "Revenues": usd([_row(265.0, "2017-10-01", "2018-09-29", 2018, "2018-11-05")]),
                "RevenueFromContractWithCustomerExcludingAssessedTax": usd(
                    _dur(416.0, 391.0, quarter=102.0)
                ),
                "NetIncomeLoss": usd(_dur(112.0, 94.0)),
                "GrossProfit": usd(_dur(195.0, 180.0)),
                "OperatingIncomeLoss": usd(_dur(133.0, 123.0)),
                "NetCashProvidedByUsedInOperatingActivities": usd(_dur(111.0, 118.0)),
                "PaymentsToAcquirePropertyPlantAndEquipment": usd(_dur(12.0, 9.0)),
                "AssetsCurrent": usd(_inst(148.0)),
                "LiabilitiesCurrent": usd(_inst(165.0)),
                "StockholdersEquity": usd(_inst(74.0)),
                "LongTermDebt": usd(_inst(91.0)),
            }
        }
    }


def _metrics() -> dict[str, str]:
    adapter = SecCompanyFactsFinancialDataAdapter(None)  # type: ignore[arg-type]
    pkg = adapter._parse_package(APPLE_COMPANY_ID, _payload(), fiscal_year=None, cik="0000320193")
    assert pkg is not None
    assert pkg.reporting_period.label == "FY2025"
    assert pkg.prior_period_package is not None
    assert pkg.prior_period_package.reporting_period.label == "FY2024"
    result = compute_financial_metrics_result(pkg)
    assert result.omissions == ()
    return {m.name.value: str(m.value) for m in result.metrics}


def test_newest_alias_wins_over_deprecated_tag_and_all_metrics_compute() -> None:
    metrics = _metrics()
    assert metrics["gross_margin"].startswith("0.46875")  # 195/416, not 265 (FY2018)
    assert metrics["revenue_growth"].startswith("0.0639")  # 416/391-1
    assert metrics["free_cash_flow"].startswith("99")  # 111-12
    assert "debt_to_equity" in metrics


def test_complete_package_is_available_and_missing_cash_flow_is_partial() -> None:
    from amrp.domain.financial import FinancialDataAvailability

    adapter = SecCompanyFactsFinancialDataAdapter(None)  # type: ignore[arg-type]
    full = adapter._parse_package(APPLE_COMPANY_ID, _payload(), fiscal_year=None, cik="0000320193")
    assert full is not None and full.availability is FinancialDataAvailability.AVAILABLE
    payload = _payload()
    del payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]
    del payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    partial = adapter._parse_package(APPLE_COMPANY_ID, payload, fiscal_year=None, cik="0000320193")
    assert partial is not None and partial.availability is FinancialDataAvailability.PARTIAL
