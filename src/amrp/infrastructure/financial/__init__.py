"""Phase 4 financial infrastructure adapters."""

from amrp.infrastructure.financial.cache import CachingFinancialDataAdapter
from amrp.infrastructure.financial.fallback import FallbackFinancialDataAdapter
from amrp.infrastructure.financial.in_memory import InMemoryFinancialDataAdapter
from amrp.infrastructure.financial.india_filings import (
    INDIA_AUTHORITY_PRECEDENCE,
    IndiaFilingAuthority,
    parse_india_results_fixture,
)
from amrp.infrastructure.financial.sec_company_facts import (
    SecCompanyFactsFinancialDataAdapter,
)

__all__ = [
    "INDIA_AUTHORITY_PRECEDENCE",
    "CachingFinancialDataAdapter",
    "FallbackFinancialDataAdapter",
    "InMemoryFinancialDataAdapter",
    "IndiaFilingAuthority",
    "SecCompanyFactsFinancialDataAdapter",
    "parse_india_results_fixture",
]
