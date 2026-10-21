"""Live company universe: extends the reference catalog with exchange masters.

The reference catalog only covers ~10 demo issuers. When enabled, lookups that
miss the reference catalog fall through to a universe built from:

* India: the BSE active equity scrip list (``ListofScripData``). Each issuer is
  given an NSE listing (BSE ``scrip_id`` == NSE symbol for most equities) and
  a BSE listing.
* US: SEC ``company_tickers_exchange.json`` (NASDAQ/NYSE only). Requires a
  SEC fair-access User-Agent; the CIK is registered for SEC companyfacts.

Raw payloads are cached on disk for ``ttl`` (default 24h) so restarts do not
re-download. Identifiers are deterministic UUIDv4-shaped hashes of
``exchange:ticker`` so ids are stable across restarts. Reference-catalog
entries always win; universe entries never shadow them.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock
from typing import Protocol
from urllib.parse import urlencode

from financial_intelligence.domain.identity import (
    AliasType,
    CompanyAlias,
    CompanyId,
    CompanyIdentity,
    CountryCode,
    CurrencyCode,
    ExchangeCode,
    ListingId,
    ListingIdentity,
    ListingStatus,
    ProviderIdentifier,
    ProviderKind,
    SecurityId,
    SecurityIdentity,
    SecurityType,
    TickerSymbol,
)
from financial_intelligence.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from financial_intelligence.infrastructure.financial.sec_cik_mapping import register_sec_cik
from financial_intelligence.infrastructure.http import BoundedHttpClient, HttpTransportError
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from financial_intelligence.observability.logging import get_logger

logger = get_logger("financial_intelligence.infrastructure.company.universe")

_TICKER_OK = re.compile(r"^[A-Z0-9./-]{1,32}$")
_BSE_LIST_URL = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
_BSE_HEADERS = {
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept-Language": "en-US,en;q=0.9",
}
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_US_EXCHANGES = {"NASDAQ": "NASDAQ", "NYSE": "NYSE"}

#: Well-known renames so old brand names still resolve (ticker -> former names).
FORMER_NAMES: dict[str, tuple[str, ...]] = {
    "ETERNAL": ("Zomato", "Zomato Limited"),
    # Tata Motors demerger (Oct 2025): the old listed entity (BSE 500570) became
    # TMPV "Tata Motors Passenger Vehicles"; the commercial-vehicle business listed
    # as TMCV and took the "Tata Motors Limited" name. "Tata Motors" is therefore
    # deliberately ambiguous and returns both as candidates.
    "TMPV": ("Tata Motors", "Tata Motors Passenger Vehicles"),
    "TMCV": ("Tata Motors", "Tata Motors Commercial Vehicles"),
    "LTIM": ("Mindtree", "Larsen & Toubro Infotech", "LTI"),
    "ADANIENSOL": ("Adani Transmission",),
    "UNITDSPR": ("United Spirits", "McDowell"),
    "SAMMAANCAP": ("Indiabulls Housing Finance",),
}


_SUFFIXES = re.compile(r"[\s,.]+(limited|ltd|inc|corp|corporation|co)\.?$", re.IGNORECASE)


def display_case(name: str) -> str:
    """Readable display name for ALL-CAPS registry names ("TATA MOTORS LIMITED").

    Mixed-case names are kept. Vowel-less tokens (HDFC, ABB, ITC) stay upper-case.
    """

    if not name.isupper():
        return name
    words = []
    for word in name.split():
        letters = [c for c in word if c.isalpha()]
        if (
            letters
            and not any(c in "AEIOU" for c in letters)
            and word.strip(".,") not in {"LTD", "PVT", "CO", "CORP"}
        ):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def short_name(name: str) -> str:
    """Name without a trailing Limited/Ltd/Inc/Corp suffix ("HDFC Bank")."""

    return _SUFFIXES.sub("", name.strip()).strip()


def stable_v4_uuid(key: str) -> str:
    """Deterministic UUID with version-4 bits (domain ids require v4)."""

    digest = bytearray(hashlib.sha256(key.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _company(
    *,
    key: str,
    name: str,
    country: str,
    currency: str,
    listings: Sequence[tuple[str, str, bool]],
    aliases: Sequence[str] = (),
    provider_ids: tuple[ProviderIdentifier, ...] = (),
    display_name: str | None = None,
    industry: str | None = None,
    short_names: Sequence[str] = (),
) -> CompanyIdentity:
    company_id = CompanyId.from_string(stable_v4_uuid(f"company:{key}"))
    security_id = SecurityId.from_string(stable_v4_uuid(f"security:{key}"))
    listing_objs = tuple(
        ListingIdentity(
            listing_id=ListingId.from_string(stable_v4_uuid(f"listing:{exchange}:{ticker}")),
            security_id=security_id,
            exchange=ExchangeCode(exchange),
            ticker=TickerSymbol(ticker),
            currency=CurrencyCode(currency),
            country=CountryCode(country),
            is_primary=primary,
            status=ListingStatus.ACTIVE,
        )
        for exchange, ticker, primary in listings
    )
    return CompanyIdentity(
        company_id=company_id,
        legal_name=name,
        display_name=display_name or display_case(name),
        industry=industry,
        country=CountryCode(country),
        aliases=(
            *(CompanyAlias.create(a, AliasType.FORMER_NAME) for a in dict.fromkeys(aliases) if a),
            *(
                CompanyAlias.create(a, AliasType.SHORT_NAME)
                for a in dict.fromkeys(short_names)
                if a and a not in aliases
            ),
        ),
        securities=(
            SecurityIdentity(
                security_id=security_id,
                company_id=company_id,
                security_type=SecurityType.COMMON_SHARE,
                display_name=f"{name} Common Share",
                listings=listing_objs,
            ),
        ),
        provider_identifiers=provider_ids,
    )


class UniverseSource(Protocol):
    name: str

    def fetch_raw(self) -> object:
        """Download the raw master payload (JSON-decoded)."""

    def build(self, raw: object) -> list[CompanyIdentity]:
        """Convert a raw payload into company identities."""


class BseEquityUniverseSource:
    """Indian listed equities from the BSE active scrip list."""

    name = "india_bse_equity"

    def __init__(self, http: BoundedHttpClient) -> None:
        self._http = http

    def fetch_raw(self) -> object:
        query = {
            "Group": "",
            "Scripcode": "",
            "industry": "",
            "segment": "Equity",
            "status": "Active",
        }
        response = self._http.get_response(
            f"{_BSE_LIST_URL}?{urlencode(query)}",
            accept="application/json",
            extra_headers=_BSE_HEADERS,
        )
        return json.loads(response.body.decode("utf-8"))

    def build(self, raw: object) -> list[CompanyIdentity]:
        if not isinstance(raw, list):
            msg = "BSE scrip list must be a JSON array"
            raise ValueError(msg)
        out: list[CompanyIdentity] = []
        seen: set[str] = set()
        for row in raw:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("scrip_id") or "").strip().upper()
            name = str(row.get("Issuer_Name") or row.get("Scrip_Name") or "").strip()
            scrip_name = str(row.get("Scrip_Name") or "").strip()
            industry = str(row.get("INDUSTRY") or "").strip() or None
            if not ticker or not name or ticker in seen or not _TICKER_OK.match(ticker):
                continue
            seen.add(ticker)
            ids: tuple[ProviderIdentifier, ...] = ()
            code = str(row.get("SCRIP_CD") or "").strip()
            if code.isdigit():
                ids = (ProviderIdentifier(ProviderKind.BSE, code),)
            try:
                out.append(
                    _company(
                        key=f"IN:{ticker}",
                        name=name,
                        country="IN",
                        currency="INR",
                        listings=(("NSE", ticker, True), ("BSE", ticker, False)),
                        aliases=FORMER_NAMES.get(ticker, ()),
                        short_names=(
                            short_name(display_case(name)),
                            *((scrip_name, short_name(scrip_name)) if scrip_name else ()),
                        ),
                        provider_ids=ids,
                        display_name=(
                            scrip_name
                            if name.isupper() and scrip_name and not scrip_name.isupper()
                            else None
                        ),
                        industry=industry,
                    )
                )
            except ValueError:
                continue
        return out


class SecTickerUniverseSource:
    """US NASDAQ/NYSE issuers from SEC company_tickers_exchange.json."""

    name = "us_sec_tickers"

    def __init__(self, http: BoundedHttpClient) -> None:
        self._http = http

    def fetch_raw(self) -> object:
        return self._http.get_json(_SEC_TICKERS_URL)

    def build(self, raw: object) -> list[CompanyIdentity]:
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
            msg = "SEC ticker payload missing data"
            raise ValueError(msg)
        by_cik: dict[int, tuple[str, list[tuple[str, str, bool]]]] = {}
        for row in raw["data"]:
            if not isinstance(row, list) or len(row) < 4:
                continue
            cik, name, ticker, exchange = row[0], row[1], row[2], row[3]
            if not isinstance(cik, int) or not isinstance(name, str):
                continue
            ticker = str(ticker or "").strip().upper()
            mapped = _US_EXCHANGES.get(str(exchange or "").strip().upper())
            if mapped is None or not _TICKER_OK.match(ticker):
                continue
            entry = by_cik.setdefault(cik, (name.strip(), []))
            entry[1].append((mapped, ticker, not entry[1]))
        out: list[CompanyIdentity] = []
        for cik, (name, listings) in by_cik.items():
            cik_text = f"{cik:010d}"
            try:
                company = _company(
                    key=f"US:CIK{cik_text}",
                    name=name,
                    country="US",
                    currency="USD",
                    listings=listings,
                    provider_ids=(ProviderIdentifier(ProviderKind.SEC_CIK, cik_text),),
                    short_names=(short_name(display_case(name)),),
                )
            except ValueError:
                continue
            register_sec_cik(company.company_id, cik_text)
            out.append(company)
        return out


class UniverseCompanyCatalog:
    """CompanyCatalogPort: reference catalog first, then the live universe."""

    def __init__(
        self,
        base: InMemoryCompanyCatalog,
        sources: Sequence[UniverseSource],
        *,
        cache_dir: Path | None = None,
        ttl_seconds: float = 86_400.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._base = base
        self._sources = tuple(sources)
        self._cache_dir = cache_dir
        self._ttl = ttl_seconds
        self._wall = wall_clock
        self._lock = Lock()
        self._universe: InMemoryCompanyCatalog | None = None
        for source in self._sources:
            PROVIDER_HEALTH.register(source.name)
        self._loaded_at: float | None = None

    # -- loading -------------------------------------------------------------
    def _raw_for(self, source: UniverseSource) -> object | None:
        path = self._cache_dir / f"{source.name}.json" if self._cache_dir else None
        if path is not None and path.exists():
            try:
                if self._wall() - path.stat().st_mtime < self._ttl:
                    cached: object = json.loads(path.read_text(encoding="utf-8"))
                    PROVIDER_HEALTH.record_success(source.name, via="disk_cache")
                    return cached
            except (OSError, ValueError):
                pass
        try:
            raw = source.fetch_raw()
        except (HttpTransportError, ValueError, UnicodeDecodeError) as exc:
            PROVIDER_HEALTH.record_failure(source.name, type(exc).__name__)
            logger.warning(
                "company_universe_source_unavailable",
                extra={"source": source.name, "error_type": type(exc).__name__},
            )
            if path is not None and path.exists():  # stale cache beats nothing
                try:
                    stale: object = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
                return stale
            return None
        PROVIDER_HEALTH.record_success(source.name)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(raw), encoding="utf-8")
            except OSError:
                logger.info("company_universe_cache_not_written", extra={"source": source.name})
        return raw

    def _ensure(self) -> InMemoryCompanyCatalog | None:
        with self._lock:
            now = self._wall()
            if self._loaded_at is not None and now - self._loaded_at < self._ttl:
                return self._universe
            self._loaded_at = now
            taken_listings = {
                (listing.exchange.as_text(), listing.ticker.as_text())
                for company in self._base_companies()
                for listing in company.all_listings()
            }
            companies: list[CompanyIdentity] = []
            for source in self._sources:
                raw = self._raw_for(source)
                if raw is None:
                    continue
                try:
                    built = source.build(raw)
                except (ValueError, TypeError, KeyError) as exc:
                    logger.warning(
                        "company_universe_invalid_payload",
                        extra={"source": source.name, "error_type": type(exc).__name__},
                    )
                    continue
                for company in built:
                    keys = {
                        (lst.exchange.as_text(), lst.ticker.as_text())
                        for lst in company.all_listings()
                    }
                    if keys & taken_listings:
                        continue
                    taken_listings |= keys
                    companies.append(company)
            self._universe = InMemoryCompanyCatalog(tuple(companies)) if companies else None
            logger.info("company_universe_loaded", extra={"companies": len(companies)})
            return self._universe

    def _base_companies(self) -> tuple[CompanyIdentity, ...]:
        return self._base._companies

    # -- CompanyCatalogPort --------------------------------------------------
    def get_by_id(self, company_id: CompanyId) -> CompanyIdentity | None:
        found = self._base.get_by_id(company_id)
        if found is not None:
            return found
        universe = self._ensure()
        return universe.get_by_id(company_id) if universe else None

    def find_by_ticker(
        self,
        ticker: TickerSymbol,
        *,
        exchange: ExchangeCode | None = None,
        country: CountryCode | None = None,
    ) -> tuple[CompanyIdentity, ...]:
        return self._find_ticker(ticker, exchange=exchange, country=country)

    def _find_ticker(
        self,
        ticker: TickerSymbol,
        *,
        exchange: ExchangeCode | None,
        country: CountryCode | None,
    ) -> tuple[CompanyIdentity, ...]:
        found = self._base.find_by_ticker(ticker, exchange=exchange, country=country)
        if found:
            return found
        universe = self._ensure()
        return (
            universe.find_by_ticker(ticker, exchange=exchange, country=country) if universe else ()
        )

    def find_by_alias(
        self, normalized_alias: str, *, country: CountryCode | None = None
    ) -> tuple[CompanyIdentity, ...]:
        found = self._base.find_by_alias(normalized_alias, country=country)
        if found:
            return found
        universe = self._ensure()
        return universe.find_by_alias(normalized_alias, country=country) if universe else ()

    def find_by_name(
        self, normalized_name: str, *, country: CountryCode | None = None
    ) -> tuple[CompanyIdentity, ...]:
        found = self._base.find_by_name(normalized_name, country=country)
        if found:
            return found
        universe = self._ensure()
        return universe.find_by_name(normalized_name, country=country) if universe else ()

    def search_name_candidates(
        self,
        normalized_name: str,
        *,
        country: CountryCode | None = None,
        limit: int = 5,
    ) -> tuple[CompanyIdentity, ...]:
        found = self._base.search_name_candidates(normalized_name, country=country, limit=limit)
        if found:
            return found
        universe = self._ensure()
        if universe is None:
            return ()
        return universe.search_name_candidates(normalized_name, country=country, limit=limit)
