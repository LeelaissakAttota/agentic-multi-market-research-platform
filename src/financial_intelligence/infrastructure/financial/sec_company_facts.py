"""Optional SEC EDGAR company-facts HTTP adapter (Tier-1 structured data).

Uses the public ``data.sec.gov`` XBRL companyfacts JSON endpoint. No API key.
Automated tests must inject a fake transport — CI never depends on live SEC.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from financial_intelligence.domain.data_origin import DataOrigin
from financial_intelligence.domain.financial import (
    BalanceSheet,
    CashFlowStatement,
    CompanyFinancialPackage,
    FilingFormType,
    FilingId,
    FilingMetadata,
    FinancialConcept,
    FinancialDataAvailability,
    FinancialFact,
    FinancialScale,
    FinancialUnit,
    IncomeStatement,
    PeriodBasis,
    ReportingPeriod,
    build_fact,
    detect_fact_conflicts,
)
from financial_intelligence.domain.identity import CompanyId, CurrencyCode
from financial_intelligence.domain.sources import SourceAuthorityTier, SourceId
from financial_intelligence.infrastructure.financial.concept_mapping import map_us_gaap
from financial_intelligence.infrastructure.financial.sec_cik_mapping import sec_cik_for_company
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpTransportError
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from financial_intelligence.observability.logging import get_logger

logger = get_logger("financial_intelligence.infrastructure.financial.sec_company_facts")

_SEC_COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts/"
_SEC_ALLOWED_HOST = "data.sec.gov"

# Duration (income) vs instant (balance) concept tags.
_DURATION_TAGS: tuple[tuple[str, FinancialConcept], ...] = (
    ("Revenues", FinancialConcept.REVENUE),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", FinancialConcept.REVENUE),
    ("NetIncomeLoss", FinancialConcept.NET_INCOME),
    ("GrossProfit", FinancialConcept.GROSS_PROFIT),
    ("OperatingIncomeLoss", FinancialConcept.OPERATING_INCOME),
    ("NetCashProvidedByUsedInOperatingActivities", FinancialConcept.OPERATING_CASH_FLOW),
    ("PaymentsToAcquirePropertyPlantAndEquipment", FinancialConcept.CAPITAL_EXPENDITURE),
)
_CASH_FLOW_PACKAGE_CONCEPTS = frozenset(
    {FinancialConcept.OPERATING_CASH_FLOW, FinancialConcept.CAPITAL_EXPENDITURE}
)
#: Annual duration window (days) — excludes quarterly rows inside 10-K data.
_ANNUAL_MIN_DAYS = 330
_ANNUAL_MAX_DAYS = 400
_INSTANT_TAGS: tuple[tuple[str, FinancialConcept], ...] = (
    ("Assets", FinancialConcept.TOTAL_ASSETS),
    ("AssetsCurrent", FinancialConcept.CURRENT_ASSETS),
    ("LiabilitiesCurrent", FinancialConcept.CURRENT_LIABILITIES),
    ("StockholdersEquity", FinancialConcept.SHAREHOLDERS_EQUITY),
    ("LongTermDebt", FinancialConcept.TOTAL_DEBT),
    ("LongTermDebtNoncurrent", FinancialConcept.TOTAL_DEBT),
    ("CashAndCashEquivalentsAtCarryingValue", FinancialConcept.CASH),
)


class SecCompanyFactsFinancialDataAdapter:
    """Live FinancialDataPort adapter for SEC companyfacts JSON."""

    provider_name = "sec_company_facts"

    def __init__(
        self,
        http: BoundedHttpClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._clock = clock or (lambda: datetime.now(UTC))
        PROVIDER_HEALTH.register(self.provider_name)

    def get_financial_package(
        self,
        company_id: CompanyId,
        *,
        fiscal_year: int | None = None,
    ) -> CompanyFinancialPackage | None:
        cik = sec_cik_for_company(company_id)
        if cik is None:
            return None
        url = f"{_SEC_COMPANYFACTS_BASE}CIK{cik}.json"
        if not self._is_allowlisted_url(url):
            logger.warning(
                "sec_company_facts_url_rejected",
                extra={"company_id": company_id.as_text()},
            )
            return None
        try:
            payload = self._http.get_json(url)
        except HttpTransportError as exc:
            PROVIDER_HEALTH.record_failure(self.provider_name, exc.kind.value, exc.status_code)
            logger.warning(
                "sec_company_facts_failed",
                extra={
                    "company_id": company_id.as_text(),
                    "status": exc.status_code,
                    "error_type": type(exc).__name__,
                    "failure_kind": exc.kind.value,
                },
            )
            return None
        except Exception as exc:
            logger.warning(
                "sec_company_facts_error",
                extra={
                    "company_id": company_id.as_text(),
                    "error_type": type(exc).__name__,
                },
            )
            return None
        if not isinstance(payload, dict):
            PROVIDER_HEALTH.record_failure(self.provider_name, "invalid_payload")
            return None
        PROVIDER_HEALTH.record_success(self.provider_name)
        return self._parse_package(company_id, payload, fiscal_year=fiscal_year, cik=cik)

    @staticmethod
    def _is_allowlisted_url(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == _SEC_ALLOWED_HOST
            and parsed.path.startswith("/api/xbrl/companyfacts/")
        )

    def _parse_package(
        self,
        company_id: CompanyId,
        payload: dict[str, Any],
        *,
        fiscal_year: int | None,
        cik: str,
        target_end: date | None = None,
        with_prior: bool = True,
        label_year: int | None = None,
    ) -> CompanyFinancialPackage | None:
        facts_root = payload.get("facts", {})
        if not isinstance(facts_root, dict):
            return None
        us_gaap = facts_root.get("us-gaap", {})
        if not isinstance(us_gaap, dict) or not us_gaap:
            return None

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=UTC)

        source_id = SourceId.new()
        filing_id = FilingId.new()
        currency = CurrencyCode("USD")
        fact_rows: list[FinancialFact] = []
        seen_concepts: set[FinancialConcept] = set()
        # Authoritative SEC 'filed' (acceptance) date per fact, keyed by fact identity —
        # never inferred from period_end. See _parse_filed_date / _select_fy_row.
        filed_dates: dict[int, date | None] = {}
        # Authoritative SEC accession number ('accn') per fact, keyed by fact identity —
        # the filing-specific identifier, never the company-level CIK. See F12.
        accession_numbers: dict[int, str | None] = {}

        # Newest observation per concept across ALL alias tags (a deprecated tag such
        # as Apple's ``Revenues`` may only carry FY2018 data and must not win).
        best: dict[FinancialConcept, tuple[FinancialFact, date | None, str | None]] = {}
        for tags, require_start in ((_DURATION_TAGS, True), (_INSTANT_TAGS, False)):
            for tag, default_concept in tags:
                extracted = self._extract_fact(
                    us_gaap=us_gaap,
                    tag=tag,
                    default_concept=default_concept,
                    company_id=company_id,
                    fiscal_year=fiscal_year,
                    source_id=source_id,
                    filing_id=filing_id,
                    retrieved_at=retrieved_at,
                    currency=currency,
                    require_start=require_start,
                    period_end=target_end,
                    label_year=label_year,
                )
                if extracted is None:
                    continue
                fact = extracted[0]
                current = best.get(fact.concept)
                if current is None or (
                    fact.period.period_end,
                    extracted[1] or date.min,
                ) > (current[0].period.period_end, current[1] or date.min):
                    best[fact.concept] = extracted
        for fact, filed_at, accession_number in best.values():
            fact_rows.append(fact)
            filed_dates[id(fact)] = filed_at
            accession_numbers[id(fact)] = accession_number
        del seen_concepts

        if not fact_rows:
            return None

        survivors, conflicts = detect_fact_conflicts(fact_rows)

        # Prefer a duration FY period for the package when available.
        duration_facts = [f for f in survivors if f.period.basis is PeriodBasis.FISCAL_YEAR]
        if not duration_facts:
            return None
        reporting_period = max(
            (f.period for f in duration_facts),
            key=lambda p: p.selection_key(),
        )
        # Authoritative SEC filing date and accession number for the selected period —
        # both drawn from the SAME fact (the one carrying the latest 'filed' value among
        # facts reporting that period), so filed_at and accession_or_reference always
        # describe one underlying filing, never mixed across facts. None when the source
        # provides no parseable value; filed_at never falls back to period_end and
        # accession_or_reference never falls back to the company CIK (see F12).
        period_facts = [f for f in duration_facts if f.period == reporting_period]
        dated_period_facts: list[tuple[date, FinancialFact]] = [
            (dated, f) for f in period_facts if (dated := filed_dates.get(id(f))) is not None
        ]
        filing_filed_at: date | None = None
        filing_accession_number: str | None = None
        if dated_period_facts:
            _, selected_fact = max(dated_period_facts, key=lambda pair: pair[0])
            filing_filed_at = filed_dates.get(id(selected_fact))
            filing_accession_number = accession_numbers.get(id(selected_fact))
        # Keep only facts whose fiscal year matches the selected package period.
        aligned = [
            f
            for f in survivors
            if f.period.fiscal_year == reporting_period.fiscal_year
            and (
                f.period == reporting_period
                or (
                    f.period.basis is PeriodBasis.INSTANT
                    and f.period.period_end == reporting_period.period_end
                )
            )
        ]
        if not aligned:
            return None

        income_concepts = {
            FinancialConcept.REVENUE,
            FinancialConcept.NET_INCOME,
            FinancialConcept.GROSS_PROFIT,
            FinancialConcept.OPERATING_INCOME,
        }
        income_facts = tuple(
            f for f in aligned if f.concept in income_concepts and f.period == reporting_period
        )
        cash_flow_facts = tuple(
            f
            for f in aligned
            if f.concept in _CASH_FLOW_PACKAGE_CONCEPTS and f.period == reporting_period
        )
        if not income_facts:
            return None

        balance_period = ReportingPeriod(
            basis=PeriodBasis.INSTANT,
            fiscal_year=reporting_period.fiscal_year,
            period_end=reporting_period.period_end,
            as_of=reporting_period.period_end,
            label=f"BS{reporting_period.fiscal_year}",
        )
        balance_concepts = {
            FinancialConcept.TOTAL_ASSETS,
            FinancialConcept.CURRENT_ASSETS,
            FinancialConcept.CURRENT_LIABILITIES,
            FinancialConcept.SHAREHOLDERS_EQUITY,
            FinancialConcept.TOTAL_DEBT,
            FinancialConcept.CASH,
        }
        balance_facts = tuple(
            build_fact(
                company_id=f.company_id,
                concept=f.concept,
                period=balance_period,
                raw_value=f.raw_value,
                unit=f.unit,
                scale=f.scale,
                source_id=f.source_id,
                authority_tier=f.authority_tier,
                retrieved_at=f.retrieved_at,
                currency=f.currency,
                filing_id=f.filing_id,
                provider_concept=f.provider_concept,
            )
            for f in aligned
            if f.concept in balance_concepts
        )

        filing = FilingMetadata(
            filing_id=filing_id,
            company_id=company_id,
            form_type=FilingFormType.US_10K,
            reporting_period=reporting_period,
            source_id=source_id,
            authority_tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
            filed_at=filing_filed_at,
            published_at=filing_filed_at,
            retrieved_at=retrieved_at,
            # The filing-specific SEC accession number (e.g. "0000320193-24-000123"),
            # never the company-level CIK — a CIK cannot identify one specific filing.
            # None (not the CIK) when the source provides no parseable accession (F12).
            accession_or_reference=filing_accession_number,
            source_url=f"{_SEC_COMPANYFACTS_BASE}CIK{cik}.json",
            provider_name=self.provider_name,
        )
        income = IncomeStatement(
            company_id=company_id,
            period=reporting_period,
            currency=currency,
            facts=income_facts,
        )
        balance = (
            BalanceSheet(
                company_id=company_id,
                period=balance_period,
                currency=currency,
                facts=balance_facts,
            )
            if balance_facts
            else None
        )
        cash_flow = (
            CashFlowStatement(
                company_id=company_id,
                period=reporting_period,
                currency=currency,
                facts=cash_flow_facts,
            )
            if cash_flow_facts
            else None
        )
        prior_package: CompanyFinancialPackage | None = None
        if with_prior:
            prior_end = self._prior_annual_end(us_gaap, reporting_period.period_end)
            if prior_end is not None:
                prior_package = self._parse_package(
                    company_id,
                    payload,
                    fiscal_year=None,
                    cik=cik,
                    target_end=prior_end,
                    with_prior=False,
                    label_year=reporting_period.fiscal_year - 1,
                )
        return CompanyFinancialPackage(
            company_id=company_id,
            reporting_period=reporting_period,
            currency=currency,
            retrieved_at=retrieved_at,
            income_statement=income,
            balance_sheet=balance,
            cash_flow_statement=cash_flow,
            prior_period_package=prior_package,
            filing=filing,
            provider_name=self.provider_name,
            # AVAILABLE only when every statement the metrics need is present
            # (income with revenue + net income, balance sheet, cash flow, prior
            # year); anything less stays PARTIAL so gaps are never hidden.
            availability=(
                FinancialDataAvailability.AVAILABLE
                if {FinancialConcept.REVENUE, FinancialConcept.NET_INCOME}
                <= {f.concept for f in income_facts}
                and balance is not None
                and cash_flow is not None
                and (prior_package is not None or not with_prior)
                else FinancialDataAvailability.PARTIAL
            ),
            data_origin=DataOrigin.LIVE,
            conflicts=conflicts,
        )

    def _extract_fact(
        self,
        *,
        us_gaap: dict[str, Any],
        tag: str,
        default_concept: FinancialConcept,
        company_id: CompanyId,
        fiscal_year: int | None,
        source_id: SourceId,
        filing_id: FilingId,
        retrieved_at: datetime,
        currency: CurrencyCode,
        require_start: bool,
        period_end: date | None = None,
        label_year: int | None = None,
    ) -> tuple[FinancialFact, date | None, str | None] | None:
        mapped = map_us_gaap(tag) or default_concept
        entry = us_gaap.get(tag)
        if not isinstance(entry, dict):
            return None
        units = entry.get("units", {})
        if not isinstance(units, dict):
            return None
        usd_rows = units.get("USD")
        if not isinstance(usd_rows, list):
            # Unsupported / non-USD units are skipped (no fabricated conversion).
            return None
        fy_row = self._select_fy_row(
            usd_rows,
            fiscal_year=fiscal_year,
            require_start=require_start,
            period_end=period_end,
        )
        if fy_row is None:
            return None
        val = fy_row.get("val")
        if val is None:
            return None
        try:
            normalized = Decimal(str(val))
        except (InvalidOperation, ValueError):
            return None
        if normalized.is_nan() or normalized.is_infinite():
            return None
        end_raw = fy_row.get("end")
        if not isinstance(end_raw, str):
            return None
        try:
            period_end = date.fromisoformat(end_raw)
            # SEC "fy" is the FILING's fiscal year (comparatives inside a FY2025
            # 10-K also say fy=2025); prior-period extraction overrides it.
            fy = label_year if label_year is not None else int(fy_row.get("fy", period_end.year))
        except (TypeError, ValueError):
            return None
        start_raw = fy_row.get("start")
        if require_start:
            if not isinstance(start_raw, str):
                return None
            try:
                period_start = date.fromisoformat(start_raw)
            except ValueError:
                return None
            period = ReportingPeriod(
                basis=PeriodBasis.FISCAL_YEAR,
                fiscal_year=fy,
                period_start=period_start,
                period_end=period_end,
                label=f"FY{fy}",
            )
        else:
            period = ReportingPeriod(
                basis=PeriodBasis.INSTANT,
                fiscal_year=fy,
                period_end=period_end,
                as_of=period_end,
                label=f"BS{fy}",
            )
        fact = build_fact(
            company_id=company_id,
            concept=mapped,
            period=period,
            raw_value=normalized,
            unit=FinancialUnit.CURRENCY,
            scale=FinancialScale.ONES,
            source_id=source_id,
            authority_tier=SourceAuthorityTier.TIER_1_AUTHORITATIVE,
            retrieved_at=retrieved_at,
            currency=currency,
            filing_id=filing_id,
            provider_concept=tag,
        )
        return (
            fact,
            self._parse_filed_date(fy_row.get("filed")),
            self._parse_accession(fy_row.get("accn")),
        )

    @staticmethod
    def _parse_filed_date(raw: object) -> date | None:
        """Parse the SEC XBRL 'filed' (acceptance) date.

        Never falls back to period_end or any other field — a missing or
        malformed value must surface as None, not a fabricated date.
        """

        if not isinstance(raw, str):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _parse_accession(raw: object) -> str | None:
        """Parse the SEC XBRL 'accn' (accession number) for this specific filing.

        This is the filing-level identifier (e.g. "0000320193-24-000123"), distinct
        from the company-level CIK. Never falls back to the CIK or any other field —
        a missing or malformed value must surface as None, not a fabricated or
        borrowed identifier (F12).
        """

        if not isinstance(raw, str):
            return None
        cleaned = raw.strip()
        return cleaned or None

    @staticmethod
    def _select_fy_row(
        rows: list[Any],
        *,
        fiscal_year: int | None,
        require_start: bool,
        period_end: date | None = None,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            fp = row.get("fp")
            if fp != "FY":
                continue
            if require_start and not isinstance(row.get("start"), str):
                continue
            if not isinstance(row.get("end"), str):
                continue
            if row.get("val") is None:
                continue
            if period_end is not None and row.get("end") != period_end.isoformat():
                continue
            if require_start and not _is_annual(row):
                continue
            candidates.append(row)
        if not candidates:
            return None
        if fiscal_year is not None:
            year_matches = []
            for row in candidates:
                try:
                    if int(row.get("fy", 0)) == fiscal_year:
                        year_matches.append(row)
                except (TypeError, ValueError):
                    continue
            candidates = year_matches
            if not candidates:
                return None
        # Prefer later filed/amended observation when present; never invent values.
        return max(
            candidates,
            key=lambda r: (
                str(r.get("filed", "")),
                str(r.get("end", "")),
                int(r.get("fy", 0)) if str(r.get("fy", "")).isdigit() else 0,
            ),
        )

    @staticmethod
    def _prior_annual_end(us_gaap: dict[str, Any], current_end: date) -> date | None:
        """Latest annual period end roughly one year before ``current_end``."""

        ends: set[date] = set()
        for tag, concept in _DURATION_TAGS:
            if concept not in {FinancialConcept.REVENUE, FinancialConcept.NET_INCOME}:
                continue
            entry = us_gaap.get(tag)
            units = entry.get("units", {}) if isinstance(entry, dict) else {}
            rows = units.get("USD") if isinstance(units, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("fp") != "FY" or not _is_annual(row):
                    continue
                try:
                    end = date.fromisoformat(str(row.get("end")))
                except ValueError:
                    continue
                if 300 <= (current_end - end).days <= 430:
                    ends.add(end)
        return max(ends) if ends else None


def _is_annual(row: dict[str, Any]) -> bool:
    """True when a duration row spans roughly one fiscal year (or lacks a start)."""

    start, end = row.get("start"), row.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return True
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return False
    return _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS
