from __future__ import annotations

import json
from pathlib import Path

from financial_intelligence.infrastructure.company.in_memory_catalog import InMemoryCompanyCatalog
from financial_intelligence.infrastructure.company.universe import (
    BseEquityUniverseSource,
    UniverseCompanyCatalog,
)
from financial_intelligence.infrastructure.http.provider_health import PROVIDER_HEALTH
from tests.unit.test_audit2_universe import BSE_ROWS, _Src


def _state(name: str) -> dict[str, object]:
    return next(p for p in PROVIDER_HEALTH.snapshot() if p["provider"] == name)


def test_configured_provider_listed_as_unused_then_cache_hit_counts(tmp_path: Path) -> None:
    PROVIDER_HEALTH.reset()
    (tmp_path / "india_bse_equity.json").write_text(json.dumps(BSE_ROWS), encoding="utf-8")
    src = _Src(BseEquityUniverseSource(None), BSE_ROWS)  # type: ignore[arg-type]
    catalog = UniverseCompanyCatalog(InMemoryCompanyCatalog(), [src], cache_dir=tmp_path)
    assert _state("india_bse_equity")["state"] == "unused"
    catalog.search_name_candidates("eternal")
    state = _state("india_bse_equity")
    assert src.calls == 0
    assert state["state"] == "ok" and state["last_success_via"] == "disk_cache"
