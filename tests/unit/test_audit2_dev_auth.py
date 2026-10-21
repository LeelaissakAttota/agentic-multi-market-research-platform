from __future__ import annotations

from financial_intelligence.api.app import create_app
from financial_intelligence.config.settings import Settings
from tests.support.asgi_client import TestClient

KEY = "dev-key-123456789"


def _client(**kw: object) -> TestClient:
    settings = Settings(_env_file=None, APP_ENV="development", **kw)  # type: ignore[arg-type]
    return TestClient(create_app(settings=settings))


def test_dev_without_api_keys_still_bypasses() -> None:
    with _client() as client:
        assert client.get("/companies/resolve", params={"q": "Apple"}).status_code == 200


def test_dev_with_api_keys_and_auth_enabled_enforces() -> None:
    with _client(API_KEYS=KEY) as client:
        assert client.get("/companies/resolve", params={"q": "Apple"}).status_code == 401
        ok = client.get(
            "/companies/resolve",
            params={"q": "Apple"},
            headers={"Authorization": f"Bearer {KEY}"},
        )
        assert ok.status_code == 200


def test_dev_with_auth_disabled_bypasses_even_with_keys() -> None:
    with _client(API_KEYS=KEY, AUTH_ENABLED=False) as client:
        assert client.get("/companies/resolve", params={"q": "Apple"}).status_code == 200
