from __future__ import annotations

from amrp.api import create_app
from amrp.config.settings import Settings
from tests.support.asgi_client import TestClient


def _status(path: str, **env: object) -> int:
    settings = Settings(_env_file=None, **env)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        return client.get(path).status_code


def test_docs_on_in_development_by_default() -> None:
    assert _status("/docs") == 200
    assert _status("/redoc") == 200


def test_docs_can_be_disabled() -> None:
    assert _status("/docs", API_DOCS_ENABLED=False) == 404


def test_docs_off_by_default_outside_development() -> None:
    assert Settings(_env_file=None, APP_ENV="staging").api_docs_active() is False  # type: ignore[call-arg]
