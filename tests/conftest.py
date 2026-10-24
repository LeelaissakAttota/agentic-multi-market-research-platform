"""Suite-wide test isolation.

Real developer/CI environments may export provider secrets (for example
``OPENROUTER_API_KEY``). Settings read the process environment, so tests that
assert "missing key fails closed" would otherwise depend on the host shell.
Every test starts with these variables removed; tests that need a value pass
it explicitly to ``Settings(...)``. The developer's ``.env`` file is also
ignored during tests so local settings (e.g. ``AUTH_ENABLED``/``API_KEYS``)
cannot change test outcomes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from amrp.config.settings import Settings, get_settings

_ISOLATED_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "FINNHUB_API_KEY",
    "SEC_USER_AGENT",
    "API_KEYS",
    "AUTH_ENABLED",
    "PLANNER_MODE",
    "OPENROUTER_LIVE_ENABLED",
    "PRIMARY_FREE_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
