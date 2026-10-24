"""SQLite persistence adapters (stdlib ``sqlite3``; opt-in via STORAGE_BACKEND=sqlite)."""

from amrp.infrastructure.sqlite.codec import CodecError
from amrp.infrastructure.sqlite.database import SqliteDatabase
from amrp.infrastructure.sqlite.stores import (
    SqliteApiKeyStore,
    SqliteResearchMemoryStore,
    SqliteResearchWorkflowStore,
    SqliteWatchlistStore,
    SqliteWorkflowReportStore,
    hash_api_key,
)

__all__ = [
    "CodecError",
    "SqliteApiKeyStore",
    "SqliteDatabase",
    "SqliteResearchMemoryStore",
    "SqliteResearchWorkflowStore",
    "SqliteWatchlistStore",
    "SqliteWorkflowReportStore",
    "hash_api_key",
]
