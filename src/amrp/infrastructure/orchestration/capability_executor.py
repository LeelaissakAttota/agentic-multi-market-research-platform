"""Infrastructure adapter: map Phase 6 capability IDs to Phase 2-5 use cases."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from amrp.application.capability_result_adapters import (
    adapt_financial_result,
    adapt_industry_result,
    adapt_market_result,
    adapt_news_result,
    adapt_regulatory_result,
    adapt_resolution_result,
)
from amrp.application.company_resolution import CompanyQuery
from amrp.application.financial_contracts import FinancialSnapshotQuery
from amrp.application.financial_snapshot import GetFinancialSnapshot
from amrp.application.industry_contracts import IndustrySnapshotQuery
from amrp.application.industry_snapshot import GetIndustryContextSnapshot
from amrp.application.market_contracts import MarketSnapshotQuery
from amrp.application.market_snapshot import GetMarketSnapshot
from amrp.application.news_event_contracts import NewsEventSnapshotQuery
from amrp.application.news_event_snapshot import GetNewsEventSnapshot
from amrp.application.regulatory_contracts import RegulatorySnapshotQuery
from amrp.application.regulatory_snapshot import GetRegulatorySnapshot
from amrp.application.resolve_company import ResolveCompany
from amrp.domain.data_origin import DataOrigin
from amrp.domain.identity import CompanyIdentity
from amrp.domain.orchestration import (
    ResearchTask,
    TaskEvidenceRef,
    TaskExecutionResult,
    TaskResultStatus,
    TaskType,
)
from amrp.observability.logging import get_logger

logger = get_logger("amrp.infrastructure.orchestration.capability_executor")

# Fixed, safe, bounded client-facing message for an unexpected capability
# failure (F04). The real exception's type and message are logged server-side
# only (see execute_task) -- never interpolated into a client-facing field.
_SAFE_CAPABILITY_FAILURE_MESSAGE = "capability execution failed unexpectedly"


class Phase6CapabilityExecutor:
    """Orchestration bridge only - does not duplicate Phase 2-5 business logic."""

    def __init__(
        self,
        *,
        resolve_company: ResolveCompany,
        get_market_snapshot: GetMarketSnapshot,
        get_financial_snapshot: GetFinancialSnapshot,
        get_news_event_snapshot: GetNewsEventSnapshot,
        get_industry_snapshot: GetIndustryContextSnapshot,
        get_regulatory_snapshot: GetRegulatorySnapshot,
        live_context: Any | None = None,
    ) -> None:
        self._resolve_company = resolve_company
        self._market = get_market_snapshot
        self._financial = get_financial_snapshot
        self._news = get_news_event_snapshot
        self._industry = get_industry_snapshot
        self._regulatory = get_regulatory_snapshot
        # Optional free/no-key live fallback (Yahoo/SEC/news) used when the
        # fixture-backed industry/regulatory snapshot has nothing for a company.
        self._live = live_context

    def execute_task(
        self,
        task: ResearchTask,
        *,
        company: CompanyIdentity,
        company_query: CompanyQuery,
    ) -> TaskExecutionResult:
        try:
            return self._dispatch(task, company=company, company_query=company_query)
        except Exception as exc:
            # F04: the exception's type and message may contain internal
            # implementation details (paths, hostnames, provider internals)
            # and must never reach the client. Log them here, server-side
            # only, for diagnostics; return a fixed, safe, bounded message.
            logger.error(
                "capability_execution_failed",
                extra={
                    "task_id": task.task_id.as_text(),
                    "capability_id": task.capability_id,
                    "error_type": exc.__class__.__name__,
                    "error_detail": str(exc),
                },
            )
            return TaskExecutionResult(
                task_id=task.task_id,
                status=TaskResultStatus.FAILED,
                message=_SAFE_CAPABILITY_FAILURE_MESSAGE,
                retryable=True,
                error_code="executor_exception",
            )

    def _dispatch(
        self,
        task: ResearchTask,
        *,
        company: CompanyIdentity,
        company_query: CompanyQuery,
    ) -> TaskExecutionResult:
        expected = company.company_id
        capability = task.capability_id
        if capability == "company_resolution" or task.task_type is TaskType.COMPANY_RESOLUTION:
            resolution = self._resolve_company.execute(company_query)
            return adapt_resolution_result(
                task.task_id,
                expected=expected,
                resolution=resolution,
                company=company,
            )
        if capability == "market_intelligence" or task.task_type is TaskType.MARKET_INTELLIGENCE:
            market = self._market.execute(MarketSnapshotQuery(company_query=company_query))
            return adapt_market_result(task.task_id, expected=expected, result=market)
        if (
            capability == "financial_intelligence"
            or task.task_type is TaskType.FINANCIAL_INTELLIGENCE
        ):
            financial = self._financial.execute(FinancialSnapshotQuery(company_query=company_query))
            return adapt_financial_result(task.task_id, expected=expected, result=financial)
        if (
            capability == "news_event_intelligence"
            or task.task_type is TaskType.NEWS_EVENT_INTELLIGENCE
        ):
            news = self._news.execute(NewsEventSnapshotQuery(company_query=company_query))
            return adapt_news_result(task.task_id, expected=expected, result=news)
        if (
            capability == "industry_intelligence"
            or task.task_type is TaskType.INDUSTRY_INTELLIGENCE
        ):
            industry = self._industry.execute(IndustrySnapshotQuery(company_query=company_query))
            adapted = adapt_industry_result(task.task_id, expected=expected, result=industry)
            return self._live_fallback(task, adapted, company, company_query, kind="industry")
        if (
            capability == "regulatory_intelligence"
            or task.task_type is TaskType.REGULATORY_INTELLIGENCE
        ):
            regulatory = self._regulatory.execute(
                RegulatorySnapshotQuery(company_query=company_query)
            )
            adapted = adapt_regulatory_result(task.task_id, expected=expected, result=regulatory)
            return self._live_fallback(task, adapted, company, company_query, kind="regulatory")
        return TaskExecutionResult(
            task_id=task.task_id,
            status=TaskResultStatus.FAILED,
            message=f"unknown capability_id={capability}",
            retryable=False,
            error_code="unknown_capability",
        )

    def _live_fallback(
        self,
        task: ResearchTask,
        adapted: TaskExecutionResult,
        company: CompanyIdentity,
        company_query: CompanyQuery,
        *,
        kind: str,
    ) -> TaskExecutionResult:
        """Replace an unavailable/failed fixture result with free live data when possible."""

        if self._live is None or adapted.status in {
            TaskResultStatus.SUCCESS,
            TaskResultStatus.PARTIAL,
            TaskResultStatus.BLOCKED,
        }:
            return adapted
        exchange = company_query.exchange.as_text() if company_query.exchange else None
        ticker = company_query.ticker.as_text() if company_query.ticker else ""
        if not ticker:
            for security in company.securities:
                for listing in security.listings:
                    exchange, ticker = listing.exchange.as_text(), listing.ticker.as_text()
                    break
                if ticker:
                    break
        if not ticker:
            return adapted
        name = getattr(company, "legal_name", None) or getattr(company, "display_name", "") or ""
        try:
            if kind == "industry":
                data = self._live.industry(exchange=exchange, ticker=ticker, name=str(name))
            else:
                data = self._live.regulatory(exchange=exchange, ticker=ticker, company=company)
        except Exception as exc:  # live fallback is best effort
            logger.warning(
                "live_fallback_failed",
                extra={"kind": kind, "error_type": exc.__class__.__name__},
            )
            return adapted
        if not isinstance(data, dict) or data.get("status") != "ok":
            return adapted
        count = len(data.get("items") or []) + len(data.get("figures") or [])
        return TaskExecutionResult(
            task_id=task.task_id,
            status=TaskResultStatus.SUCCESS,
            message=f"{kind} from free live sources: {data.get('message') or count}"[:1000],
            evidence_refs=(
                TaskEvidenceRef(
                    company_id=company.company_id,
                    data_origin=DataOrigin.LIVE,
                    retrieved_at=datetime.now(UTC),
                ),
            ),
            output_summary=f"{kind} status=ok_live provider={data.get('provider_name')} items={count}",
        )
