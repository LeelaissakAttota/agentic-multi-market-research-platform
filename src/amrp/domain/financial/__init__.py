"""Phase 4 financial & filing intelligence domain package."""

from amrp.domain.financial.calculations import (
    CALCULATION_LIBRARY_VERSION,
    FinancialMetric,
    FinancialMetricName,
    FinancialMetricsResult,
    OmittedMetric,
    compute_financial_metrics_result,
    compute_standard_financial_metrics,
    free_cash_flow,
)
from amrp.domain.financial.concepts import FinancialConcept
from amrp.domain.financial.conflicts import (
    ConflictResolutionRule,
    FinancialFactConflict,
    detect_fact_conflicts,
    resolve_fact_conflict,
)
from amrp.domain.financial.facts import FinancialFact, build_fact
from amrp.domain.financial.filings import (
    FilingFormType,
    FilingId,
    FilingMetadata,
)
from amrp.domain.financial.missing import MissingDataSemantics
from amrp.domain.financial.periods import (
    PeriodBasis,
    PeriodIncomparabilityReason,
    ReportingPeriod,
)
from amrp.domain.financial.statements import (
    BalanceSheet,
    CashFlowStatement,
    CompanyFinancialPackage,
    FinancialDataAvailability,
    IncomeStatement,
)
from amrp.domain.financial.units import FinancialScale, FinancialUnit

__all__ = [
    "CALCULATION_LIBRARY_VERSION",
    "BalanceSheet",
    "CashFlowStatement",
    "CompanyFinancialPackage",
    "ConflictResolutionRule",
    "FilingFormType",
    "FilingId",
    "FilingMetadata",
    "FinancialConcept",
    "FinancialDataAvailability",
    "FinancialFact",
    "FinancialFactConflict",
    "FinancialMetric",
    "FinancialMetricName",
    "FinancialMetricsResult",
    "FinancialScale",
    "FinancialUnit",
    "IncomeStatement",
    "MissingDataSemantics",
    "OmittedMetric",
    "PeriodBasis",
    "PeriodIncomparabilityReason",
    "ReportingPeriod",
    "build_fact",
    "compute_financial_metrics_result",
    "compute_standard_financial_metrics",
    "detect_fact_conflicts",
    "free_cash_flow",
    "resolve_fact_conflict",
]
