"""Integration tests for the opt-in SQLite storage backend (tmp paths only)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from amrp.api.app import create_app
from amrp.application.company_resolution import CompanyQuery
from amrp.application.manage_watchlist import (
    CreateWatchlistQuery,
    WatchlistEntryInput,
)
from amrp.application.workflow_contracts import CreateResearchWorkflowQuery
from amrp.composition import AppContainer, build_container
from amrp.config.settings import Settings
from amrp.domain.identity import CountryCode, ExchangeCode
from amrp.domain.orchestration import ResearchObjective
from amrp.infrastructure.memory import ResearchMemoryStoreError
from amrp.infrastructure.sqlite import (
    CodecError,
    SqliteApiKeyStore,
    SqliteDatabase,
    SqliteResearchMemoryStore,
    SqliteResearchWorkflowStore,
    SqliteWorkflowReportStore,
    hash_api_key,
)
from amrp.infrastructure.sqlite.codec import dumps, loads
from amrp.infrastructure.workflow import WorkflowStoreError
from tests.support.asgi_client import TestClient


def _settings(db_path: Path, **extra: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        STORAGE_BACKEND="sqlite",
        SQLITE_PATH=str(db_path),
        **extra,
    )


def _create_and_execute(container: AppContainer) -> str:
    created = container.create_research_workflow.execute(
        CreateResearchWorkflowQuery(
            company_query=CompanyQuery(
                raw_query="Reliance", country=CountryCode("IN"), exchange=ExchangeCode("NSE")
            ),
            objective=ResearchObjective.MARKET_ANALYSIS,
        )
    )
    assert created.workflow is not None
    executed = container.manage_research_workflow.execute(created.workflow.workflow_id)
    assert executed.workflow is not None
    return executed.workflow.workflow_id.as_text()


def test_default_backend_is_memory() -> None:
    settings = Settings(_env_file=None, APP_ENV="test")  # type: ignore[call-arg]
    assert settings.storage_backend == "memory"
    container = build_container(settings)
    assert not isinstance(container.workflow_store, SqliteResearchWorkflowStore)


def test_invalid_backend_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, APP_ENV="test", STORAGE_BACKEND="postgres")  # type: ignore[call-arg]


def test_workflow_memory_and_checkpoint_survive_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "fi.sqlite3"
    first = build_container(_settings(db_path))
    assert isinstance(first.workflow_store, SqliteResearchWorkflowStore)
    assert isinstance(first.research_memory, SqliteResearchMemoryStore)
    workflow_id_text = _create_and_execute(first)
    original = next(
        w
        for w in first.workflow_store.list_workflows()
        if w.workflow_id.as_text() == workflow_id_text
    )
    original_cp = first.workflow_store.get_latest_checkpoint(original.workflow_id)
    original_mem = first.research_memory.list_for_workflow(original.workflow_id)
    assert db_path.exists()

    second = build_container(_settings(db_path))
    reloaded = second.workflow_store.get_workflow(original.workflow_id)
    assert reloaded is not None
    assert reloaded.to_dict() == original.to_dict()
    cp = second.workflow_store.get_latest_checkpoint(original.workflow_id)
    assert cp is not None and original_cp is not None
    assert cp.to_dict() == original_cp.to_dict()
    mem = second.research_memory.list_for_workflow(original.workflow_id)
    assert [m.to_dict() for m in mem] == [m.to_dict() for m in original_mem]
    assert len(second.workflow_store.list_workflows(status=reloaded.status)) == 1
    assert second.workflow_store.list_workflows(company_id_text="nope") == ()


def test_store_invariants_match_in_memory(tmp_path: Path) -> None:
    container = build_container(_settings(tmp_path / "fi.sqlite3"))
    _create_and_execute(container)
    store = container.workflow_store
    with pytest.raises(WorkflowStoreError):
        store.list_workflows(limit=0)
    with pytest.raises(WorkflowStoreError):
        store.list_workflows(offset=-1)
    workflow = store.list_workflows()[0]
    records = container.research_memory.list_for_workflow(workflow.workflow_id)
    assert records
    with pytest.raises(ResearchMemoryStoreError):
        container.research_memory.append(records[0])
    checkpoint = store.get_latest_checkpoint(workflow.workflow_id)
    assert checkpoint is not None
    # Re-saving a stale checkpoint version is rejected by the shared validator.
    with pytest.raises(WorkflowStoreError):
        store.save_checkpoint(checkpoint)


def test_watchlist_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "fi.sqlite3"
    first = build_container(_settings(db_path))
    result = first.manage_watchlist.create(
        CreateWatchlistQuery(
            name="India", entries=(WatchlistEntryInput(q="Reliance", exchange="NSE"),)
        )
    )
    assert result.watchlist is not None
    second = build_container(_settings(db_path))
    loaded = second.watchlist_store.get(result.watchlist.watchlist_id)
    assert loaded is not None
    assert loaded.to_dict() == result.watchlist.to_dict()
    assert len(second.watchlist_store.list_all()) == 1


def test_api_keys_hashed_seeded_and_revocable(tmp_path: Path) -> None:
    db = SqliteDatabase(tmp_path / "keys.sqlite3")
    store = SqliteApiKeyStore.seeded(db, "alpha-key-123, beta-key-456")
    assert store.has_keys
    assert store.is_valid("alpha-key-123")
    assert not store.is_valid("wrong")
    assert not store.is_valid("")
    assert not store.is_valid("ключ")
    generated = store.create_key(label="ci")
    assert store.is_valid(generated)
    assert store.revoke_key("alpha-key-123")
    assert not store.is_valid("alpha-key-123")
    assert not store.revoke_key("alpha-key-123")
    db.close()
    raw = sqlite3.connect(tmp_path / "keys.sqlite3")
    hashes = {row[0] for row in raw.execute("SELECT key_hash FROM api_keys")}
    dump = "\n".join(raw.iterdump())
    raw.close()
    assert hash_api_key("beta-key-456") in hashes
    assert "beta-key-456" not in dump and generated not in dump


def test_codec_rejects_non_domain_types() -> None:
    with pytest.raises(CodecError):
        dumps(object())
    forged = json.dumps({"t": "dc", "c": "os.path:join", "f": []})
    with pytest.raises(CodecError):
        loads(forged)
    with pytest.raises(CodecError):
        loads("not json")


def test_tampered_payload_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "fi.sqlite3"
    container = build_container(_settings(db_path))
    workflow_id_text = _create_and_execute(container)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE workflows SET payload = ? WHERE workflow_id = ?",
        (json.dumps({"t": "dc", "c": "subprocess:Popen", "f": []}), workflow_id_text),
    )
    raw.commit()
    raw.close()
    reopened = build_container(_settings(db_path))
    with pytest.raises(CodecError):
        reopened.workflow_store.list_workflows()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_report_route_renders_and_stores(tmp_path: Path, backend: str) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        APP_ENV="test",
        LOG_LEVEL="WARNING",
        STORAGE_BACKEND=backend,
        SQLITE_PATH=str(tmp_path / "fi.sqlite3"),
    )
    container = build_container(settings)
    with TestClient(create_app(container=container)) as client:
        created = client.post(
            "/research/workflows",
            json={"q": "Reliance", "exchange": "NSE", "objective": "market_analysis"},
        )
        workflow_id = created.json()["workflow_id"]
        assert client.post(f"/research/workflows/{workflow_id}/execute").status_code == 200

        md = client.post(f"/research/workflows/{workflow_id}/report")
        assert md.status_code == 200
        body = md.json()
        stored = body["stored_report"]
        assert stored["format"] == "markdown"
        assert "## Summary" in stored["content"]
        assert "not investment advice" in stored["content"].lower()

        js = client.post(f"/research/workflows/{workflow_id}/report?format=json")
        assert js.status_code == 200
        parsed = json.loads(js.json()["stored_report"]["content"])
        assert parsed["workflow"]["workflow_id"] == workflow_id

        deferred = client.post(f"/research/workflows/{workflow_id}/report?format=deferred")
        assert deferred.status_code == 200
        assert "deferred" in json.dumps(deferred.json()).lower()

        bad = client.post(f"/research/workflows/{workflow_id}/report?format=pdf")
        assert bad.status_code == 400

    if backend == "sqlite":
        db = SqliteDatabase(tmp_path / "fi.sqlite3")
        reports = SqliteWorkflowReportStore(db)
        workflow = build_container(settings).workflow_store.list_workflows()[0]
        saved = reports.list_for_workflow(workflow.workflow_id)
        assert {r.report_format for r in saved} == {"markdown", "json"}
        db.close()
