from storage.base import EventStore, SessionInfo
from storage.memory import InMemoryEventStore
from storage.recorder import Recorder
from storage.sqlite_store import SQLiteEventStore

__all__ = [
    "EventStore",
    "InMemoryEventStore",
    "Recorder",
    "SQLiteEventStore",
    "SessionInfo",
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
