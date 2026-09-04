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
from core.events import EventType
from core.logging import configure_logging
from replay.engine import (
    LegacyTimelineRequired,
    PartialReplayUnsupported,
    ReplayMode,
    ReplaySession,
    UnverifiedRecordingError,
)
from storage import build_store

log = logging.getLogger("replay")


async def list_sessions() -> None:
    settings = load_settings()
    store = build_store(
        settings.storage.backend,
        sqlite_path=settings.storage.sqlite_path,
        postgres_dsn=settings.storage.postgres_dsn.get_secret_value(),
    )
    await store.open()
    sessions = await store.sessions()
    if not sessions:
        print("no recorded sessions")
    for info in sessions:
        span = (info.ended_at - info.started_at) / 1000 if info.ended_at else None
        # Status, not just ended_at: only COMPLETE means "every event the
        # recorder accepted is here", and that is what decides whether this
        # session can be replayed exactly (Phase 2 Batch 2).
        status = info.status.value
        if info.events_lost:
            status += f"(-{info.events_lost})"
        print(
            f"{info.session_id}  events={info.event_count:<8} "
            f"status={status:<20} "
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
        postgres_dsn=settings.storage.postgres_dsn.get_secret_value(),
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
        legacy_timeline=args.legacy_timeline,
        legacy_input_visibility=args.legacy_input_visibility,
        allow_partial_range=args.allow_partial_range,
        allow_incomplete_session=args.allow_incomplete_session,
    )
    try:
        await session.open()
    except (
        LegacyTimelineRequired,
        PartialReplayUnsupported,
        UnverifiedRecordingError,
    ) as exc:
        # LegacyInputVisibilityRequired is a LegacyTimelineRequired subclass,
        # and IncompleteSessionError / UnverifiedLegacySessionError are both
        # UnverifiedRecordingError. Each message names the flag that accepts
        # the risk deliberately.
        raise SystemExit(str(exc)) from exc

    # NOTE: session.close() is deliberately not called here -- that is a
    # separate, already-identified defect (global id-generator/log-clock
    # state leak) out of scope for this batch (see the Phase 2 audit
    # report's P2-4; CLI cleanup is its own later batch).
    processed = 0
    while True:
        event = await session.step()
        if event is None:
            break
        if event.type is EventType.ORCHESTRATOR_TICK:
            # A tick boundary, not a market input: replay the original
            # cadence exactly, one orchestrator.tick() per marker -- never
            # one per market event (that was the defect this marker exists
            # to fix).
            await platform.orchestrator.tick()
            continue
        processed += 1
        if args.step and processed >= args.step:
            break
        if args.max_events and processed >= args.max_events:
            break

    portfolio = platform.account.snapshot()
    reconciliation = platform.marin.reconcile()
    summary = {
        "session": args.session,
        "config_hash_recorded": info.config_hash,
        "recording_status": info.status.value,
        "events_replayed": session.stats.events_published,
        "ticks_replayed": session.stats.ticks_read,
        "timeline_fidelity": session.stats.timeline_fidelity,
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


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated so it can be tested without running a replay.

    Every flag here accepts a specific, named loss of fidelity, and each one
    exists because the engine refuses that case by default. A flag whose
    ``dest`` stopped matching what ``replay()`` reads would silently disable
    the escape hatch and leave the refusal unanswerable.
    """
    parser = argparse.ArgumentParser(description="Replay a recorded trading session")
    parser.add_argument("--list", action="store_true", help="list recorded sessions")
    parser.add_argument("--session", help="session id to replay")
    parser.add_argument("--step", type=int, default=0, help="replay only N events")
    parser.add_argument("--max-events", type=int, default=0, help="stop after N events")
    parser.add_argument(
        "--record-output", action="store_true", help="record the replay as its own session"
    )
    parser.add_argument(
        "--legacy-timeline",
        action="store_true",
        help=(
            "replay a session (or range) recorded before ORCHESTRATOR_TICK markers "
            "existed, accepting that its tick cadence is unverified rather than an "
            "exact reproduction of the original run"
        ),
    )
    parser.add_argument(
        "--legacy-input-visibility",
        action="store_true",
        help=(
            "replay a session whose tick markers predate the input-visibility "
            "watermark, accepting that the exact market-input cut each tick "
            "observed is unverified even though the tick cadence is not"
        ),
    )
    parser.add_argument(
        "--allow-partial-range",
        action="store_true",
        help=(
            "replay a ReplaySession range that starts after the session's true "
            "beginning, accepting that every component starts empty with no "
            "checkpoint of whatever state had accumulated before that point in "
            "the original run (this CLI does not itself expose a start-ms option; "
            "this flag matters only to callers constructing a ReplaySession "
            "programmatically with one)"
        ),
    )
    parser.add_argument(
        "--allow-incomplete-session",
        action="store_true",
        help=(
            "replay a session that is not verified COMPLETE -- one still OPEN, "
            "one finalised INCOMPLETE after losing events, or one recorded "
            "before recording integrity was tracked -- accepting that the "
            "recording may be missing history and that the run is therefore "
            "not an exact reproduction (the summary reports "
            "timeline_fidelity accordingly)"
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.list:
        asyncio.run(list_sessions())
        return
    if not args.session:
        parser.error("--session is required unless --list is given")
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
