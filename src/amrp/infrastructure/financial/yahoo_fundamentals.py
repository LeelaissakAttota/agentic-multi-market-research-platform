"""Live India fundamentals from Yahoo Finance's public fundamentals timeseries.

No API key. Used only for Indian (NSE/BSE) companies, where no free official
structured XBRL endpoint exists (SEC companyfacts covers US filers only). Yahoo
is an unofficial aggregator, so facts are labelled Tier 2 (structured financial
aggregator), provider ``yahoo_fundamentals``, data origin LIVE. Values are the
consolidated annual figures Yahoo reports in the listing currency (INR).
Any transport/shape problem returns ``None`` so the caller falls back.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode

from amrp.application.ports import CompanyCatalogPort
from amrp.domain.data_origin import DataOrigin
from amrp.domain.financial import (
    BalanceSheet,
    CashFlowStatement,
    CompanyFinancialPackage,
    FinancialConcept,
    FinancialDataAvailability,
    FinancialFact,
    FinancialScale,
    FinancialUnit,
    IncomeStatement,
    PeriodBasis,
    ReportingPeriod,
    build_fact,
)
from amrp.domain.identity import CompanyId, CurrencyCode
from amrp.domain.sources import SourceAuthorityTier, SourceId
from amrp.infrastructure.http import BoundedHttpClient, HttpTransportError
from amrp.infrastructure.http.provider_health import PROVIDER_HEALTH
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.financial.yahoo_fundamentals")

PROVIDER = "yahoo_fundamentals"
_BASE = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
_TYPES: dict[str, FinancialConcept] = {
    "annualTotalRevenue": FinancialConcept.REVENUE,
    "annualGrossProfit": FinancialConcept.GROSS_PROFIT,
    "annualOperatingIncome": FinancialConcept.OPERATING_INCOME,
    "annualNetIncome": FinancialConcept.NET_INCOME,
    "annualOperatingCashFlow": FinancialConcept.OPERATING_CASH_FLOW,
    "annualCapitalExpenditure": FinancialConcept.CAPITAL_EXPENDITURE,
    "annualCurrentAssets": FinancialConcept.CURRENT_ASSETS,
    "annualCurrentLiabilities": FinancialConcept.CURRENT_LIABILITIES,
    "annualStockholdersEquity": FinancialConcept.SHAREHOLDERS_EQUITY,
    "annualTotalDebt": FinancialConcept.TOTAL_DEBT,
    "annualTotalAssets": FinancialConcept.TOTAL_ASSETS,
}
_INCOME = {
    FinancialConcept.REVENUE,
    FinancialConcept.GROSS_PROFIT,
    FinancialConcept.OPERATING_INCOME,
    FinancialConcept.NET_INCOME,
}
_CASH = {FinancialConcept.OPERATING_CASH_FLOW, FinancialConcept.CAPITAL_EXPENDITURE}
_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}


def _fiscal_year(end: date) -> int:
    """Indian FY convention: the year ending 31-Mar-2026 is FY2026."""

    return end.year


class YahooFundamentalsFinancialDataAdapter:
    """FinancialDataPort for Indian listings via Yahoo fundamentals timeseries."""

    provider_name = PROVIDER

    def __init__(
        self,
        http: BoundedHttpClient,
        catalog: CompanyCatalogPort,
        *,
        clock: Callable[[], datetime] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wall = wall_clock
        PROVIDER_HEALTH.register(PROVIDER)

    def _symbol(self, company_id: CompanyId) -> str | None:
        company = self._catalog.get_by_id(company_id)
        if company is None or company.country.as_text() != "IN":
            return None
        listings = sorted(
            company.all_listings(),
            key=lambda lst: (lst.exchange.as_text() != "NSE", not lst.is_primary),
        )
        for listing in listings:
            suffix = _SUFFIX.get(listing.exchange.as_text())
            if suffix:
                return listing.ticker.as_text() + suffix
        return None

    def get_financial_package(
        self,
        company_id: CompanyId,
        *,
        fiscal_year: int | None = None,
    ) -> CompanyFinancialPackage | None:
        symbol = self._symbol(company_id)
        if symbol is None:
            return None
        now = int(self._wall())
        query = urlencode(
            {"type": ",".join(_TYPES), "period1": now - 6 * 366 * 86400, "period2": now + 86400}
        )
        url = f"{_BASE}{quote(symbol)}?{query}"
        try:
            payload = self._http.get_json(url)
        except HttpTransportError as exc:
            PROVIDER_HEALTH.record_failure(PROVIDER, exc.kind.value)
            logger.warning(
                "yahoo_fundamentals_failed",
                extra={"symbol": symbol, "failure_kind": exc.kind.value},
            )
            return None
        try:
            package = self.parse(company_id, payload, fiscal_year=fiscal_year)
        except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
            PROVIDER_HEALTH.record_failure(PROVIDER, type(exc).__name__)
            logger.warning("yahoo_fundamentals_invalid", extra={"error_type": type(exc).__name__})
            return None
        if package is None:
            PROVIDER_HEALTH.record_failure(PROVIDER, "empty")
            return None
        PROVIDER_HEALTH.record_success(PROVIDER)
        return package

    # ------------------------------------------------------------ parsing

    def parse(
        self,
        company_id: CompanyId,
        payload: dict[str, Any],
        *,
        fiscal_year: int | None = None,
    ) -> CompanyFinancialPackage | None:
        results = payload.get("timeseries", {}).get("result") or []
        by_end: dict[date, dict[FinancialConcept, Decimal]] = {}
        currency_code = "INR"
        for result in results:
            if not isinstance(result, dict):
                continue
            types = (result.get("meta") or {}).get("type") or []
            if not types or types[0] not in _TYPES:
                continue
            concept = _TYPES[types[0]]
            for point in result.get(types[0]) or []:
                if not isinstance(point, dict):
                    continue
                raw = (point.get("reportedValue") or {}).get("raw")
                if raw is None or not isinstance(point.get("asOfDate"), str):
                    continue
                value = Decimal(str(raw))
                if not value.is_finite():
                    continue
                if concept is FinancialConcept.CAPITAL_EXPENDITURE:
                    value = abs(value)  # Yahoo reports capex as a negative outflow
                end = date.fromisoformat(point["asOfDate"])
                by_end.setdefault(end, {})[concept] = value
                currency_code = str(point.get("currencyCode") or currency_code)
        ends = sorted(
            (e for e, facts in by_end.items() if facts.keys() & {FinancialConcept.REVENUE}),
            reverse=True,
        )
        if fiscal_year is not None:
            ends = [e for e in ends if _fiscal_year(e) <= fiscal_year]
        if not ends:
            return None
        currency = CurrencyCode(currency_code)
        retrieved_at = self._clock()
        prior = (
            self._package(company_id, ends[1], by_end[ends[1]], currency, retrieved_at, None)
            if len(ends) > 1
            else None
        )
        return self._package(company_id, ends[0], by_end[ends[0]], currency, retrieved_at, prior)

    def _package(
        self,
        company_id: CompanyId,
        end: date,
        values: dict[FinancialConcept, Decimal],
        currency: CurrencyCode,
        retrieved_at: datetime,
        prior: CompanyFinancialPackage | None,
    ) -> CompanyFinancialPackage:
        fy = _fiscal_year(end)
        period = ReportingPeriod(
            basis=PeriodBasis.FISCAL_YEAR,
            fiscal_year=fy,
            period_start=end - timedelta(days=364),
            period_end=end,
            label=f"FY{fy}",
        )
        balance_period = ReportingPeriod(
            basis=PeriodBasis.INSTANT,
            fiscal_year=fy,
            period_end=end,
            as_of=end,
            label=f"BS{fy}",
        )
        source_id = SourceId.new()

        def fact(concept: FinancialConcept, p: ReportingPeriod) -> FinancialFact:
            return build_fact(
                company_id=company_id,
                concept=concept,
                period=p,
                raw_value=values[concept],
                unit=FinancialUnit.CURRENCY,
                scale=FinancialScale.ONES,
                source_id=source_id,
                authority_tier=SourceAuthorityTier.TIER_2_STRUCTURED_FINANCIAL,
                retrieved_at=retrieved_at,
                currency=currency,
                provider_concept=f"yahoo:{concept.value}",
            )

        income = tuple(fact(c, period) for c in values if c in _INCOME)
        cash = tuple(fact(c, period) for c in values if c in _CASH)
        balance = tuple(
            fact(c, balance_period) for c in values if c not in _INCOME and c not in _CASH
        )
        complete = len(values) >= 9
        return CompanyFinancialPackage(
            company_id=company_id,
            reporting_period=period,
            currency=currency,
            retrieved_at=retrieved_at,
            income_statement=IncomeStatement(
                company_id=company_id, period=period, currency=currency, facts=income
            )
            if income
            else None,
            balance_sheet=BalanceSheet(
                company_id=company_id, period=balance_period, currency=currency, facts=balance
            )
            if balance
            else None,
            cash_flow_statement=CashFlowStatement(
                company_id=company_id, period=period, currency=currency, facts=cash
            )
            if cash
            else None,
            prior_period_package=prior,
            provider_name=PROVIDER,
            availability=(
                FinancialDataAvailability.AVAILABLE
                if complete
                else FinancialDataAvailability.PARTIAL
            ),
            data_origin=DataOrigin.LIVE,
        )
