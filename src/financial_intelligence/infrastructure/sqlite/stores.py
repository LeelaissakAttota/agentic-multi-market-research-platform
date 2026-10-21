"""SQLite adapters for the application persistence ports.

Each store implements the same port and invariants as its in-memory
counterpart; aggregates are stored as allow-listed typed JSON (see
``codec.py``) next to indexed columns used for filtering and ordering.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from financial_intelligence.domain.identity import CompanyId
from financial_intelligence.domain.memory import MemoryRecordId, ResearchMemoryRecord
from financial_intelligence.domain.report import ReportRequestId, StoredWorkflowReport
from financial_intelligence.domain.watchlist import Watchlist, WatchlistId
from financial_intelligence.domain.workflow import (
    ResearchWorkflow,
    WorkflowCheckpoint,
    WorkflowId,
    WorkflowStatus,
)
from financial_intelligence.infrastructure.memory import ResearchMemoryStoreError
from financial_intelligence.infrastructure.reporting import ReportStoreError
from financial_intelligence.infrastructure.sqlite.codec import dumps, loads
from financial_intelligence.infrastructure.sqlite.database import SqliteDatabase
from financial_intelligence.infrastructure.watchlist import WatchlistStoreError
from financial_intelligence.infrastructure.workflow import WorkflowStoreError
from financial_intelligence.infrastructure.workflow.in_memory_store import (
    validate_checkpoint,
    validate_workflow_update,
)


def _check_page(limit: int, offset: int, *, maximum: int, error: type[ValueError]) -> None:
    if limit < 1 or limit > maximum:
        msg = f"limit must be between 1 and {maximum}"
        raise error(msg)
    if offset < 0:
        msg = "offset must be non-negative"
        raise error(msg)


def _load[T](raw: object, expected: type[T]) -> T:
    value = loads(str(raw))
    if not isinstance(value, expected):
        msg = f"stored payload is not a {expected.__name__}"
        raise TypeError(msg)
    return value


class SqliteResearchWorkflowStore:
    """Durable ResearchWorkflowStorePort."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def save_workflow(self, workflow: ResearchWorkflow) -> None:
        key = workflow.workflow_id.as_text()
        with self._db.transaction() as cur:
            row = cur.execute(
                "SELECT payload FROM workflows WHERE workflow_id = ?", (key,)
            ).fetchone()
            existing = _load(row[0], ResearchWorkflow) if row else None
            validate_workflow_update(existing, workflow)
            cur.execute(
                "INSERT INTO workflows(workflow_id, status, company_id, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(workflow_id) DO UPDATE SET "
                "status = excluded.status, payload = excluded.payload",
                (
                    key,
                    workflow.status.value,
                    workflow.company_id.as_text(),
                    workflow.created_at.isoformat(),
                    dumps(workflow),
                ),
            )

    def get_workflow(self, workflow_id: WorkflowId) -> ResearchWorkflow | None:
        rows = self._db.query(
            "SELECT payload FROM workflows WHERE workflow_id = ?", (workflow_id.as_text(),)
        )
        return _load(rows[0][0], ResearchWorkflow) if rows else None

    def list_workflows(
        self,
        *,
        status: WorkflowStatus | None = None,
        company_id_text: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ResearchWorkflow, ...]:
        _check_page(limit, offset, maximum=200, error=WorkflowStoreError)
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if company_id_text is not None:
            clauses.append("company_id = ?")
            params.append(company_id_text)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.query(
            f"SELECT payload FROM workflows {where} "
            "ORDER BY created_at, workflow_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return tuple(_load(row[0], ResearchWorkflow) for row in rows)

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        key = checkpoint.workflow_id.as_text()
        with self._db.transaction() as cur:
            wf_row = cur.execute(
                "SELECT payload FROM workflows WHERE workflow_id = ?", (key,)
            ).fetchone()
            cp_row = cur.execute(
                "SELECT payload FROM workflow_checkpoints WHERE workflow_id = ?", (key,)
            ).fetchone()
            validate_checkpoint(
                _load(wf_row[0], ResearchWorkflow) if wf_row else None,
                _load(cp_row[0], WorkflowCheckpoint) if cp_row else None,
                checkpoint,
            )
            cur.execute(
                "INSERT INTO workflow_checkpoints(workflow_id, version, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET version = excluded.version, "
                "payload = excluded.payload",
                (key, checkpoint.version, dumps(checkpoint)),
            )

    def get_latest_checkpoint(self, workflow_id: WorkflowId) -> WorkflowCheckpoint | None:
        rows = self._db.query(
            "SELECT payload FROM workflow_checkpoints WHERE workflow_id = ?",
            (workflow_id.as_text(),),
        )
        return _load(rows[0][0], WorkflowCheckpoint) if rows else None


class SqliteWatchlistStore:
    """Durable WatchlistStorePort."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def save(self, watchlist: Watchlist) -> None:
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO watchlists(watchlist_id, created_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(watchlist_id) DO UPDATE SET payload = excluded.payload",
                (
                    watchlist.watchlist_id.as_text(),
                    watchlist.created_at.isoformat(),
                    dumps(watchlist),
                ),
            )

    def get(self, watchlist_id: WatchlistId) -> Watchlist | None:
        rows = self._db.query(
            "SELECT payload FROM watchlists WHERE watchlist_id = ?", (watchlist_id.as_text(),)
        )
        return _load(rows[0][0], Watchlist) if rows else None

    def list_all(self, *, limit: int = 50, offset: int = 0) -> tuple[Watchlist, ...]:
        _check_page(limit, offset, maximum=200, error=WatchlistStoreError)
        rows = self._db.query(
            "SELECT payload FROM watchlists ORDER BY created_at, watchlist_id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return tuple(_load(row[0], Watchlist) for row in rows)


class SqliteResearchMemoryStore:
    """Durable, append-only ResearchMemoryPort."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def append(self, record: ResearchMemoryRecord) -> None:
        key = record.record_id.as_text()
        task_key = f"{record.workflow_id.as_text()}:{record.task_id.as_text()}"
        with self._db.transaction() as cur:
            if cur.execute("SELECT 1 FROM research_memory WHERE record_id = ?", (key,)).fetchone():
                msg = f"duplicate memory record_id: {key}"
                raise ResearchMemoryStoreError(msg)
            if cur.execute(
                "SELECT 1 FROM research_memory WHERE task_key = ?", (task_key,)
            ).fetchone():
                msg = f"duplicate memory for workflow task: {task_key}"
                raise ResearchMemoryStoreError(msg)
            cur.execute(
                "INSERT INTO research_memory(record_id, workflow_id, task_key, company_id, "
                "created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key,
                    record.workflow_id.as_text(),
                    task_key,
                    record.company_id.as_text(),
                    record.created_at.isoformat(),
                    dumps(record),
                ),
            )

    def get_record(self, record_id: MemoryRecordId) -> ResearchMemoryRecord | None:
        rows = self._db.query(
            "SELECT payload FROM research_memory WHERE record_id = ?", (record_id.as_text(),)
        )
        return _load(rows[0][0], ResearchMemoryRecord) if rows else None

    def _list(self, column: str, value: str, limit: int) -> tuple[ResearchMemoryRecord, ...]:
        _check_page(limit, 0, maximum=500, error=ResearchMemoryStoreError)
        rows = self._db.query(
            f"SELECT payload FROM research_memory WHERE {column} = ? "
            "ORDER BY created_at, record_id LIMIT ?",
            (value, limit),
        )
        return tuple(_load(row[0], ResearchMemoryRecord) for row in rows)

    def list_for_workflow(
        self, workflow_id: WorkflowId, *, limit: int = 100
    ) -> tuple[ResearchMemoryRecord, ...]:
        return self._list("workflow_id", workflow_id.as_text(), limit)

    def list_for_company(
        self, company_id: CompanyId, *, limit: int = 100
    ) -> tuple[ResearchMemoryRecord, ...]:
        return self._list("company_id", company_id.as_text(), limit)


