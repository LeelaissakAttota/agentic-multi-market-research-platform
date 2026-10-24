"""Process-wide live-provider health registry (last success / last failure).

Live adapters fall back silently by design (fixtures, secondary providers),
so this registry makes provider outages visible: every live adapter records
its outcome here and ``GET /health/providers`` reports it. Contains no
secrets — only provider names, timestamps, and failure kinds/status codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock


@dataclass(slots=True)
class ProviderStatus:
    provider: str
    successes: int = 0
    failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure: str | None = None
    consecutive_failures: int = 0
    last_success_via: str | None = None

    @property
    def state(self) -> str:
        if self.successes == 0 and self.failures == 0:
            return "unused"
        if self.consecutive_failures == 0:
            return "ok"
        return "failing" if self.successes == 0 else "degraded"

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "state": self.state,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "last_failure": self.last_failure,
            "last_success_via": self.last_success_via,
        }


@dataclass(slots=True)
class ProviderHealthRegistry:
    _items: dict[str, ProviderStatus] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def _get(self, provider: str) -> ProviderStatus:
        item = self._items.get(provider)
        if item is None:
            item = self._items[provider] = ProviderStatus(provider)
        return item

    def register(self, provider: str) -> None:
        """List a configured provider (state ``unused``) before its first call."""

        with self._lock:
            self._get(provider)

    def record_success(self, provider: str, *, via: str = "network") -> None:
        with self._lock:
            item = self._get(provider)
            item.last_success_via = via
            item.successes += 1
            item.consecutive_failures = 0
            item.last_success_at = datetime.now(UTC)

    def record_failure(self, provider: str, reason: str, status_code: int | None = None) -> None:
        detail = reason[:80] if status_code is None else f"{reason[:80]} (http {status_code})"
        with self._lock:
            item = self._get(provider)
            item.failures += 1
            item.consecutive_failures += 1
            item.last_failure_at = datetime.now(UTC)
            item.last_failure = detail

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._items[name].as_dict() for name in sorted(self._items)]

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


PROVIDER_HEALTH = ProviderHealthRegistry()
