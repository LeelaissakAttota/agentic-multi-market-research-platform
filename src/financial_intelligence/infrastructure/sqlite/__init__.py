"""SQLite persistence adapters (stdlib ``sqlite3``; opt-in via STORAGE_BACKEND=sqlite)."""

from financial_intelligence.infrastructure.sqlite.codec import CodecError
from financial_intelligence.infrastructure.sqlite.database import SqliteDatabase
from financial_intelligence.infrastructure.sqlite.stores import (
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
