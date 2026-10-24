"""Replaceable report-generation port for deterministic Phase 9 adapters."""

from __future__ import annotations

from typing import Protocol

from amrp.domain.report import (
    ReportArtifact,
    ResearchReportGenerationRequest,
)
from amrp.domain.synthesis import ResearchSynthesis


class ResearchReportGeneratorPort(Protocol):
    """Generate an artifact solely from an existing structured synthesis."""

    def generate(
        self,
        request: ResearchReportGenerationRequest,
        synthesis: ResearchSynthesis,
    ) -> ReportArtifact: ...
