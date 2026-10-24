"""Deterministic FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from amrp.api.errors import register_exception_handlers
from amrp.api.middleware import CorrelationIdMiddleware, RequestSafetyMiddleware
from amrp.api.routes import (
    companies,
    financials,
    health,
    industry,
    market,
    news,
    regulatory,
    research,
    synthesis,
    watchlists,
    workflows,
)
from amrp.api.versioning import (
    VERSIONED_API_PREFIX,
    install_openapi_version_policy,
)
from amrp.composition import AppContainer, build_container
from amrp.config.settings import Settings
from amrp.observability.logging import configure_logging, get_logger
from amrp.security.auth import require_api_key
from amrp.security.headers import SecurityHeadersMiddleware

# F10: environments in which Settings requires (and validates at startup) an
# explicit, non-wildcard ALLOWED_HOSTS (config/settings.py's model_validator
# uses this identical pair). Host-allowlist enforcement must match that same
# set, or a hardened-looking staging configuration silently goes unenforced.
_HOST_ENFORCEMENT_ENVIRONMENTS = frozenset({"production", "staging"})


def create_app(
    settings: Settings | None = None,
    container: AppContainer | None = None,
) -> FastAPI:
    """Create a FastAPI application with explicit composition and lifecycle."""

    resolved_container = container if container is not None else build_container(settings)
    configure_logging(
        resolved_container.settings.log_level,
        log_format=resolved_container.settings.log_format,
    )
    logger = get_logger("amrp.api")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Extension points for PostgreSQL, Redis, live providers, and research
        # services belong here in later phases. Phase 3 uses fixture market data.
        logger.info(
            "application_startup",
            extra=resolved_container.settings.safe_log_context(),
        )
        yield
        logger.info("application_shutdown")

    application = FastAPI(
        title="AMRP - Agentic Multi-Market Research Platform",
        version=resolved_container.metadata.version,
        lifespan=lifespan,
        # Swagger UI / ReDoc: on by default in development (API_DOCS_ENABLED).
        docs_url="/docs" if resolved_container.settings.api_docs_active() else None,
        redoc_url="/redoc" if resolved_container.settings.api_docs_active() else None,
        openapi_url="/openapi.json",
    )
    application.state.container = resolved_container
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(CorrelationIdMiddleware)
    application.add_middleware(
        RequestSafetyMiddleware,
        max_body_bytes=resolved_container.settings.api_max_request_body_bytes,
        allowed_hosts=resolved_container.settings.allowed_host_values(),
        enforce_allowed_hosts=resolved_container.settings.app_env in _HOST_ENFORCEMENT_ENVIRONMENTS,
    )
    register_exception_handlers(application)
    # Public endpoints — no authentication dependency.
    application.include_router(health.router)
    # Protected domain endpoints — require_api_key enforced in production/staging.
    _auth = [Depends(require_api_key)]
    application.include_router(companies.router, dependencies=_auth)
    application.include_router(market.router, dependencies=_auth)
    application.include_router(financials.router, dependencies=_auth)
    application.include_router(news.router, dependencies=_auth)
    application.include_router(industry.router, dependencies=_auth)
    application.include_router(regulatory.router, dependencies=_auth)
    application.include_router(research.router, dependencies=_auth)
    application.include_router(synthesis.router, dependencies=_auth)
    application.include_router(workflows.router, dependencies=_auth)
    application.include_router(watchlists.router, dependencies=_auth)
    # Versioned aliases — health stays public; companies and synthesis are protected.
    application.include_router(health.router, prefix=VERSIONED_API_PREFIX)
    application.include_router(companies.router, prefix=VERSIONED_API_PREFIX, dependencies=_auth)
    application.include_router(synthesis.router, prefix=VERSIONED_API_PREFIX, dependencies=_auth)
    install_openapi_version_policy(application)
    return application
