"""Verification domain — Phase 8 Prompt 1 foundation."""

from amrp.domain.verification.claim import (
    Claim,
    ClaimId,
    ClaimStatus,
    ClaimType,
)
from amrp.domain.verification.engine import VerificationEngine
from amrp.domain.verification.evidence import (
    EvidenceBundle,
    EvidenceRef,
)
from amrp.domain.verification.result import (
    ConfidenceFactor,
    ContradictionRecord,
    CriticAssessment,
    CriticAssessmentStatus,
    CriticRequest,
    VerificationResult,
    VerificationStatus,
)

__all__ = [
    "Claim",
    "ClaimId",
    "ClaimStatus",
    "ClaimType",
    "ConfidenceFactor",
    "ContradictionRecord",
    "CriticAssessment",
    "CriticAssessmentStatus",
    "CriticRequest",
    "EvidenceBundle",
    "EvidenceRef",
    "VerificationEngine",
    "VerificationResult",
    "VerificationStatus",
]
