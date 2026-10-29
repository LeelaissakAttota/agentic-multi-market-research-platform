"""Composition root: wires concrete adapters to application ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from amrp import __version__
from amrp.application.approval_policy import DeterministicApprovalPolicy
from amrp.application.capability_registry import CapabilityRegistry
from amrp.application.contracts import (
    ApplicationMetadata,
    ReadinessCheckResult,
)
from amrp.application.create_research_plan import CreateResearchPlan
from amrp.application.create_research_workflow import CreateResearchWorkflow
from amrp.application.deterministic_planner import DeterministicPlanner
from amrp.application.execute_research_plan import ExecuteResearchPlan
from amrp.application.financial_snapshot import GetFinancialSnapshot
from amrp.application.generate_research_synthesis import GenerateResearchSynthesis
from amrp.application.industry_snapshot import GetIndustryContextSnapshot
from amrp.application.manage_research_workflow import ManageResearchWorkflow
from amrp.application.manage_watchlist import ManageWatchlist
from amrp.application.market_freshness import MarketFreshnessPolicy
from amrp.application.market_snapshot import GetMarketSnapshot
from amrp.application.news_event_snapshot import GetNewsEventSnapshot
from amrp.application.ports import (
    ApiKeyStorePort,
    CompanyCatalogPort,
    FinancialDataPort,
    IndustryContextPort,
    LlmRouterPort,
    MarketDataPort,
    NewsEventPort,
    NotificationPort,
    PlannerPort,
    RegulatoryEventPort,
    ResearchMemoryPort,
    ResearchWorkflowStorePort,
    WatchlistStorePort,
    WorkflowReportStorePort,
)
from amrp.application.readiness import ReadinessRegistry
from amrp.application.regulatory_snapshot import GetRegulatorySnapshot
from amrp.application.replan_round import Replanner
from amrp.application.report_findings import ReportFindingsCollector
from amrp.application.reporting_ports import ResearchReportGeneratorPort
from amrp.application.request_research_report import RequestResearchReport
from amrp.application.resolve_company import ResolveCompany
from amrp.application.verify_claims import VerifyClaimUseCase
from amrp.config.settings import Settings
from amrp.domain.orchestration import ResearchExecutionBudget
from amrp.domain.synthesis import DeterministicSynthesisAssembler
from amrp.domain.verification.engine import VerificationEngine
from amrp.infrastructure.auth import InMemoryApiKeyStore
from amrp.infrastructure.company import InMemoryCompanyCatalog
from amrp.infrastructure.company.universe import (
    BseEquityUniverseSource,
    SecTickerUniverseSource,
    UniverseCompanyCatalog,
    UniverseSource,
)
from amrp.infrastructure.financial import (
    CachingFinancialDataAdapter,
    FallbackFinancialDataAdapter,
    InMemoryFinancialDataAdapter,
    SecCompanyFactsFinancialDataAdapter,
)
from amrp.infrastructure.financial.india_filings import (
    IndiaCorporateAnnouncementsSource,
)
from amrp.infrastructure.financial.yahoo_fundamentals import (
    YahooFundamentalsFinancialDataAdapter,
)
from amrp.infrastructure.http import BoundedHttpClient, UrlLibHttpTransport
from amrp.infrastructure.industry import (
    CachingIndustryAdapter,
    InMemoryIndustryAdapter,
)
from amrp.infrastructure.llm import DisabledLlmRouterAdapter, OpenRouterAdapter
from amrp.infrastructure.llm.free_model_router import (
    AUTO_MODEL,
    AutoFreeModelRouter,
    FreeModelCatalog,
)
from amrp.infrastructure.market import (
    CachingMarketDataAdapter,
    FallbackMarketDataAdapter,
    InMemoryMarketDataAdapter,
    YahooChartMarketDataAdapter,
)
from amrp.infrastructure.market.alpha_vantage import (
    AlphaVantageMarketDataAdapter,
    DailyRequestBudget,
)
from amrp.infrastructure.market.india_quotes import (
    BROWSER_USER_AGENT,
    BseScripResolver,
    HostRateLimiter,
    IndiaExchangeQuoteClient,
    IndiaLiveQuoteMarketDataAdapter,
)
from amrp.infrastructure.mcp import SelectedMcpFacade
from amrp.infrastructure.memory import InMemoryResearchMemoryStore
from amrp.infrastructure.news import (
    CachingNewsEventAdapter,
    InMemoryNewsEventAdapter,
)
from amrp.infrastructure.news.finnhub import FinnhubSource
from amrp.infrastructure.news.live_events import (
    CompanyEventSource,
    FallbackNewsEventAdapter,
    LiveCompanyEventAdapter,
    rank_news_source,
)
from amrp.infrastructure.news.live_feeds import (
    GdeltDocSource,
    GoogleNewsRssSource,
    RssFeedSource,
    parse_ir_feed_setting,
)
from amrp.infrastructure.notification import InMemoryNotificationAdapter
from amrp.infrastructure.orchestration import (
    DeterministicPlannerAdapter,
    FallbackPlannerAdapter,
    LlmPlannerAdapter,
    Phase6CapabilityExecutor,
)
from amrp.infrastructure.regulatory import (
    CachingRegulatoryAdapter,
    InMemoryRegulatoryAdapter,
)
from amrp.infrastructure.reporting.live_context import (
    LIVE_CONTEXT_USER_AGENT,
    LiveCompanyContext,
)
from amrp.infrastructure.regulatory.live_feeds import (
    FallbackRegulatoryAdapter,
    LiveRegulatoryAdapter,
    RegulatoryFeedSource,
    SebiOrdersFeedSource,
    SecLitigationFeedSource,
)
from amrp.infrastructure.reporting import DeterministicResearchReportGenerator
from amrp.infrastructure.reporting.report_store import InMemoryWorkflowReportStore
from amrp.infrastructure.sqlite import (
    SqliteApiKeyStore,
    SqliteDatabase,
    SqliteResearchMemoryStore,
    SqliteResearchWorkflowStore,
    SqliteWatchlistStore,
    SqliteWorkflowReportStore,
)
from amrp.infrastructure.watchlist import InMemoryWatchlistStore
from amrp.infrastructure.workflow import InMemoryResearchWorkflowStore
from amrp.observability.logging import get_logger

# Environments in which `require_api_key` (security/auth.py) enforces
# authentication rather than bypassing it. Duplicated here (not imported)
# because `security.auth` imports `AppContainer` from this module, so a
# top-level import back into `security.auth` would be circular. Mirrors that
# module's `_ENFORCED_ENVIRONMENTS` and `Settings`'s own validator condition
# (config/settings.py) exactly; keep all three in sync if this ever changes.
_AUTH_ENFORCED_ENVIRONMENTS = frozenset({"production", "staging"})

# Phase 3: response-size bound for OpenRouter chat-completions calls. Not
# separately configurable (no OPENROUTER_MAX_RESPONSE_BYTES setting exists);
# matches the default already used for market/financial live adapters.
_OPENROUTER_MAX_RESPONSE_BYTES = 1_048_576
_composition_logger = get_logger("amrp.composition")


def _build_planner(
    settings: Settings,
    resolve_company: ResolveCompany,
    capability_registry: CapabilityRegistry,
    budget: ResearchExecutionBudget,
    llm_router: LlmRouterPort,
) -> PlannerPort:
    """Select and construct the planner based on explicit PLANNER_MODE setting.

    ``auto`` (default): LLM planner with automatic per-request deterministic
    fallback when a live LLM router is configured, else deterministic.
    ``llm``: LLM only â€” if prerequisites are missing, Settings validation fails
    at startup. ``deterministic``: never calls an LLM.
    """
    mode = settings.planner_mode
    deterministic = DeterministicPlannerAdapter(
        DeterministicPlanner(capability_registry, budget=budget),
        resolve_company,
    )
    if mode == "auto":
        if not _llm_live(settings, llm_router):
            return deterministic
        return FallbackPlannerAdapter(
            LlmPlannerAdapter(
                router=llm_router,
                resolve_company=resolve_company,
                registry=capability_registry,
                budget=budget,
                max_output_tokens=settings.openrouter_max_output_tokens,
            ),
            deterministic,
        )
    if mode == "deterministic":
        return deterministic
    if mode == "llm":
        return FallbackPlannerAdapter(
            LlmPlannerAdapter(
                router=llm_router,
                resolve_company=resolve_company,
                registry=capability_registry,
                budget=budget,
                max_output_tokens=settings.openrouter_max_output_tokens,
            ),
            DeterministicPlannerAdapter(
                DeterministicPlanner(capability_registry, budget=budget),
                resolve_company,
            ),
        )
    # This should never be reached because Settings validation rejects invalid modes,
    # but we keep it as a safety net for defense in depth.
    raise ValueError(f"invalid planner_mode: {mode!r}")


def _llm_live(settings: Settings, llm_router: LlmRouterPort) -> bool:
    """LLM agent steps need live OpenRouter (key + model) and a real router."""

    return (
        settings.openrouter_live_enabled
        and bool(settings.primary_free_model or settings.openrouter_selected_model)
        and bool(settings.openrouter_api_key.get_secret_value())
        and not isinstance(llm_router, DisabledLlmRouterAdapter)
    )


def _build_replanner(
    settings: Settings,
    capability_registry: CapabilityRegistry,
    budget: ResearchExecutionBudget,
    llm_router: LlmRouterPort,
    clock: Callable[[], datetime] | None,
) -> Replanner | None:
    """One bounded replan round: LLM with deterministic fallback, deterministic, or off."""

    mode = settings.replan_mode
    llm_live = _llm_live(settings, llm_router)
    if mode == "off" or (mode == "auto" and not llm_live):
        return None
    return Replanner(
        capability_registry,
        budget=budget,
        router=llm_router if mode in {"auto", "llm"} and llm_live else None,
        max_tasks=settings.replan_max_tasks,
        max_output_tokens=settings.openrouter_max_output_tokens,
        clock=clock,
    )


@dataclass(slots=True)
class AppContainer:
    """Application composition container."""

    settings: Settings
    readiness: ReadinessRegistry
    metadata: ApplicationMetadata
    company_catalog: CompanyCatalogPort
    resolve_company: ResolveCompany
    market_data: MarketDataPort
    get_market_snapshot: GetMarketSnapshot
    financial_data: FinancialDataPort
    get_financial_snapshot: GetFinancialSnapshot
    news_events: NewsEventPort
    get_news_event_snapshot: GetNewsEventSnapshot
    industry: IndustryContextPort
    get_industry_snapshot: GetIndustryContextSnapshot
    regulatory: RegulatoryEventPort
    get_regulatory_snapshot: GetRegulatorySnapshot
    capability_registry: CapabilityRegistry
    create_research_plan: CreateResearchPlan
    capability_executor: Phase6CapabilityExecutor
    execute_research_plan: ExecuteResearchPlan
    workflow_store: ResearchWorkflowStorePort
    create_research_workflow: CreateResearchWorkflow
    manage_research_workflow: ManageResearchWorkflow
    research_memory: ResearchMemoryPort
    watchlist_store: WatchlistStorePort
    manage_watchlist: ManageWatchlist
    notifications: NotificationPort
    request_research_report: RequestResearchReport
    verification_engine: VerificationEngine
    verify_claim: VerifyClaimUseCase
    generate_research_synthesis: GenerateResearchSynthesis
    research_report_generator: ResearchReportGeneratorPort
    selected_mcp: SelectedMcpFacade
    api_key_store: ApiKeyStorePort
    llm_router: LlmRouterPort


def _app_user_agent() -> str:
    return (
        "agentic-multi-market-research-platform/1.0 "
        "(research; contact=local-dev; +https://github.com/"
        "LeelaissakAttota/agentic-multi-market-research-platform)"
    )


def _sec_user_agent(resolved: Settings, *, feature: str) -> str | None:
    """Return the SEC fair-access User-Agent, or ``None`` (with a warning) if unset.

    SEC answers 403 to anonymous agents, so without ``SEC_USER_AGENT`` the live
    SEC path is skipped explicitly instead of silently degrading to samples.
    """

    if resolved.sec_user_agent:
        if "noreply" in resolved.sec_user_agent.lower().replace("-", ""):
            _composition_logger.warning(
                "sec_user_agent_noreply",
                extra={
                    "feature": feature,
                    "action": "SEC rejects noreply emails with HTTP 403; use a real email",
                },
            )
        return resolved.sec_user_agent
    _composition_logger.warning(
        "sec_user_agent_missing",
        extra={
            "feature": feature,
            "action": "live SEC data disabled; set SEC_USER_AGENT='Your Name you@email.com'",
        },
    )
    return None


def _build_auto_free_router(resolved: Settings, llm_http: BoundedHttpClient) -> LlmRouterPort:
    """PRIMARY_FREE_MODEL=auto: probed, 6h-disk-cached free-model ranking."""

    probe_http = BoundedHttpClient(
        UrlLibHttpTransport(max_response_bytes=_OPENROUTER_MAX_RESPONSE_BYTES * 8),
        timeout_seconds=min(float(resolved.openrouter_timeout_seconds), 10.0),
        max_retries=0,
        user_agent=_app_user_agent(),
    )
    catalog = FreeModelCatalog(
        probe_http,
        api_key=resolved.openrouter_api_key,
        cache_path=Path(resolved.openrouter_free_model_cache_path),
        ttl_seconds=float(resolved.openrouter_free_model_cache_ttl_seconds),
    )

    def factory(model_id: str) -> LlmRouterPort:
        return OpenRouterAdapter(
            llm_http,
            api_key=resolved.openrouter_api_key,
            model=model_id,
            max_output_tokens_ceiling=resolved.openrouter_max_output_tokens,
        )

    return AutoFreeModelRouter(
        catalog,
        factory,
        pinned=(resolved.fallback_free_model_1, resolved.fallback_free_model_2),
    )


def _build_catalog(resolved: Settings) -> CompanyCatalogPort:
    """Reference catalog, optionally extended with the live India/US universe."""

    base = InMemoryCompanyCatalog()
    if not resolved.company_universe_active():
        return base
    sources: list[UniverseSource] = [
        BseEquityUniverseSource(
            BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=8 * 1024 * 1024),
                timeout_seconds=float(resolved.market_data_timeout_seconds) * 2,
                max_retries=1,
                user_agent=BROWSER_USER_AGENT,
            )
        )
    ]
    sec_agent = _sec_user_agent(resolved, feature="us_company_universe")
    if sec_agent is not None:
        sources.append(
            SecTickerUniverseSource(
                BoundedHttpClient(
                    UrlLibHttpTransport(max_response_bytes=8 * 1024 * 1024),
                    timeout_seconds=float(resolved.market_data_timeout_seconds) * 2,
                    max_retries=1,
                    user_agent=sec_agent,
                )
            )
        )
    return UniverseCompanyCatalog(
        base, sources, cache_dir=Path(resolved.company_universe_cache_dir)
    )


def _build_alpha_vantage(
    resolved: Settings,
    clock: Callable[[], datetime] | None,
) -> MarketDataPort | None:
    """Second live market source; only when ALPHA_VANTAGE_API_KEY is configured."""

    if not resolved.alpha_vantage_api_key.get_secret_value():
        return None
    http = BoundedHttpClient(
        UrlLibHttpTransport(max_response_bytes=resolved.market_data_max_response_bytes),
        timeout_seconds=float(resolved.market_data_timeout_seconds),
        max_retries=0,  # every retry would burn the 25/day free budget
        user_agent=_app_user_agent(),
    )
    budget = DailyRequestBudget(
        resolved.alpha_vantage_daily_limit,
        path=Path(resolved.alpha_vantage_budget_path),
        clock=clock,
    )
    return AlphaVantageMarketDataAdapter(
        http,
        api_key=resolved.alpha_vantage_api_key,
        budget=budget,
        cache_ttl=timedelta(seconds=resolved.alpha_vantage_cache_ttl_seconds),
        clock=clock,
    )


def _live_http(resolved: Settings, *, max_bytes: int | None = None) -> BoundedHttpClient:
    return BoundedHttpClient(
        UrlLibHttpTransport(
            max_response_bytes=max_bytes or resolved.market_data_max_response_bytes
        ),
        timeout_seconds=float(resolved.market_data_timeout_seconds),
        max_retries=resolved.market_data_max_retries,
        user_agent=BROWSER_USER_AGENT,
    )


def _build_live_context(
    resolved: Settings, clock: Callable[[], datetime] | None
) -> LiveCompanyContext | None:
    """Free live Industry/Regulatory report context (Yahoo + SEC EDGAR + NSE/BSE)."""

    if str(getattr(resolved.app_env, "value", resolved.app_env)) == "test" or not (resolved.market_data_live_enabled or resolved.financial_data_live_enabled or resolved.live_news_enabled):
        return None
    sec_agent = _sec_user_agent(resolved, feature="report_live_context")
    sec_http = (
        BoundedHttpClient(
            UrlLibHttpTransport(max_response_bytes=8 * 1024 * 1024),
            timeout_seconds=float(resolved.market_data_timeout_seconds),
            max_retries=1,
            user_agent=sec_agent,
        )
        if sec_agent
        else None
    )
    limiter = HostRateLimiter(resolved.india_quotes_min_interval_seconds)
    india = IndiaCorporateAnnouncementsSource(
        _live_http(resolved),
        rate_limiter=limiter,
        scrip_resolver=BseScripResolver(
            _live_http(resolved, max_bytes=8 * 1024 * 1024), rate_limiter=limiter, clock=clock
        ),
        cache_ttl_seconds=float(resolved.india_filings_cache_ttl_seconds),
        lookback_days=resolved.india_filings_lookback_days,
        clock=clock,
    )
    yahoo_http = BoundedHttpClient(
        UrlLibHttpTransport(max_response_bytes=2 * 1024 * 1024),
        timeout_seconds=float(resolved.market_data_timeout_seconds),
        max_retries=1,
        user_agent=LIVE_CONTEXT_USER_AGENT,
    )
    return LiveCompanyContext(
        yahoo_http, sec_http=sec_http, india_announcements=india, clock=clock
    )


def _live_news_sources(
    resolved: Settings,
    clock: Callable[[], datetime] | None,
) -> list[CompanyEventSource]:
    sources: list[CompanyEventSource] = []
    if resolved.india_filings_live_enabled:
        limiter = HostRateLimiter(resolved.india_quotes_min_interval_seconds)
        sources.append(
            IndiaCorporateAnnouncementsSource(
                _live_http(resolved),
                rate_limiter=limiter,
                scrip_resolver=BseScripResolver(
                    _live_http(resolved, max_bytes=8 * 1024 * 1024),
                    rate_limiter=limiter,
                    clock=clock,
                ),
                cache_ttl_seconds=float(resolved.india_filings_cache_ttl_seconds),
                lookback_days=resolved.india_filings_lookback_days,
                clock=clock,
            )
        )
    if resolved.live_news_enabled:
        enabled = {item.strip().lower() for item in resolved.live_news_sources.split(",")}
        news_limiter = HostRateLimiter(resolved.live_news_min_interval_seconds)
        gdelt_limiter = HostRateLimiter(max(5.0, resolved.live_news_min_interval_seconds))
        ttl = float(resolved.live_news_cache_ttl_seconds)
        days = resolved.live_news_lookback_days
        if "gdelt" in enabled:
            sources.append(
                GdeltDocSource(
                    _live_http(resolved),
                    rate_limiter=gdelt_limiter,
                    cache_ttl_seconds=ttl,
                    lookback_days=days,
                    clock=clock,
                )
            )
        if "google_news" in enabled:
            sources.append(
                GoogleNewsRssSource(
                    _live_http(resolved),
                    rate_limiter=news_limiter,
                    cache_ttl_seconds=ttl,
                    lookback_days=days,
                    clock=clock,
                )
            )
        feeds = parse_ir_feed_setting(resolved.news_ir_rss_feeds)
        if "company_ir" in enabled and feeds:
            sources.append(
                RssFeedSource(
                    _live_http(resolved),
                    feeds,
                    rate_limiter=news_limiter,
                    cache_ttl_seconds=ttl,
                    lookback_days=days,
                    clock=clock,
                )
            )
    if resolved.finnhub_api_key.get_secret_value():
        # Optional: US news + analyst consensus + earnings date (Finnhub free tier).
        sources.append(
            FinnhubSource(
                _live_http(resolved),
                api_key=resolved.finnhub_api_key,
                rate_limiter=HostRateLimiter(1.0),
                cache_ttl_seconds=float(resolved.live_news_cache_ttl_seconds),
                lookback_days=min(30, resolved.live_news_lookback_days),
                clock=clock,
            )
        )
    return sources


def _build_news_events(
    resolved: Settings,
    catalog: CompanyCatalogPort,
    clock: Callable[[], datetime] | None,
) -> NewsEventPort:
    fixture: NewsEventPort = InMemoryNewsEventAdapter()
    sources = _live_news_sources(resolved, clock)
    if not sources:
        return fixture
    live = LiveCompanyEventAdapter(catalog, sources, source_rank=rank_news_source, clock=clock)
    return FallbackNewsEventAdapter(live, fixture)


def _build_regulatory(
    resolved: Settings,
    catalog: CompanyCatalogPort,
    clock: Callable[[], datetime] | None,
) -> RegulatoryEventPort:
    fixture: RegulatoryEventPort = InMemoryRegulatoryAdapter()
    if not resolved.live_regulatory_enabled:
        return fixture
    enabled = {item.strip().lower() for item in resolved.live_regulatory_sources.split(",")}
    limiter = HostRateLimiter(1.0)
    ttl = float(resolved.live_regulatory_cache_ttl_seconds)
    sources: list[RegulatoryFeedSource] = []
    sec_agent = _sec_user_agent(resolved, feature="live_regulatory") if "sec" in enabled else None
    if sec_agent is not None:
        sec_http = BoundedHttpClient(
            UrlLibHttpTransport(max_response_bytes=resolved.market_data_max_response_bytes),
            timeout_seconds=float(resolved.market_data_timeout_seconds),
            max_retries=resolved.market_data_max_retries,
            user_agent=sec_agent,
        )
        sources.append(
            SecLitigationFeedSource(
                sec_http, rate_limiter=limiter, cache_ttl_seconds=ttl, clock=clock
            )
        )
    if "sebi" in enabled:
        sources.append(
            SebiOrdersFeedSource(
                _live_http(resolved), rate_limiter=limiter, cache_ttl_seconds=ttl, clock=clock
            )
        )
    if not sources:
        return fixture
    live = LiveRegulatoryAdapter(
        catalog, sources, lookback_days=resolved.live_regulatory_lookback_days, clock=clock
    )
    return FallbackRegulatoryAdapter(live, fixture)


def build_container(
    settings: Settings | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    market_data: MarketDataPort | None = None,
    financial_data: FinancialDataPort | None = None,
    news_events: NewsEventPort | None = None,
    industry: IndustryContextPort | None = None,
    regulatory: RegulatoryEventPort | None = None,
    llm_router: LlmRouterPort | None = None,
) -> AppContainer:
    """Wire settings, readiness, Phase 2-6 intelligence, and Phase 7 workflow foundation.

    Optional ``clock`` freezes evaluation time for deterministic tests.
    Optional adapters inject fully wired ports (tests).

    Live Yahoo chart and SEC companyfacts acquisition are opt-in via settings.
    Phase 5 qualitative intelligence remains fixture-first.
    Phase 6 executes deterministic plans synchronously through existing
    Phase 2-5 use cases (no LLM planner, no external workflow-engine dependency).
    Phase 7 Prompt 1 adds in-memory workflow persistence / approval / pause-resume
    coordination on top of Phase 6 (not durable DB; not RAG or vector memory).
    Phase 8 Prompt 1 adds deterministic verification engine (no LLM, no RAG).
    Phase 9 Prompt 1 adds deterministic verified synthesis (no LLM or renderer).
    LLM Foundation Phase 3 adds a single-call OpenRouter LlmRouterPort adapter,
    wired live only when ``openrouter_live_enabled`` and a primary free model
    are both configured; otherwise a fail-closed disabled adapter is used.
    No fallback routing, caching, or agent orchestration exists yet.
    """

    resolved = settings if settings is not None else Settings()
    metadata = ApplicationMetadata(
        service=resolved.service_name,
        version=__version__,
        environment=resolved.app_env,
    )
    readiness = ReadinessRegistry()
    readiness.register(
        "application",
        lambda: ReadinessCheckResult(
            name="application",
            ready=True,
            detail="application foundation loaded",
        ),
    )
    readiness.register(
        "configuration",
        lambda: ReadinessCheckResult(
            name="configuration",
            ready=True,
            detail=f"{resolved.app_env}_configuration_validated",
        ),
    )
    if resolved.app_env not in ("production", "staging") and not (
        resolved.auth_enabled and resolved.api_keys.get_secret_value().strip(" ,")
    ):
        _composition_logger.warning(
            "api_authentication_bypassed",
            extra={
                "app_env": resolved.app_env,
                "action": "bind to 127.0.0.1 only, or set API_KEYS to enforce Bearer auth",
            },
        )
    catalog: CompanyCatalogPort = _build_catalog(resolved)
    resolve_company = ResolveCompany(catalog)

    if market_data is None:
        fixture_market = InMemoryMarketDataAdapter()
        # Yahoo (all markets) is governed only by MARKET_DATA_LIVE_ENABLED; the
        # India quote overlay below never switches on live data for US listings.
        if (
            resolved.market_data_live_enabled
            and resolved.market_data_primary_provider == "yahoo_finance_chart"
        ):
            http = BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=resolved.market_data_max_response_bytes),
                timeout_seconds=float(resolved.market_data_timeout_seconds),
                max_retries=resolved.market_data_max_retries,
                user_agent=_app_user_agent(),
            )
            live = YahooChartMarketDataAdapter(
                http,
                history_days=resolved.market_data_history_days,
                clock=clock,
            )
            secondary: MarketDataPort = fixture_market
            alpha = _build_alpha_vantage(resolved, clock)
            if alpha is not None:
                secondary = FallbackMarketDataAdapter(alpha, fixture_market)
            stacked_market: MarketDataPort = FallbackMarketDataAdapter(live, secondary)
        else:
            stacked_market = fixture_market
        if resolved.india_live_quotes_active():
            india_http = BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=resolved.market_data_max_response_bytes),
                timeout_seconds=float(resolved.market_data_timeout_seconds),
                max_retries=resolved.market_data_max_retries,
                user_agent=BROWSER_USER_AGENT,
            )
            india_limiter = HostRateLimiter(resolved.india_quotes_min_interval_seconds)
            stacked_market = IndiaLiveQuoteMarketDataAdapter(
                stacked_market,
                IndiaExchangeQuoteClient(
                    india_http,
                    rate_limiter=india_limiter,
                    cache_ttl=timedelta(seconds=resolved.india_quote_cache_ttl_seconds),
                    clock=clock,
                    scrip_resolver=BseScripResolver(
                        # The BSE equity scrip list is ~1-3 MB; give it its own cap.
                        BoundedHttpClient(
                            UrlLibHttpTransport(max_response_bytes=8 * 1024 * 1024),
                            timeout_seconds=float(resolved.market_data_timeout_seconds),
                            max_retries=resolved.market_data_max_retries,
                            user_agent=BROWSER_USER_AGENT,
                        ),
                        rate_limiter=india_limiter,
                        clock=clock,
                    ),
                ),
                clock=clock,
            )
        market_data = CachingMarketDataAdapter(
            stacked_market,
            ttl=timedelta(seconds=resolved.market_cache_ttl_seconds),
            clock=clock,
        )

    if financial_data is None:
        fixture_financial = InMemoryFinancialDataAdapter()
        fin_agent = (
            _sec_user_agent(resolved, feature="live_financial_data")
            if resolved.financial_data_live_enabled
            and resolved.financial_data_primary_provider == "sec_company_facts"
            else None
        )
        if fin_agent is not None:
            fin_http = BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=resolved.financial_data_max_response_bytes),
                timeout_seconds=float(resolved.financial_data_timeout_seconds),
                max_retries=resolved.financial_data_max_retries,
                user_agent=fin_agent,
            )
            live_financial = SecCompanyFactsFinancialDataAdapter(fin_http, clock=clock)
            stacked_financial: FinancialDataPort = FallbackFinancialDataAdapter(
                live_financial,
                fixture_financial,
            )
        else:
            stacked_financial = fixture_financial
        if resolved.financial_data_live_enabled and resolved.india_fundamentals_live_enabled:
            # Indian companies have no SEC filings; Yahoo fundamentals (live, Tier 2)
            # comes before the labelled fixture sample.
            india_fin_http = BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=resolved.financial_data_max_response_bytes),
                timeout_seconds=float(resolved.financial_data_timeout_seconds),
                max_retries=resolved.financial_data_max_retries,
                # Yahoo answers 429 to Chrome-like agents from servers; the app UA works.
                user_agent=_app_user_agent(),
            )
            stacked_financial = FallbackFinancialDataAdapter(
                YahooFundamentalsFinancialDataAdapter(india_fin_http, catalog, clock=clock),
                stacked_financial,
            )
        financial_data = CachingFinancialDataAdapter(
            stacked_financial,
            ttl=timedelta(seconds=resolved.financial_cache_ttl_seconds),
            clock=clock,
        )

    if news_events is None:
        news_events = CachingNewsEventAdapter(
            _build_news_events(resolved, catalog, clock),
            ttl=timedelta(seconds=resolved.news_cache_ttl_seconds),
            clock=clock,
        )

    if industry is None:
        industry = CachingIndustryAdapter(
            InMemoryIndustryAdapter(),
            ttl=timedelta(seconds=resolved.industry_cache_ttl_seconds),
            clock=clock,
        )

    if regulatory is None:
        regulatory = CachingRegulatoryAdapter(
            _build_regulatory(resolved, catalog, clock),
            ttl=timedelta(seconds=resolved.regulatory_cache_ttl_seconds),
            clock=clock,
        )

    if llm_router is None:
        selected_model = resolved.openrouter_selected_model.strip()
        if resolved.openrouter_live_enabled and (resolved.primary_free_model or selected_model):
            llm_http = BoundedHttpClient(
                UrlLibHttpTransport(max_response_bytes=_OPENROUTER_MAX_RESPONSE_BYTES),
                timeout_seconds=float(resolved.openrouter_timeout_seconds),
                max_retries=resolved.openrouter_max_retries,
                user_agent=_app_user_agent(),
            )
            if selected_model:
                # Explicit user choice (free or opt-in paid; Settings validated it).
                llm_router = OpenRouterAdapter(
                    llm_http,
                    api_key=resolved.openrouter_api_key,
                    model=selected_model,
                    max_output_tokens_ceiling=resolved.openrouter_max_output_tokens,
                    base_url=resolved.openrouter_base_url,
                    allow_paid=resolved.selected_model_is_paid() and resolved.allow_paid_models,
                )
            elif resolved.primary_free_model.strip().lower() == AUTO_MODEL:
                llm_router = _build_auto_free_router(resolved, llm_http)
            else:
                llm_router = OpenRouterAdapter(
                    llm_http,
                    api_key=resolved.openrouter_api_key,
                    model=resolved.primary_free_model,
                    max_output_tokens_ceiling=resolved.openrouter_max_output_tokens,
                )
        else:
            llm_router = DisabledLlmRouterAdapter()

    freshness = MarketFreshnessPolicy(
        stale_after=timedelta(hours=resolved.market_stale_after_hours),
    )
    get_market_snapshot = GetMarketSnapshot(
        resolve_company=resolve_company,
        market_data=market_data,
        freshness_policy=freshness,
        clock=clock,
    )
    get_financial_snapshot = GetFinancialSnapshot(
        live_expected=resolved.financial_data_live_enabled,
        resolve_company=resolve_company,
        financial_data=financial_data,
        clock=clock,
    )
    get_news_event_snapshot = GetNewsEventSnapshot(
        resolve_company=resolve_company,
        news_events=news_events,
        clock=clock,
    )
    get_industry_snapshot = GetIndustryContextSnapshot(
        resolve_company=resolve_company,
        industry=industry,
        clock=clock,
    )
    get_regulatory_snapshot = GetRegulatorySnapshot(
        resolve_company=resolve_company,
        regulatory=regulatory,
        clock=clock,
    )
    capability_registry = CapabilityRegistry()
    budget = ResearchExecutionBudget()
    planner = _build_planner(
        resolved,
        resolve_company,
        capability_registry,
        budget,
        llm_router,
    )
    create_research_plan = CreateResearchPlan(
        resolve_company=resolve_company,
        planner=planner,
        budget=budget,
        clock=clock,
    )
    capability_executor = Phase6CapabilityExecutor(
        resolve_company=resolve_company,
        get_market_snapshot=get_market_snapshot,
        get_financial_snapshot=get_financial_snapshot,
        get_news_event_snapshot=get_news_event_snapshot,
        get_industry_snapshot=get_industry_snapshot,
        get_regulatory_snapshot=get_regulatory_snapshot,
        live_context=_build_live_context(resolved, clock),
    )
    execute_research_plan = ExecuteResearchPlan(
        create_research_plan=create_research_plan,
        capability_executor=capability_executor,
        budget=budget,
        clock=clock,
    )
    workflow_store: ResearchWorkflowStorePort
    research_memory: ResearchMemoryPort
    watchlist_store: WatchlistStorePort
    report_store: WorkflowReportStorePort
    sqlite_db: SqliteDatabase | None = None
    if resolved.storage_backend == "sqlite":
        sqlite_db = SqliteDatabase(resolved.sqlite_path)
        workflow_store = SqliteResearchWorkflowStore(sqlite_db)
        research_memory = SqliteResearchMemoryStore(sqlite_db)
        watchlist_store = SqliteWatchlistStore(sqlite_db)
        report_store = SqliteWorkflowReportStore(sqlite_db)
    else:
        workflow_store = InMemoryResearchWorkflowStore()
        research_memory = InMemoryResearchMemoryStore()
        watchlist_store = InMemoryWatchlistStore()
        report_store = InMemoryWorkflowReportStore()
    notifications: NotificationPort = InMemoryNotificationAdapter()
    create_research_workflow = CreateResearchWorkflow(
        create_research_plan=create_research_plan,
        workflow_store=workflow_store,
        approval_policy=DeterministicApprovalPolicy(),
        notifications=notifications,
        clock=clock,
    )
    manage_research_workflow = ManageResearchWorkflow(
        workflow_store=workflow_store,
        execute_research_plan=execute_research_plan,
        resolve_company=resolve_company,
        research_memory=research_memory,
        notifications=notifications,
        clock=clock,
        replanner=_build_replanner(resolved, capability_registry, budget, llm_router, clock),
    )
    manage_watchlist = ManageWatchlist(
        watchlist_store=watchlist_store,
        resolve_company=resolve_company,
        create_research_workflow=create_research_workflow,
        notifications=notifications,
        clock=clock,
    )
    request_research_report = RequestResearchReport(
        workflow_store=workflow_store,
        clock=clock,
        research_memory=research_memory,
        report_store=report_store,
        findings_collector=ReportFindingsCollector(
            market=get_market_snapshot,
            financial=get_financial_snapshot,
            news=get_news_event_snapshot,
            regulatory=get_regulatory_snapshot,
            industry=get_industry_snapshot,
            resolve=resolve_company,
            live_context=_build_live_context(resolved, clock),
            llm_router=llm_router,
            llm_enabled=not isinstance(llm_router, DisabledLlmRouterAdapter),
            # Never exceed the router's configured ceiling (else INVALID_REQUEST).
            max_output_tokens=resolved.openrouter_max_output_tokens,
        ),
    )

    verification_engine = VerificationEngine()
    verify_claim = VerifyClaimUseCase(engine=verification_engine)
    synthesis_assembler = DeterministicSynthesisAssembler()
    research_report_generator = DeterministicResearchReportGenerator()
    if clock is None:
        generate_research_synthesis = GenerateResearchSynthesis(
            resolve_company=resolve_company,
            assembler=synthesis_assembler,
        )
    else:
        generate_research_synthesis = GenerateResearchSynthesis(
            resolve_company=resolve_company,
            assembler=synthesis_assembler,
            clock=clock,
        )
    selected_mcp = SelectedMcpFacade(
        metadata=metadata,
        readiness=readiness,
        resolve_company=resolve_company,
    )

    api_key_store: ApiKeyStorePort
    if sqlite_db is not None:
        api_key_store = SqliteApiKeyStore.seeded(sqlite_db, resolved.api_keys.get_secret_value())
    else:
        api_key_store = InMemoryApiKeyStore.from_csv(resolved.api_keys.get_secret_value())

    # F09: authentication readiness. When auth is enforced (production/staging),
    # a configuration with zero usable keys makes every protected endpoint
    # permanently unauthenticatable; /ready must surface that instead of
    # reporting unconditional success. When auth is bypassed (development/test),
    # this check is a deliberate no-op, matching require_api_key's own bypass so
    # readiness never contradicts actual endpoint usability. Uses
    # api_key_store.has_keys directly (the same authoritative source
    # require_api_key ultimately relies on) rather than re-parsing API_KEYS, so
    # there is exactly one place that decides "are there usable keys."
    #
    # `resolved.app_env in _AUTH_ENFORCED_ENVIRONMENTS` mirrors
    # require_api_key's own bypass condition (security/auth.py's
    # `_ENFORCED_ENVIRONMENTS`) rather than `resolved.auth_enabled` alone:
    # require_api_key bypasses auth by *environment* in development/test even
    # if AUTH_ENABLED=true is explicitly set there, so readiness must use the
    # same predicate or it would report "not ready" for a bypassed-auth
    # environment that is, in practice, fully usable. `security.auth` cannot
    # be imported here (it imports `AppContainer` from this module, which
    # would create a circular import), so this mirrors the identical
    # `("production", "staging")` condition already used by
    # `Settings`'s own validator (config/settings.py) rather than inventing a
    # new one.
    readiness.register(
        "authentication",
        lambda: ReadinessCheckResult(
            name="authentication",
            ready=(resolved.app_env not in _AUTH_ENFORCED_ENVIRONMENTS) or api_key_store.has_keys,
            detail=(
                "authentication_not_enforced_in_this_environment"
                if resolved.app_env not in _AUTH_ENFORCED_ENVIRONMENTS
                else "usable_api_keys_configured"
                if api_key_store.has_keys
                else "auth_enabled_but_no_usable_api_keys_configured"
            ),
        ),
    )

    return AppContainer(
        settings=resolved,
        readiness=readiness,
        metadata=metadata,
        company_catalog=catalog,
        resolve_company=resolve_company,
        market_data=market_data,
        get_market_snapshot=get_market_snapshot,
        financial_data=financial_data,
        get_financial_snapshot=get_financial_snapshot,
        news_events=news_events,
        get_news_event_snapshot=get_news_event_snapshot,
        industry=industry,
        get_industry_snapshot=get_industry_snapshot,
        regulatory=regulatory,
        get_regulatory_snapshot=get_regulatory_snapshot,
        capability_registry=capability_registry,
        create_research_plan=create_research_plan,
        capability_executor=capability_executor,
        execute_research_plan=execute_research_plan,
        workflow_store=workflow_store,
        create_research_workflow=create_research_workflow,
        manage_research_workflow=manage_research_workflow,
        research_memory=research_memory,
        watchlist_store=watchlist_store,
        manage_watchlist=manage_watchlist,
        notifications=notifications,
        request_research_report=request_research_report,
        verification_engine=verification_engine,
        verify_claim=verify_claim,
        generate_research_synthesis=generate_research_synthesis,
        research_report_generator=research_report_generator,
        selected_mcp=selected_mcp,
        api_key_store=api_key_store,
        llm_router=llm_router,
    )
