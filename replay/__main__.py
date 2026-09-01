"""Replay a recorded session.

    python -m replay --list
    python -m replay --session session-abc123
    python -m replay --session session-abc123 --step 500

Replay drives recorded market inputs back through a freshly constructed
pipeline.  Everything downstream is *recomputed*, so running the same session
against two versions of the code is a direct comparison of the code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings
from core.logging import configure_logging
from replay.engine import ReplayMode, ReplaySession
from storage import build_store

log = logging.getLogger("replay")


async def list_sessions() -> None:
    settings = load_settings()
    store = build_store(
        settings.storage.backend,
        sqlite_path=settings.storage.sqlite_path,
        postgres_dsn=settings.storage.postgres_dsn,
    )
    await store.open()
    sessions = await store.sessions()
    if not sessions:
        print("no recorded sessions")
    for info in sessions:
        span = (info.ended_at - info.started_at) / 1000 if info.ended_at else None
        print(
            f"{info.session_id}  events={info.event_count:<8} "
            f"started={info.started_at}  duration={span if span is None else round(span, 1)}s  "
            f"config={info.config_hash}  {info.label}"
        )
    await store.close()


async def replay(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format, service="replay")

    store = build_store(
        settings.storage.backend,
        sqlite_path=settings.storage.sqlite_path,
        postgres_dsn=settings.storage.postgres_dsn,
    )
    await store.open()
    info = await store.session(args.session)
    if info is None:
        raise SystemExit(f"no such session: {args.session}")

    clock = ManualClock(info.started_at)
    bus = InMemoryEventBus()
    # A fresh in-memory store: the replay's own output must not be written
    # back over the session being replayed.
    platform = build_platform(
        settings,
        clock=clock,
        bus=bus,
        store=build_store("memory"),
        session_label=f"replay-of-{args.session}",
    )
    # Adapters stay stopped: recorded events *are* the market.
    await platform.start(record=args.record_output, feeds=False)

    session = ReplaySession(
        store=store,
        bus=bus,
        clock=clock,
        session_id=args.session,
        mode=ReplayMode.STEP if args.step else ReplayMode.FAST,
    )
    await session.open()

    processed = 0
    while True:
        event = await session.step()
        if event is None:
            break
        processed += 1
        await platform.orchestrator.tick()
        if args.step and processed >= args.step:
            break
        if args.max_events and processed >= args.max_events:
            break

    portfolio = platform.account.snapshot()
    reconciliation = platform.marin.reconcile()
    summary = {
        "session": args.session,
        "config_hash_recorded": info.config_hash,
        "events_replayed": session.stats.events_published,
        "span_ms": session.stats.span_ms,
        "ticks": platform.orchestrator.ticks,
        "opportunities": len(platform.state.opportunities),
        "orders": len(platform.oms.orders),
        "fills": len(platform.account.fill_log),
        "net_pnl": round(portfolio.net_pnl, 6),
        "fees": round(portfolio.fees_paid, 6),
        "drawdown": round(portfolio.drawdown, 6),
        "reconciliation_ok": reconciliation.ok,
        "kill_switch": platform.kill_switch.state.triggered_by,
    }
    print(json.dumps(summary, indent=2))
    await platform.stop()
    await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a recorded trading session")
    parser.add_argument("--list", action="store_true", help="list recorded sessions")
    parser.add_argument("--session", help="session id to replay")
    parser.add_argument("--step", type=int, default=0, help="replay only N events")
    parser.add_argument("--max-events", type=int, default=0, help="stop after N events")
    parser.add_argument(
        "--record-output", action="store_true", help="record the replay as its own session"
    )
    args = parser.parse_args()
    if args.list:
        asyncio.run(list_sessions())
        return
    if not args.session:
        parser.error("--session is required unless --list is given")
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
