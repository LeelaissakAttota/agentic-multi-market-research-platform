"""US SEC CIK mapping for canonical company IDs (infrastructure-only)."""

from __future__ import annotations

from threading import Lock

from amrp.domain.identity import CompanyId

# Reference issuers. CIK is zero-padded to 10 digits for URLs.
_US_SEC_CIK: dict[str, str] = {
    "22222222-2222-4222-8222-222222222001": "0000320193",  # Apple Inc.
    "22222222-2222-4222-8222-222222222002": "0000789019",  # Microsoft Corp.
    "22222222-2222-4222-8222-222222222003": "0001652044",  # Alphabet Inc.
    "22222222-2222-4222-8222-222222222004": "0001018724",  # Amazon.com Inc.
    "22222222-2222-4222-8222-222222222005": "0001318605",  # Tesla Inc.
}
_lock = Lock()


def register_sec_cik(company_id: CompanyId, cik: str) -> None:
    """Register a CIK discovered from the live company universe."""

    if not (cik.isdigit() and len(cik) == 10):
        msg = "CIK must be 10 zero-padded digits"
        raise ValueError(msg)
    with _lock:
        _US_SEC_CIK.setdefault(company_id.as_text(), cik)


def sec_cik_for_company(company_id: CompanyId) -> str | None:
    """Return SEC CIK for a known US issuer, or None when unsupported."""

    return _US_SEC_CIK.get(company_id.as_text())
