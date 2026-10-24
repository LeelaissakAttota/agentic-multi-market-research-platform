"""Demo-readiness logging: text format, message redaction, 4xx/validation/crash context."""

from __future__ import annotations

import json
import logging

import pytest

from amrp.api import create_app
from amrp.config.settings import Settings
from amrp.observability.logging import (
    StructuredFormatter,
    TextFormatter,
    configure_logging,
    redact_text,
    reset_logging_configuration,
)
from tests.support.asgi_client import TestClient

FAKE_OPENROUTER = "sk-or-v1-" + "a1b2c3d4e5f6a7b8c9d0"


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("demo", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


@pytest.fixture(autouse=True)
def _reset_logging() -> object:
    reset_logging_configuration()
    yield
    reset_logging_configuration()


def test_default_format_is_json() -> None:
    assert Settings(_env_file=None).log_format == "json"
    line = StructuredFormatter().format(_record("hello", ticker="AAPL"))
    assert json.loads(line)["ticker"] == "AAPL"


def test_text_format_is_single_readable_line() -> None:
    line = TextFormatter().format(_record("research_started", ticker="AAPL", steps=3))
    assert "\n" not in line
    assert "INFO" in line and "research_started" in line
    assert "ticker=AAPL" in line and "steps=3" in line


def test_log_format_setting_selects_text_formatter() -> None:
    configure_logging("INFO", force=True, log_format="text")
    ours = [h for h in logging.getLogger().handlers if h.name == "amrp.structured"]
    assert isinstance(ours[0].formatter, TextFormatter)
    assert Settings(_env_file=None, LOG_FORMAT="text").log_format == "text"


@pytest.mark.parametrize(
    "raw",
    [
        f"calling provider with key {FAKE_OPENROUTER}",
        "Authorization: Bearer abcDEF123456.xyz",
        "GET https://example.test/q?apikey=SECRET123&symbol=IBM",
        "api_key=SECRET123 failed",
    ],
)
def test_secrets_inside_messages_are_redacted(raw: str) -> None:
    for formatter in (StructuredFormatter(), TextFormatter()):
        out = formatter.format(_record(raw))
        assert "SECRET123" not in out
        assert FAKE_OPENROUTER not in out
        assert "abcDEF123456" not in out
        assert "[REDACTED]" in out


def test_string_extra_values_are_redacted() -> None:
    out = StructuredFormatter().format(_record("x", detail=f"upstream said {FAKE_OPENROUTER}"))
    assert FAKE_OPENROUTER not in out
    assert redact_text("symbol=IBM") == "symbol=IBM"


def test_crash_log_includes_exception_where() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "demo", logging.ERROR, __file__, 1, "crash", None, sys.exc_info()
        )
    payload = json.loads(StructuredFormatter().format(record))
    assert payload["exception_type"] == "RuntimeError"
    assert payload["exception_where"].startswith("test_logging_demo_readiness.py:")


def _client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, APP_ENV="test", LOG_LEVEL="INFO")))


def test_404_is_logged_with_route_method_status(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="amrp.api.errors"):
        response = _client().get("/definitely-not-a-route?apikey=SECRET123")
    assert response.status_code == 404
    records = [r for r in caplog.records if r.getMessage() == "http_client_error"]
    assert records, "4xx should be logged"
    record = records[-1]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.status_code == 404  # type: ignore[attr-defined]
    assert hasattr(record, "route")
    assert "SECRET123" not in StructuredFormatter().format(record)


def test_validation_log_names_field_not_value(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(Settings(_env_file=None, APP_ENV="test", LOG_LEVEL="INFO"))
    from fastapi import Body

    @app.post("/__demo_validation")
    def _demo(limit: int = Body(..., embed=True)) -> dict[str, int]:
        return {"limit": limit}

    with caplog.at_level(logging.WARNING, logger="amrp.api.errors"):
        response = TestClient(app).post("/__demo_validation", json={"limit": "SECRET-VALUE-42"})
    assert response.status_code == 422
    assert "SECRET-VALUE-42" not in response.text
    records = [r for r in caplog.records if r.getMessage() == "request_validation_failed"]
    assert records
    line = StructuredFormatter().format(records[-1])
    assert "limit" in line
    assert "SECRET-VALUE-42" not in line
