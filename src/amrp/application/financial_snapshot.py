"""GetFinancialSnapshot use case — Phase 4 financial analysis vertical slice."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from amrp.application.company_resolution import ResolutionResult, ResolutionStatus
from amrp.application.financial_contracts import (
    FinancialSnapshotQuery,
    FinancialSnapshotResult,
    FinancialSnapshotStatus,
    resolution_blocks_financials,
)
from amrp.application.ports import FinancialDataPort
from amrp.application.resolve_company import ResolveCompany
from amrp.domain.data_origin import DataOrigin
from amrp.domain.financial import (
    CompanyFinancialPackage,
    FinancialDataAvailability,
    compute_financial_metrics_result,
)


class GetFinancialSnapshot:
    """Resolve company identity safely, then load traceable financial fundamentals.

    Never attaches financial data to AMBIGUOUS / NOT_FOUND / INVALID companies.
    Never fabricates facts or metrics when the financial adapter has no package.

    Period selection: when ``fiscal_year`` is omitted, the adapter returns the
    latest available authoritative reporting period for the company.
    """

    def __init__(
        self,
        resolve_company: ResolveCompany,
        financial_data: FinancialDataPort,
        *,
        clock: Callable[[], datetime] | None = None,
        live_expected: bool = False,
    ) -> None:
        self._live_expected = live_expected
        self._resolve_company = resolve_company
        self._financial_data = financial_data
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, query: FinancialSnapshotQuery) -> FinancialSnapshotResult:
        evaluated_at = self._clock()
        if evaluated_at.tzinfo is None:
            msg = "clock must return timezone-aware datetime"
            raise ValueError(msg)
        resolution = self._resolve_company.execute(query.company_query)

        if resolution.status is ResolutionStatus.INVALID:
            return FinancialSnapshotResult(
                query=query,
                status=FinancialSnapshotStatus.INVALID,
                message=resolution.message or "invalid company query",
                resolution=resolution,
                evaluated_at=evaluated_at,
            )

        if resolution_blocks_financials(resolution.status):
            return FinancialSnapshotResult(
                query=query,
                status=FinancialSnapshotStatus.RESOLUTION_BLOCKED,
                message="financial data is withheld until company identity is uniquely resolved",
                resolution=resolution,
                evaluated_at=evaluated_at,
            )

        assert resolution.company is not None
        package = self._financial_data.get_financial_package(
            resolution.company.company_id,
            fiscal_year=query.fiscal_year,
        )
        if package is None:
            return FinancialSnapshotResult(
                query=query,
                status=FinancialSnapshotStatus.UNAVAILABLE,
                message="financial fundamentals are unavailable for the resolved company",
                resolution=resolution,
                evaluated_at=evaluated_at,
            )

        if package.company_id != resolution.company.company_id:
            return FinancialSnapshotResult(
                query=query,
                status=FinancialSnapshotStatus.UNAVAILABLE,
                message="financial adapter returned data for a different company",
                resolution=resolution,
                provider_name=package.provider_name,
                evaluated_at=evaluated_at,
            )

        return self._build_success(query, resolution, package, evaluated_at)

    def _build_success(
        self,
        query: FinancialSnapshotQuery,
        resolution: ResolutionResult,
        package: CompanyFinancialPackage,
        evaluated_at: datetime,
    ) -> FinancialSnapshotResult:
        metrics_result = compute_financial_metrics_result(package)
        availability = package.availability

        if availability is FinancialDataAvailability.PARTIAL:
            status = FinancialSnapshotStatus.PARTIAL
            message = "financial fundamentals are partial"
        elif availability is FinancialDataAvailability.DEGRADED:
            status = FinancialSnapshotStatus.DEGRADED
            message = "financial fundamentals returned in degraded mode"
        else:
            status = FinancialSnapshotStatus.OK
            message = "financial snapshot computed"
        if package.data_origin is DataOrigin.FIXTURE:
            # Honest labelling: a bundled sample must never look like live data.
            message = "FIXTURE/DEMO SAMPLE data (not live filings); " + message
            if self._live_expected and status is FinancialSnapshotStatus.OK:
                status = FinancialSnapshotStatus.DEGRADED
                message = (
                    "live fundamentals unavailable for this company; showing "
                    "FIXTURE/DEMO SAMPLE data (not live filings)"
                )

        return FinancialSnapshotResult(
            query=query,
            status=status,
            message=message,
            resolution=resolution,
            package=package,
            metrics=metrics_result.metrics,
            omissions=metrics_result.omissions,
            provider_name=package.provider_name,
            evaluated_at=evaluated_at,
        )
