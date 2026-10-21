"""Structured logging foundation with secret-safe defaults."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from financial_intelligence.observability.correlation import get_correlation_id

_CONFIGURED = False
_HANDLER_NAME = "financial_intelligence.structured"
_NOISY_OR_URL_BEARING_LOGGERS = ("uvicorn.access", "httpx", "httpcore")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
    "database_url",
    "redis_url",
    "dsn",
)


_LOG_FORMATS = ("json", "text")
_REDACTED = "[REDACTED]"
# Secret-looking substrings inside free-text messages and string values.
_MESSAGE_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{4,}"), r"\1 " + _REDACTED),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)"
            r"(\s*[=:]\s*)(\"?)[^\s&\"',;]+"
        ),
        r"\1\2\3" + _REDACTED,
    ),
    (re.compile(r"\bsk-(?:or-)?(?:v1-)?[A-Za-z0-9_\-]{8,}"), _REDACTED),
    (re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}"), _REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{8,}"), _REDACTED),
    (re.compile(r"\btvly-[A-Za-z0-9_\-]{8,}"), _REDACTED),
)
_RESERVED_RECORD_KEYS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


def redact_text(text: str) -> str:
    """Mask API keys, Bearer tokens, and key=value secrets inside free text."""

    for pattern, replacement in _MESSAGE_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def exception_where(tb: TracebackType | None) -> str | None:
    """Return ``file.py:function:line`` of the innermost frame, without paths or values."""

    if tb is None:
        return None
    frames = traceback.extract_tb(tb)
    if not frames:
        return None
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.name}:{last.lineno}"


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "level": record.levelname,
        "logger": record.name,
        "message": redact_text(record.getMessage()),
    }
    correlation_id = get_correlation_id()
    if correlation_id is not None:
        payload["correlation_id"] = correlation_id
    for key, value in record.__dict__.items():
        if key not in _RESERVED_RECORD_KEYS:
            payload[key] = _redact_value(key, value)
    if record.exc_info:
        exception_type = record.exc_info[0]
        payload["exception_type"] = (
            exception_type.__name__ if exception_type is not None else "Exception"
        )
        where = exception_where(record.exc_info[2])
        if where is not None and "exception_where" not in payload:
            payload["exception_where"] = where
    return payload


class TextFormatter(logging.Formatter):
    """Readable one-line logs: ``time LEVEL logger message key=value ...``."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _record_payload(record)
        head = (
            f"{payload.pop('timestamp')} {payload.pop('level'):<7} "
            f"{payload.pop('logger')} | {payload.pop('message')}"
        )
        extras = " ".join(
            f"{key}={json.dumps(value, default=str, ensure_ascii=False)}"
            if not isinstance(value, str) or " " in value or not value
            else f"{key}={value}"
            for key, value in payload.items()
        )
        return f"{head} {extras}".rstrip()


class StructuredFormatter(logging.Formatter):
    """Emit one JSON object per log record with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_payload(record), default=str, ensure_ascii=False)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _redact_value(key: str, value: Any) -> Any:
    """Redact sensitive keys, including nested mappings and sequences."""

    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(key, item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def _make_formatter(log_format: str | None) -> logging.Formatter:
    chosen = (log_format or os.environ.get("LOG_FORMAT") or "json").strip().lower()
    return TextFormatter() if chosen == "text" else StructuredFormatter()


def configure_logging(
    level: str = "INFO", *, force: bool = False, log_format: str | None = None
) -> None:
    """Configure process logging once, unless ``force`` is requested.

    Repeated ``create_app()`` calls must not wipe handlers installed by prior
    instances in the same process.
    """

    global _CONFIGURED
    root = logging.getLogger()
    normalized = level.upper()
    if _CONFIGURED and not force:
        root.setLevel(normalized)
        if log_format is not None:
            for existing in root.handlers:
                if getattr(existing, "name", None) == _HANDLER_NAME:
                    existing.setFormatter(_make_formatter(log_format))
        for logger_name in _NOISY_OR_URL_BEARING_LOGGERS:
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        return

    # Remove only our previous structured handler to avoid stacking duplicates
    # when force=True, without deleting unrelated application handlers.
    root.handlers = [
        handler for handler in root.handlers if getattr(handler, "name", None) != _HANDLER_NAME
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.setFormatter(_make_formatter(log_format))
    root.addHandler(handler)
    root.setLevel(normalized)
    for logger_name in _NOISY_OR_URL_BEARING_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    _CONFIGURED = True


def reset_logging_configuration() -> None:
    """Test helper to clear the one-time logging configuration latch."""

    global _CONFIGURED
    root = logging.getLogger()
    root.handlers = [
        handler for handler in root.handlers if getattr(handler, "name", None) != _HANDLER_NAME
    ]
    _CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""

    return logging.getLogger(name)
