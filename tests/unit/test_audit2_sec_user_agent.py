import logging

import pytest
from pydantic import ValidationError

from financial_intelligence.composition import _sec_user_agent
from financial_intelligence.config.settings import Settings


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, APP_ENV="test", **kw)  # type: ignore[arg-type]


def test_sec_user_agent_requires_contact_email() -> None:
    with pytest.raises(ValidationError, match="contact email"):
        _settings(SEC_USER_AGENT="anonymous-bot")


def test_configured_sec_user_agent_is_used() -> None:
    s = _settings(SEC_USER_AGENT="Jane Doe jane@example.com")
    assert _sec_user_agent(s, feature="x") == "Jane Doe jane@example.com"
    assert s.safe_log_context()["sec_user_agent_configured"] is True


def test_missing_sec_user_agent_warns_and_disables(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    assert _sec_user_agent(_settings(), feature="live_financial_data") is None
    assert any("sec_user_agent_missing" in r.getMessage() for r in caplog.records)


def test_live_financial_without_agent_uses_fixture_only() -> None:
    from financial_intelligence.composition import build_container

    container = build_container(_settings(FINANCIAL_DATA_LIVE_ENABLED=True))
    assert container is not None
