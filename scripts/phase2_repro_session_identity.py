"""Phase 2 audit reproduction: double start_session() diverges across backends.

Not a permanent test -- a one-shot reproduction script for the audit report.
Proves, against real SQLite and real PostgreSQL (plus the in-memory store),
that calling start_session() a second time on an already-ended session
resets ended_at to NULL in two backends but not the third.
"""

from __future__ import annotations

import asyncio
import sys

from storage.memory import InMemoryEventStore
from storage.postgres_store import PostgresEventStore
from storage.sqlite_store import SQLiteEventStore

DSN = "postgresql://postgres:postgres@localhost:5432/octopush_test"


async def probe(name: str, store) -> None:
    await store.open()
    sid = f"repro-session-identity-{name}"
    await store.start_session(sid, started_at=1000, label="first", config_hash="hash-a")
    await store.end_session(sid, ended_at=2000)
    info = await store.session(sid)
    print(f"[{name}] after end_session:      ended_at={info.ended_at}")

    # A second start_session() call on the SAME session_id -- e.g. a crashed
    # process restarting and reusing an id, or any caller error.
    await store.start_session(sid, started_at=1500, label="second", config_hash="hash-b")
    info2 = await store.session(sid)
    print(f"[{name}] after 2nd start_session: ended_at={info2.ended_at}  "
          f"started_at={info2.started_at}  config_hash={info2.config_hash}")
    await store.close()


async def main() -> None:
    print("=" * 70)
    print("Reproduction: double start_session() on an already-ended session")
    print("=" * 70)
    await probe("memory", InMemoryEventStore())
    await probe("sqlite", SQLiteEventStore(":memory:"))
    try:
        await probe("postgres", PostgresEventStore(DSN))
    except Exception as exc:
        print(f"[postgres] SKIPPED: {exc}")
        return

    print("-" * 70)
    print("If memory/sqlite show ended_at=None after the 2nd start_session but")
    print("postgres still shows ended_at=2000, the three backends disagree on")
    print("whether a duplicate start_session() call can silently un-end an")
    print("already-completed session -- confirmed backend-parity defect.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
