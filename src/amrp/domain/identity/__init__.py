"""Company / security / listing identity domain package."""

from amrp.domain.identity.aliases import AliasType, CompanyAlias
from amrp.domain.identity.codes import (
    CountryCode,
    CurrencyCode,
    ExchangeCode,
    TickerSymbol,
)
from amrp.domain.identity.company import CompanyIdentity
from amrp.domain.identity.ids import CompanyId, ListingId, SecurityId
from amrp.domain.identity.listing import ListingIdentity, ListingStatus
from amrp.domain.identity.normalization import (
    MAX_COMPANY_NAME_LENGTH,
    MAX_QUERY_LENGTH,
    company_match_key,
    normalize_company_display_name,
    normalize_ticker,
)
from amrp.domain.identity.providers import ProviderIdentifier, ProviderKind
from amrp.domain.identity.security import SecurityIdentity, SecurityType

__all__ = [
    "MAX_COMPANY_NAME_LENGTH",
    "MAX_QUERY_LENGTH",
    "AliasType",
    "CompanyAlias",
    "CompanyId",
    "CompanyIdentity",
    "CountryCode",
    "CurrencyCode",
    "ExchangeCode",
    "ListingId",
    "ListingIdentity",
    "ListingStatus",
    "ProviderIdentifier",
    "ProviderKind",
    "SecurityId",
    "SecurityIdentity",
    "SecurityType",
    "TickerSymbol",
    "company_match_key",
    "normalize_company_display_name",
    "normalize_ticker",
]
