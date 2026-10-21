from __future__ import annotations

import logging
from pathlib import Path

import pytest

from financial_intelligence.composition import _sec_user_agent
from financial_intelligence.config.settings import Settings


def test_noreply_sec_user_agent_warns(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, SEC_USER_AGENT="Leela 1+x@users.noreply.github.com"
    )
    with caplog.at_level(logging.WARNING):
        assert _sec_user_agent(settings, feature="test") is not None
    assert any(r.getMessage() == "sec_user_agent_noreply" for r in caplog.records)


def test_env_example_explains_real_email_requirement() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "noreply" in text and "REAL mailbox" in text
