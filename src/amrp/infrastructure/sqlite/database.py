"""SQLite connection management and schema (stdlib ``sqlite3`` only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    company_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_workflows_status ON workflows(status);
CREATE INDEX IF NOT EXISTS ix_workflows_company ON workflows(company_id);
CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    workflow_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlists (
    watchlist_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_memory (
    record_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_key TEXT NOT NULL UNIQUE,
    company_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_workflow ON research_memory(workflow_id);
CREATE INDEX IF NOT EXISTS ix_memory_company ON research_memory(company_id);
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS workflow_reports (
    report_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    report_format TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reports_workflow ON workflow_reports(workflow_id);
"""


class SqliteDatabase:
    """One shared, serialized SQLite connection per database file.

    Writes are serialized with a process-local lock and run in explicit
    transactions. ``:memory:`` is supported for tests.
    """

    def __init__(self, path: str | Path) -> None:
        text = str(path)
        if text != ":memory:":
            target = Path(text).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            text = str(target)
        self.path = text
        self._lock = RLock()
        self._conn = sqlite3.connect(text, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if text != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def ping(self) -> bool:
        try:
            return self.query("SELECT 1") == [(1,)]
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