class SqliteWorkflowReportStore:
    """Durable WorkflowReportStorePort."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def save(self, report: StoredWorkflowReport) -> None:
        key = report.report_id.as_text()
        with self._db.transaction() as cur:
            if cur.execute("SELECT 1 FROM workflow_reports WHERE report_id = ?", (key,)).fetchone():
                msg = f"duplicate report_id: {key}"
                raise ReportStoreError(msg)
            cur.execute(
                "INSERT INTO workflow_reports(report_id, workflow_id, report_format, created_at, "
                "payload) VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    report.workflow_id.as_text(),
                    report.report_format,
                    report.created_at.isoformat(),
                    dumps(report),
                ),
            )

    def get(self, report_id: ReportRequestId) -> StoredWorkflowReport | None:
        rows = self._db.query(
            "SELECT payload FROM workflow_reports WHERE report_id = ?", (report_id.as_text(),)
        )
        return _load(rows[0][0], StoredWorkflowReport) if rows else None

    def list_for_workflow(
        self, workflow_id: WorkflowId, *, limit: int = 20
    ) -> tuple[StoredWorkflowReport, ...]:
        _check_page(limit, 0, maximum=200, error=ReportStoreError)
        rows = self._db.query(
            "SELECT payload FROM workflow_reports WHERE workflow_id = ? "
            "ORDER BY created_at DESC, report_id DESC LIMIT ?",
            (workflow_id.as_text(), limit),
        )
        return tuple(_load(row[0], StoredWorkflowReport) for row in rows)


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest of an API key (keys are high-entropy secrets)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class SqliteApiKeyStore:
    """Durable ApiKeyStorePort storing only SHA-256 key hashes.

    Configured keys (``API_KEYS``) are seeded on startup; additional keys can
    be created and revoked programmatically (``create_key`` / ``revoke_key``).
    Plaintext keys are never stored. Comparison is constant-time over hashes.
    """

    def __init__(
        self,
        db: SqliteDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def seeded(cls, db: SqliteDatabase, csv_value: str) -> SqliteApiKeyStore:
        store = cls(db)
        for token in (raw.strip() for raw in csv_value.split(",")):
            if token:
                store.add_key(token, label="configured", source="config")
        return store

    def add_key(self, raw_key: str, *, label: str = "manual", source: str = "admin") -> None:
        if not raw_key or not raw_key.isascii():
            msg = "API keys must be non-empty ASCII"
            raise ValueError(msg)
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO api_keys(key_hash, label, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (hash_api_key(raw_key), label[:64], source[:32], self._clock().isoformat()),
            )

    def create_key(self, *, label: str = "generated") -> str:
        """Generate, store (hashed) and return a new random API key once."""
        raw_key = secrets.token_urlsafe(32)
        self.add_key(raw_key, label=label, source="generated")
        return raw_key

    def revoke_key(self, raw_key: str) -> bool:
        with self._db.transaction() as cur:
            cur.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ? AND revoked_at IS NULL",
                (self._clock().isoformat(), hash_api_key(raw_key)),
            )
            return cur.rowcount > 0

    def _active_hashes(self) -> list[str]:
        return [
            str(row[0])
            for row in self._db.query("SELECT key_hash FROM api_keys WHERE revoked_at IS NULL")
        ]

    def is_valid(self, presented_key: str) -> bool:
        if not presented_key or not presented_key.isascii():
            return False
        candidate = hash_api_key(presented_key)
        hashes = self._active_hashes()
        return any(secrets.compare_digest(candidate, stored) for stored in hashes)

    @property
    def has_keys(self) -> bool:
        return bool(self._db.query("SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1"))
