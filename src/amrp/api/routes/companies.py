"""Company resolution HTTP routes.

HTTP semantics (frozen for Phase 2):

- RESOLVED / AMBIGUOUS / NOT_FOUND → ``200`` with structured body
- INVALID application query → ``400`` with stable error envelope
- Framework parameter validation (e.g. oversized ``country``) → ``422``

Ambiguity and not-found are successful protocol outcomes carrying identity
status, not server failures.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from amrp.api.errors import build_error_response
from amrp.application.company_resolution import (
    QUERY_MAX_LENGTH,
    CompanyQuery,
    ResolutionStatus,
)
from amrp.composition import AppContainer
from amrp.domain.identity import CountryCode, ExchangeCode, TickerSymbol
from amrp.observability.logging import get_logger

router = APIRouter(tags=["companies"])
logger = get_logger("amrp.api.companies")


class CompanyResolveResponse(BaseModel):
    """Structured company-resolution response contract."""

    query: dict[str, Any]
    status: str
    matched_by: str
    confidence: str
    normalized_query: str = ""
    message: str = ""
    company: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)


def _container(request: Request) -> AppContainer:
    return cast(AppContainer, request.app.state.container)


@router.get(
    "/companies/resolve",
    response_model=CompanyResolveResponse,
    responses={
        400: {"description": "Invalid company query"},
    },
)
def resolve_company(
    request: Request,
    q: str = Query(default="", max_length=QUERY_MAX_LENGTH),
    country: str | None = Query(default=None, max_length=2),
    exchange: str | None = Query(default=None, max_length=32),
    ticker: str | None = Query(default=None, max_length=32),
) -> CompanyResolveResponse | JSONResponse:
    """Resolve a company query against the local identity catalog."""

    container = _container(request)
    try:
        country_code = CountryCode(country) if country else None
        exchange_code = ExchangeCode(exchange) if exchange else None
        ticker_symbol = TickerSymbol(ticker) if ticker else None
    except ValueError as exc:
        logger.info(
            "company_resolution",
            extra={
                "resolution_status": ResolutionStatus.INVALID.value,
                "matched_by": "invalid_input",
                "candidate_count": 0,
                "query_length": len(q),
            },
        )
        return build_error_response(
            code="invalid_company_query",
            message=str(exc),
            correlation_id=str(getattr(request.state, "correlation_id", "") or ""),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    query = CompanyQuery(
        raw_query=q,
        country=country_code,
        exchange=exchange_code,
        ticker=ticker_symbol,
    )
    result = container.resolve_company.execute(query)
    logger.info(
        "company_resolution",
        extra={
            "resolution_status": result.status.value,
            "matched_by": result.matched_by.value,
            "candidate_count": len(result.candidates),
            "query_length": len(q),
        },
    )

    if result.status is ResolutionStatus.INVALID:
        return build_error_response(
            code="invalid_company_query",
            message=result.message or "invalid company query",
            correlation_id=str(getattr(request.state, "correlation_id", "") or ""),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return CompanyResolveResponse.model_validate(result.to_dict())
