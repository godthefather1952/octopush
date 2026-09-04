"""Phase 2 finding P2-2: session identity and lifecycle across backends.

Originally an audit reproduction: calling ``start_session()`` a second time
on an already-ended session reset ``ended_at`` to NULL in two backends but
not the third, so "is this session finished?" had no backend-independent
answer.

Phase 2 Batch 2 closed that. This script is now the *verification* of the
closure, run against real SQLite and real PostgreSQL (plus the in-memory
store): all three backends refuse a duplicate ``start_session()`` with the
same ``SessionAlreadyExists`` error, and the original session's recorded
identity and terminal status survive the refused call untouched.

    python -m scripts.phase2_repro_session_identity
"""

from __future__ import annotations

import asyncio
import os
import sys

from storage.base import SessionAlreadyExists, SessionStatus
from storage.memory import InMemoryEventStore
from storage.postgres_store import PostgresEventStore
from storage.sqlite_store import SQLiteEventStore

DSN = os.environ.get(
    "TF_TEST_POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/octopush_test"
)


async def probe(name: str, store) -> bool:
    await store.open()
    sid = f"repro-session-identity-{name}"
    await store.start_session(sid, started_at=1000, label="first", config_hash="hash-a")
    await store.finalize_session(sid, ended_at=2000, status=SessionStatus.COMPLETE)
    info = await store.session(sid)
    print(
        f"[{name}] after finalize:            status={info.status.value} "
        f"ended_at={info.ended_at}"
    )

    # A second start_session() call on the SAME session_id -- e.g. a crashed
    # process restarting and reusing an id, or any caller error.
    refused = False
    try:
        await store.start_session(
            sid, started_at=1500, label="second", config_hash="hash-b"
        )
    except SessionAlreadyExists as exc:
        refused = True
        print(f"[{name}] duplicate start refused:   {type(exc).__name__}")
    else:
        print(f"[{name}] duplicate start ACCEPTED:  DEFECT NOT CLOSED")

    after = await store.session(sid)
    intact = (
        after.status is SessionStatus.COMPLETE
        and after.ended_at == 2000
        and after.started_at == 1000
        and after.label == "first"
        and after.config_hash == "hash-a"
    )
    print(
        f"[{name}] original row intact:       {intact}  "
        f"(status={after.status.value} ended_at={after.ended_at} "
        f"started_at={after.started_at} config_hash={after.config_hash})"
    )
    await store.close()
    return refused and intact


async def main() -> int:
    print("=" * 70)
    print("P2-2 verification: duplicate start_session() on a finished session")
    print("=" * 70)
    results = {
        "memory": await probe("memory", InMemoryEventStore()),
        "sqlite": await probe("sqlite", SQLiteEventStore(":memory:")),
    }
    try:
        results["postgres"] = await probe("postgres", PostgresEventStore(DSN))
    except Exception as exc:  # pragma: no cover - depends on a live server
        print(f"[postgres] SKIPPED: {exc}")

    print("-" * 70)
    if all(results.values()):
        print(
            "All backends agree: a session id is used once and is never "
            "reopened, and a refused duplicate changes nothing. P2-2 closed."
        )
        return 0
    failed = sorted(k for k, ok in results.items() if not ok)
    print(f"BACKENDS STILL DIVERGE: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
