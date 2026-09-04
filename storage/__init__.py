from storage.base import (
    EventIdCollision,
    EventStore,
    SessionAlreadyExists,
    SessionInfo,
    SessionNotOpen,
    SessionStatus,
    StorageIntegrityError,
    UnknownSession,
)
from storage.memory import InMemoryEventStore
from storage.recorder import Recorder, RecorderAtCapacity
from storage.sqlite_store import SQLiteEventStore

__all__ = [
    "EventIdCollision",
    "EventStore",
    "InMemoryEventStore",
    "Recorder",
    "RecorderAtCapacity",
    "SQLiteEventStore",
    "SessionAlreadyExists",
    "SessionInfo",
    "SessionNotOpen",
    "SessionStatus",
    "StorageIntegrityError",
    "UnknownSession",
    "build_store",
]


def build_store(backend: str, *, sqlite_path: str = "", postgres_dsn: str = "") -> EventStore:
    """Factory used by the composition root."""
    if backend == "memory":
        return InMemoryEventStore()
    if backend == "sqlite":
        return SQLiteEventStore(sqlite_path or ":memory:")
    if backend == "postgres":
        from storage.postgres_store import PostgresEventStore

        return PostgresEventStore(postgres_dsn)
    raise ValueError(f"unknown storage backend: {backend}")
