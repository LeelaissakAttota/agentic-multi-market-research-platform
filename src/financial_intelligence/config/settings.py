"""Production-quality typed settings with fail-closed paid-model policy."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnvironment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
PlannerMode = Literal["auto", "deterministic", "llm"]
ReplanMode = Literal["auto", "llm", "deterministic", "off"]

SERVICE_NAME = "agentic-financial-intelligence"
_SAFE_HOST_PATTERN = re.compile(
    r"^(?:localhost|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*|(?:\d{1,3}\.){3}\d{1,3})$"
)

_SECRET_FIELD_NAMES = frozenset(
    {
        "openrouter_api_key",
        "database_url",
        "redis_url",
        "alpha_vantage_api_key",
        "finnhub_api_key",
        "api_keys",
    }
)


class Settings(BaseSettings):
    """Environment-driven application settings.

    Blank optional provider URLs/keys are intentional: Phase 1 startup must
    succeed without live external services.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnvironment = Field(default="development", alias="APP_ENV")
    api_docs_enabled: bool | None = Field(
        default=None,
        alias="API_DOCS_ENABLED",
        description=(
            "Serve Swagger UI at /docs and ReDoc at /redoc. Unset = on in "
            "development/test, off in staging/production."
        ),
    )
    log_level: LogLevel = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "text"] = Field(default="json", alias="LOG_FORMAT")
    service_name: str = Field(default=SERVICE_NAME, alias="SERVICE_NAME")
    allowed_hosts: str = Field(
        default="localhost,127.0.0.1",
        alias="ALLOWED_HOSTS",
        max_length=1024,
    )
    api_max_request_body_bytes: int = Field(
        default=1_048_576,
        alias="API_MAX_REQUEST_BODY_BYTES",
        ge=4096,
        le=10_485_760,
    )

    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")
    allow_paid_models: bool = Field(default=False, alias="ALLOW_PAID_MODELS")
    primary_free_model: str = Field(default="", alias="PRIMARY_FREE_MODEL")
    fallback_free_model_1: str = Field(default="", alias="FALLBACK_FREE_MODEL_1")
    fallback_free_model_2: str = Field(default="", alias="FALLBACK_FREE_MODEL_2")

    # LLM Foundation Phase 1 — configuration only; no live OpenRouter calls exist yet.
    openrouter_live_enabled: bool = Field(default=False, alias="OPENROUTER_LIVE_ENABLED")
    openrouter_timeout_seconds: int = Field(
        default=30,
        alias="OPENROUTER_TIMEOUT_SECONDS",
        ge=1,
        le=120,
    )
    openrouter_max_retries: int = Field(
        default=2,
        alias="OPENROUTER_MAX_RETRIES",
        ge=0,
        le=5,
    )
    openrouter_max_output_tokens: int = Field(
        default=1024,
        alias="OPENROUTER_MAX_OUTPUT_TOKENS",
        ge=1,
        le=32_768,
    )

    # PRIMARY_FREE_MODEL=auto: discover + probe OpenRouter :free models,
    # cache the ranking on disk for 6h, then re-check automatically.
    openrouter_free_model_cache_path: str = Field(
        default="data/openrouter_free_models.json",
        alias="OPENROUTER_FREE_MODEL_CACHE_PATH",
        min_length=1,
        max_length=1024,
    )
    openrouter_free_model_cache_ttl_seconds: int = Field(
        default=21_600,
        alias="OPENROUTER_FREE_MODEL_CACHE_TTL_SECONDS",
        ge=300,
        le=7 * 86_400,
    )

    # Planner selection. "auto" (default): the LLM planner when live OpenRouter is
    # configured, with automatic per-request fallback to the deterministic planner on
    # any failure; deterministic otherwise. "llm": LLM planner only (startup fails
    # without prerequisites). "deterministic": never call an LLM for planning.
    planner_mode: PlannerMode = Field(default="auto", alias="PLANNER_MODE")
    # One bounded replan round after execution. "auto" (default): LLM replanner with
    # deterministic fallback when live OpenRouter is configured, off otherwise.
    # "deterministic": retry failed/blocked tasks once, no LLM. "off": no replan round.
    replan_mode: ReplanMode = Field(default="auto", alias="REPLAN_MODE")
    replan_max_tasks: int = Field(default=3, alias="REPLAN_MAX_TASKS", ge=1, le=5)

    # Phase 11.2 — API-key authentication.
    # AUTH_ENABLED controls whether inbound requests must supply a valid Bearer key.
    # In production and staging, AUTH_ENABLED=false is rejected at startup (fail-closed).
    # In development and test, AUTH_ENABLED=false is permitted for local workflow.
    # API_KEYS is a comma-separated list of opaque secret Bearer keys.
    # Keys are stored in process memory only; persistent storage belongs to a later phase.
    auth_enabled: bool = Field(default=True, alias="AUTH_ENABLED")
    api_keys: SecretStr = Field(default=SecretStr(""), alias="API_KEYS")

    database_url: SecretStr = Field(default=SecretStr(""), alias="DATABASE_URL")
    redis_url: SecretStr = Field(default=SecretStr(""), alias="REDIS_URL")

    alpha_vantage_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="ALPHA_VANTAGE_API_KEY",
    )
    # Reserved: no Finnhub adapter exists (Yahoo + Alpha Vantage cover US/India).
    finnhub_api_key: SecretStr = Field(default=SecretStr(""), alias="FINNHUB_API_KEY")
    alpha_vantage_daily_limit: int = Field(
        default=25,
        alias="ALPHA_VANTAGE_DAILY_LIMIT",
        ge=0,
        le=100_000,
    )
    alpha_vantage_cache_ttl_seconds: int = Field(
        default=43_200,
        alias="ALPHA_VANTAGE_CACHE_TTL_SECONDS",
        ge=60,
        le=7 * 86_400,
    )
    alpha_vantage_budget_path: str = Field(
        default="data/alpha_vantage_budget.json",
        alias="ALPHA_VANTAGE_BUDGET_PATH",
        min_length=1,
        max_length=1024,
    )

    # SEC fair-access policy: data.sec.gov rejects (HTTP 403) requests whose
    # User-Agent does not identify a person/company with a contact email,
    # e.g. "Jane Doe jane@example.com". Required for live SEC data.
    sec_user_agent: str = Field(default="", alias="SEC_USER_AGENT", max_length=256)

    http_timeout_seconds: int = Field(default=30, alias="HTTP_TIMEOUT_SECONDS", ge=1, le=300)
    max_http_retries: int = Field(default=2, alias="MAX_HTTP_RETRIES", ge=0, le=10)

    market_stale_after_hours: int = Field(
        default=72,
        alias="MARKET_STALE_AFTER_HOURS",
        ge=1,
        le=8760,
    )
    market_cache_ttl_seconds: int = Field(
        default=300,
        alias="MARKET_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    market_data_live_enabled: bool = Field(
        default=False,
        alias="MARKET_DATA_LIVE_ENABLED",
    )
    market_data_primary_provider: Literal["yahoo_finance_chart", "none"] = Field(
        default="yahoo_finance_chart",
        alias="MARKET_DATA_PRIMARY_PROVIDER",
    )
    market_data_timeout_seconds: int = Field(
        default=15,
        alias="MARKET_DATA_TIMEOUT_SECONDS",
        ge=1,
        le=120,
    )
    market_data_max_response_bytes: int = Field(
        default=1_048_576,
        alias="MARKET_DATA_MAX_RESPONSE_BYTES",
        ge=1024,
        le=10_485_760,
    )
    market_data_max_retries: int = Field(
        default=2,
        alias="MARKET_DATA_MAX_RETRIES",
        ge=0,
        le=5,
    )
    market_data_history_days: int = Field(
        default=30,
        alias="MARKET_DATA_HISTORY_DAYS",
        ge=1,
        le=365,
    )

    financial_cache_ttl_seconds: int = Field(
        default=3600,
        alias="FINANCIAL_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    financial_data_live_enabled: bool = Field(
        default=False,
        alias="FINANCIAL_DATA_LIVE_ENABLED",
    )
    india_fundamentals_live_enabled: bool = Field(
        default=True,
        alias="INDIA_FUNDAMENTALS_LIVE_ENABLED",
        description=(
            "Live India fundamentals via Yahoo fundamentals timeseries (no key). "
            "Only effective when FINANCIAL_DATA_LIVE_ENABLED=true."
        ),
    )
    financial_data_primary_provider: Literal["sec_company_facts", "none"] = Field(
        default="sec_company_facts",
        alias="FINANCIAL_DATA_PRIMARY_PROVIDER",
    )
    financial_data_timeout_seconds: int = Field(
        default=15,
        alias="FINANCIAL_DATA_TIMEOUT_SECONDS",
        ge=1,
        le=120,
    )
    # SEC companyfacts JSON is 4-5 MB for large caps (AAPL ~3.8 MB, MSFT ~4.9 MB);
    # a 2 MB cap made every live request fail as "oversized".
    financial_data_max_response_bytes: int = Field(
        default=16_777_216,
        alias="FINANCIAL_DATA_MAX_RESPONSE_BYTES",
        ge=1024,
        le=67_108_864,
    )
    financial_data_max_retries: int = Field(
        default=2,
        alias="FINANCIAL_DATA_MAX_RETRIES",
        ge=0,
        le=5,
    )

    news_cache_ttl_seconds: int = Field(
        default=900,
        alias="NEWS_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    industry_cache_ttl_seconds: int = Field(
        default=3600,
        alias="INDUSTRY_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    regulatory_cache_ttl_seconds: int = Field(
        default=3600,
        alias="REGULATORY_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    # None (unset) = follow MARKET_DATA_LIVE_ENABLED; true/false overrides.
    india_live_quotes_enabled: bool | None = Field(
        default=None,
        alias="INDIA_LIVE_QUOTES_ENABLED",
    )
    india_quotes_min_interval_seconds: float = Field(
        default=1.0,
        alias="INDIA_QUOTES_MIN_INTERVAL_SECONDS",
        ge=0.2,
        le=60.0,
    )
    india_quote_cache_ttl_seconds: int = Field(
        default=15,
        alias="INDIA_QUOTE_CACHE_TTL_SECONDS",
        ge=1,
        le=3600,
    )
    # None (unset) = follow MARKET_DATA_LIVE_ENABLED. Extends the ~10-company
    # reference catalog with the BSE equity list (India) and SEC tickers (US).
    company_universe_live_enabled: bool | None = Field(
        default=None,
        alias="COMPANY_UNIVERSE_LIVE_ENABLED",
    )
    company_universe_cache_dir: str = Field(
        default="data/universe",
        alias="COMPANY_UNIVERSE_CACHE_DIR",
        min_length=1,
        max_length=1024,
    )
    india_filings_live_enabled: bool = Field(
        default=False,
        alias="INDIA_FILINGS_LIVE_ENABLED",
    )
    india_filings_lookback_days: int = Field(
        default=30,
        alias="INDIA_FILINGS_LOOKBACK_DAYS",
        ge=1,
        le=365,
    )
    india_filings_cache_ttl_seconds: int = Field(
        default=900,
        alias="INDIA_FILINGS_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    live_news_enabled: bool = Field(default=False, alias="LIVE_NEWS_ENABLED")
    live_news_sources: str = Field(
        default="gdelt,google_news,company_ir",
        alias="LIVE_NEWS_SOURCES",
        max_length=256,
    )
    news_ir_rss_feeds: str = Field(default="", alias="NEWS_IR_RSS_FEEDS", max_length=4096)
    live_news_min_interval_seconds: float = Field(
        default=2.0,
        alias="LIVE_NEWS_MIN_INTERVAL_SECONDS",
        ge=0.5,
        le=60.0,
    )
    live_news_lookback_days: int = Field(
        default=7,
        alias="LIVE_NEWS_LOOKBACK_DAYS",
        ge=1,
        le=90,
    )
    live_news_cache_ttl_seconds: int = Field(
        default=900,
        alias="LIVE_NEWS_CACHE_TTL_SECONDS",
        ge=1,
        le=86400,
    )
    live_regulatory_enabled: bool = Field(default=False, alias="LIVE_REGULATORY_ENABLED")
    live_regulatory_sources: str = Field(
        default="sec,sebi",
        alias="LIVE_REGULATORY_SOURCES",
        max_length=128,
    )
    live_regulatory_lookback_days: int = Field(
        default=365,
        alias="LIVE_REGULATORY_LOOKBACK_DAYS",
        ge=1,
        le=3650,
    )
    live_regulatory_cache_ttl_seconds: int = Field(
        default=1800,
        alias="LIVE_REGULATORY_CACHE_TTL_SECONDS",
        ge=60,
        le=86400,
    )
    storage_backend: Literal["memory", "sqlite"] = Field(
        default="memory",
        alias="STORAGE_BACKEND",
    )
    sqlite_path: str = Field(
        default="data/financial_intelligence.sqlite3",
        alias="SQLITE_PATH",
        min_length=1,
        max_length=1024,
    )

    @field_validator("allow_paid_models")
    @classmethod
    def paid_models_must_remain_disabled(cls, value: bool) -> bool:
        """Fail closed: paid models are prohibited in development policy."""

        if value is True:
            msg = "ALLOW_PAID_MODELS=true is rejected; paid models are disabled"
            raise ValueError(msg)
        return False

    @model_validator(mode="after")
    def reject_duplicate_free_model_ids(self) -> Self:
        """Reject unsafe or internally inconsistent runtime configuration."""

        configured = [
            model_id
            for model_id in (
                self.primary_free_model,
                self.fallback_free_model_1,
                self.fallback_free_model_2,
            )
            if model_id
        ]
        if len(configured) != len(set(configured)):
            msg = "configured free model IDs must be unique"
            raise ValueError(msg)

        allowed_hosts = self.allowed_host_values()
        if self.app_env in ("production", "staging"):
            if self.log_level == "DEBUG":
                msg = "LOG_LEVEL=DEBUG is rejected in production"
                raise ValueError(msg)
            if not allowed_hosts or "*" in allowed_hosts:
                msg = "production ALLOWED_HOSTS must be an explicit non-wildcard allowlist"
                raise ValueError(msg)
            if not self.auth_enabled:
                msg = (
                    "AUTH_ENABLED=false is rejected in production/staging; "
                    "authentication must be enabled for public-facing deployment"
                )
                raise ValueError(msg)
        if self.sec_user_agent and "@" not in self.sec_user_agent:
            msg = "SEC_USER_AGENT must include a contact email, e.g. 'Jane Doe jane@example.com'"
            raise ValueError(msg)
        if self.market_data_live_enabled and self.market_data_primary_provider == "none":
            msg = "live market data requires an enabled primary provider"
            raise ValueError(msg)
        if self.financial_data_live_enabled and self.financial_data_primary_provider == "none":
            msg = "live financial data requires an enabled primary provider"
            raise ValueError(msg)
        if self.openrouter_live_enabled and not self.primary_free_model:
            msg = "live OpenRouter routing requires a configured primary free model"
            raise ValueError(msg)
        if self.planner_mode == "llm" or self.replan_mode == "llm":
            label = "planner_mode=llm" if self.planner_mode == "llm" else "replan_mode=llm"
            if not self.openrouter_live_enabled:
                msg = f"{label} requires OPENROUTER_LIVE_ENABLED=true"
                raise ValueError(msg)
            if not self.primary_free_model:
                msg = f"{label} requires a configured PRIMARY_FREE_MODEL"
                raise ValueError(msg)
            if not self.openrouter_api_key.get_secret_value():
                msg = f"{label} requires a configured OPENROUTER_API_KEY"
                raise ValueError(msg)
        return self

    def api_docs_active(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.app_env not in ("production", "staging")

    def india_live_quotes_active(self) -> bool:
        """NSE/BSE live quotes default ON whenever live market data is ON."""

        if self.india_live_quotes_enabled is None:
            return self.market_data_live_enabled
        return self.india_live_quotes_enabled

    def company_universe_active(self) -> bool:
        """Live company universe defaults ON whenever live market data is ON."""

        if self.company_universe_live_enabled is None:
            return self.market_data_live_enabled
        return self.company_universe_live_enabled

    def allowed_host_values(self) -> tuple[str, ...]:
        """Return a stable validated host allowlist without ports or wildcards."""

        values = tuple(
            dict.fromkeys(
                part.strip().lower() for part in self.allowed_hosts.split(",") if part.strip()
            )
        )
        for host in values:
            if host == "*":
                continue
            if len(host) > 253 or not _SAFE_HOST_PATTERN.fullmatch(host):
                msg = "ALLOWED_HOSTS contains an invalid hostname"
                raise ValueError(msg)
            if host.replace(".", "").isdigit():
                octets = host.split(".")
                if len(octets) != 4 or any(int(octet) > 255 for octet in octets):
                    msg = "ALLOWED_HOSTS contains an invalid IP address"
                    raise ValueError(msg)
        return values

    def secret_values(self) -> dict[str, str]:
        """Return secret field values for internal redaction tests only."""

        return {
            "openrouter_api_key": self.openrouter_api_key.get_secret_value(),
            "database_url": self.database_url.get_secret_value(),
            "redis_url": self.redis_url.get_secret_value(),
            "alpha_vantage_api_key": self.alpha_vantage_api_key.get_secret_value(),
            "finnhub_api_key": self.finnhub_api_key.get_secret_value(),
            "api_keys": self.api_keys.get_secret_value(),
        }

    def safe_log_context(self) -> dict[str, Any]:
        """Return non-secret settings suitable for structured logs."""

        payload: dict[str, Any] = {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "service_name": self.service_name,
            "allowed_hosts": self.allowed_host_values(),
            "api_max_request_body_bytes": self.api_max_request_body_bytes,
            "allow_paid_models": self.allow_paid_models,
            "primary_free_model": self.primary_free_model or None,
            "fallback_free_model_1": self.fallback_free_model_1 or None,
            "fallback_free_model_2": self.fallback_free_model_2 or None,
            "openrouter_live_enabled": self.openrouter_live_enabled,
            "openrouter_timeout_seconds": self.openrouter_timeout_seconds,
            "openrouter_max_retries": self.openrouter_max_retries,
            "openrouter_max_output_tokens": self.openrouter_max_output_tokens,
            "http_timeout_seconds": self.http_timeout_seconds,
            "max_http_retries": self.max_http_retries,
            "market_stale_after_hours": self.market_stale_after_hours,
            "market_cache_ttl_seconds": self.market_cache_ttl_seconds,
            "market_data_live_enabled": self.market_data_live_enabled,
            "market_data_primary_provider": self.market_data_primary_provider,
            "market_data_timeout_seconds": self.market_data_timeout_seconds,
            "market_data_max_response_bytes": self.market_data_max_response_bytes,
            "market_data_max_retries": self.market_data_max_retries,
            "market_data_history_days": self.market_data_history_days,
            "financial_cache_ttl_seconds": self.financial_cache_ttl_seconds,
            "financial_data_live_enabled": self.financial_data_live_enabled,
            "financial_data_primary_provider": self.financial_data_primary_provider,
            "financial_data_timeout_seconds": self.financial_data_timeout_seconds,
            "financial_data_max_response_bytes": self.financial_data_max_response_bytes,
            "financial_data_max_retries": self.financial_data_max_retries,
            "news_cache_ttl_seconds": self.news_cache_ttl_seconds,
            "industry_cache_ttl_seconds": self.industry_cache_ttl_seconds,
            "regulatory_cache_ttl_seconds": self.regulatory_cache_ttl_seconds,
            "storage_backend": self.storage_backend,
            "india_live_quotes_enabled": self.india_live_quotes_active(),
            "india_filings_live_enabled": self.india_filings_live_enabled,
            "company_universe_live_enabled": self.company_universe_active(),
            "live_news_enabled": self.live_news_enabled,
            "live_regulatory_enabled": self.live_regulatory_enabled,
            "auth_enabled": self.auth_enabled,
            "api_keys_configured": bool(self.api_keys.get_secret_value()),
            "database_configured": bool(self.database_url.get_secret_value()),
            "redis_configured": bool(self.redis_url.get_secret_value()),
            "openrouter_key_configured": bool(self.openrouter_api_key.get_secret_value()),
            "alpha_vantage_key_configured": bool(self.alpha_vantage_api_key.get_secret_value()),
            "finnhub_key_configured": bool(self.finnhub_api_key.get_secret_value()),
            "planner_mode": self.planner_mode,
            "sec_user_agent_configured": bool(self.sec_user_agent),
        }
        for secret_name in _SECRET_FIELD_NAMES:
            assert secret_name not in payload
        return payload


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-cached settings. Prefer explicit injection in tests."""

    return Settings()
