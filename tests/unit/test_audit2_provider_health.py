from __future__ import annotations

from amrp.api.app import create_app
from amrp.config.settings import Settings
from amrp.infrastructure.http.provider_health import (
    PROVIDER_HEALTH,
    ProviderHealthRegistry,
)
from tests.support.asgi_client import TestClient
from tests.unit.test_audit2_yahoo import _adapter, _fetch, _payload


def test_registry_states() -> None:
    reg = ProviderHealthRegistry()
    reg.record_failure("x", "rate_limited", 429)
    assert reg.snapshot()[0]["state"] == "failing"
    assert reg.snapshot()[0]["last_failure"] == "rate_limited (http 429)"
    reg.record_success("x")
    assert reg.snapshot()[0]["state"] == "ok"
    reg.record_failure("x", "timeout")
    assert reg.snapshot()[0]["state"] == "degraded"


def test_yahoo_records_success_and_endpoint_reports_it() -> None:
    PROVIDER_HEALTH.reset()
    assert _fetch(_adapter(_payload(3))) is not None
    app = create_app(settings=Settings(_env_file=None, APP_ENV="test"))
    with TestClient(app) as client:
        body = client.get("/health/providers").json()
    names = {p["provider"]: p for p in body["providers"]}
    assert names["yahoo_finance_chart"]["state"] == "ok"
    PROVIDER_HEALTH.reset()
