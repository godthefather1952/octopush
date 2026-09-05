"""Replay a recorded session.

    python -m replay --list
    python -m replay --session session-abc123
    python -m replay --session session-abc123 --max-items 500
    python -m replay --session session-abc123 --realtime --speed 2

Replay drives recorded market inputs back through a freshly constructed
pipeline.  Everything downstream is *recomputed*, so running the same session
against two versions of the code is a direct comparison of the code.

Exact replay is the default and every departure from it must be asked for by
name. A session whose recording integrity, tick timeline, input visibility,
starting state, envelope schema or configuration cannot be verified is refused
with an error naming the flag that accepts the risk; taking one of those flags
makes the run non-exact, and the summary says which dimensions are unverified.
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
    ReplayConfigError,
    ReplayMode,
    ReplaySchemaCompatibilityError,
    ReplaySession,
    UnverifiedRecordingError,
    config_digest,
)
from storage import build_store
from storage.base import SessionStatus

log = logging.getLogger("replay")

#: Every refusal the engine raises before replaying anything. Each message
#: names the flag that accepts that specific loss of fidelity deliberately.
REFUSALS = (
    LegacyTimelineRequired,  # LegacyInputVisibilityRequired subclasses this
    PartialReplayUnsupported,
    UnverifiedRecordingError,
    ReplaySchemaCompatibilityError,
    ReplayConfigError,
)


def _source_store(settings):
    return build_store(
        settings.storage.backend,
        sqlite_path=settings.storage.sqlite_path,
        postgres_dsn=settings.storage.postgres_dsn.get_secret_value(),
    )


async def list_sessions() -> None:
    settings = load_settings()
    store = _source_store(settings)
    await store.open()
    try:
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
    finally:
        await store.close()


def _output_store(settings, args):
    """Where ``--record-output`` writes, or ``None`` when not recording.

    A replay's own output is a session in its own right -- something to
    compare against the original, or against another build's replay -- which
    it can only be if it outlives the process. Recording it into an in-memory
    store looked like it worked and produced nothing (P2-5).
    """
    if not args.record_output:
        # No output recording at all: the replayed platform still needs a
        # store object, and an in-memory one that is never written to and
        # never read back is the honest choice.
        return build_store("memory"), "none"
    backend = settings.storage.backend
    if backend == "memory":
        raise SystemExit(
            "--record-output needs a durable storage backend, but "
            "storage.backend is 'memory': the recorded replay would be "
            "discarded when this process exits. Configure storage.backend as "
            "'sqlite' or 'postgres' (see core/config/settings.py) and run "
            "again."
        )
    return _source_store(settings), backend


async def replay(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format, service="replay")

    # Checked before anything is opened: whether --record-output CAN be
    # durable is a pure configuration question, and answering it first means
    # the operator hears the real reason rather than "no such session"
    # because the memory backend they asked for holds no sessions at all.
    output_store, output_backend = _output_store(settings, args)

    store = _source_store(settings)
    await store.open()
    platform = None
    session = None
    try:
        info = await store.session(args.session)
        if info is None:
            raise SystemExit(f"no such session: {args.session}")

        clock = ManualClock(info.started_at)
        bus = InMemoryEventBus()
        # A durable output session must never be mistaken for a recording of
        # the whole source session, so a bounded run says so in the label
        # itself -- the one piece of metadata that travels with the row.
        label = f"replay-of-{args.session}"
        if args.max_items:
            label += f" (partial: first {args.max_items} items)"
        # Built BEFORE the replay session opens, so the output session's id is
        # minted by the live random generator rather than by the deterministic
        # one replay installs -- two replays of one source session must not
        # collide on a single output session id.
        platform = build_platform(
            settings,
            clock=clock,
            bus=bus,
            store=output_store,
            session_label=label,
        )

        session = ReplaySession(
            store=store,
            bus=bus,
            clock=clock,
            session_id=args.session,
            mode=ReplayMode.REALTIME if args.realtime else ReplayMode.FAST,
            speed=args.speed,
            legacy_timeline=args.legacy_timeline,
            legacy_input_visibility=args.legacy_input_visibility,
            allow_partial_range=args.allow_partial_range,
            allow_incomplete_session=args.allow_incomplete_session,
            current_config_hash=config_digest(settings.model_dump()),
            allow_config_mismatch=args.counterfactual,
        )
        try:
            await session.open()
        except REFUSALS as exc:
            raise SystemExit(str(exc)) from exc

        # Only NOW does the output recorder start. Starting it earlier meant a
        # refused replay still finalised a durable output session, and
        # finalised it COMPLETE -- a recording claiming to be a complete
        # replay of a run that never happened. Adapters stay stopped either
        # way: the recorded events *are* the market.
        await platform.start(record=args.record_output, feeds=False)

        limit = args.max_items or None
        items = 0
        while limit is None or items < limit:
            event = await session.step()
            if event is None:
                break
            items += 1
            if event.type is EventType.ORCHESTRATOR_TICK:
                # A tick boundary, not a market input: replay the original
                # cadence exactly, one orchestrator.tick() per marker -- never
                # one per market event (that was the defect this marker exists
                # to fix).
                await platform.orchestrator.tick()

        stopped_early = limit is not None and items >= limit and not session.finished
        summary = _summary(args, info, session, platform, items, stopped_early)
    finally:
        # Each resource is released independently: one failing to close must
        # not strand the others, and the process-global state replay installed
        # has to come back whatever happened above (P2-4).
        if session is not None:
            session.close()
        if platform is not None:
            try:
                await platform.stop()
            except Exception:
                log.exception("failed to stop the replay platform")
        await store.close()
        if output_store is not None and output_store is not store:
            try:
                await output_store.close()
            except Exception:
                log.exception("failed to close the replay output store")

    if args.record_output:
        summary.update(await _output_summary(settings, platform, output_backend))
    print(json.dumps(summary, indent=2))


def _summary(args, info, session, platform, items, stopped_early) -> dict:
    portfolio = platform.account.snapshot()
    reconciliation = platform.marin.reconcile()
    fidelity = session.stats.fidelity
    return {
        "session": args.session,
        "config_hash_recorded": info.config_hash,
        "recording_status": info.status.value,
        "mode": session.mode.value,
        "speed": session.speed if session.mode is ReplayMode.REALTIME else None,
        "items_replayed": items,
        "stopped_early": stopped_early,
        "events_replayed": session.stats.events_published,
        "ticks_replayed": session.stats.ticks_read,
        # The authoritative answer, dimension by dimension. A single string
        # could name only one problem, and several can apply at once.
        "is_exact": fidelity.is_exact and not stopped_early,
        "fidelity": fidelity.as_dict(),
        "fidelity_issues": fidelity.reasons,
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


async def _output_summary(settings, platform, backend) -> dict:
    """Read back what the output recording actually became.

    Reported from storage rather than from the recorder's own counters: the
    question is what survived the process, and only the store can answer it.
    """
    session_id = platform.session_id
    store = _source_store(settings)
    await store.open()
    try:
        info = await store.session(session_id)
        return {
            "output_session_id": session_id,
            "output_backend": backend,
            "output_recording_status": (
                info.status.value if info is not None else SessionStatus.OPEN.value
            ),
            "output_event_count": info.event_count if info is not None else 0,
        }
    finally:
        await store.close()


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated so it can be tested without running a replay.

    Every fidelity flag here accepts a specific, named loss of fidelity, and
    each one exists because the engine refuses that case by default. A flag
    whose ``dest`` stopped matching what ``replay()`` reads would silently
    disable the escape hatch and leave the refusal unanswerable.
    """
    parser = argparse.ArgumentParser(description="Replay a recorded trading session")
    parser.add_argument("--list", action="store_true", help="list recorded sessions")
    parser.add_argument("--session", help="session id to replay")
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help=(
            "stop after N logical replay items. One item is one applied input "
            "or one ORCHESTRATOR_TICK boundary -- ticks count, because they "
            "are the original run's decision cadence. 0 (the default) replays "
            "the whole session"
        ),
    )
    # Two counters used to exist (--step and --max-events) with subtly
    # different accounting: --step counted only market events and both were
    # checked in the same loop. Aliases, so existing commands keep working
    # while meaning exactly one thing.
    parser.add_argument(
        "--step",
        type=int,
        dest="max_items",
        help="deprecated alias for --max-items",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        dest="max_items",
        help="deprecated alias for --max-items",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        help=(
            "delay the host between logical instants so the replay unfolds at "
            "the recorded pace. Changes only how long the command takes: the "
            "replayed result is identical to the default FAST mode"
        ),
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help=(
            "REALTIME pacing divisor: 2 replays a recorded second in half a "
            "second, 0.5 in two seconds. Must be positive, and is meaningful "
            "only with --realtime"
        ),
    )
    parser.add_argument(
        "--record-output",
        action="store_true",
        help=(
            "record the replay as its own durable session in the configured "
            "storage backend, under a new session id labelled "
            "replay-of-<source>. Refused when the backend is 'memory', where "
            "the recording would not survive the process"
        ),
    )
    parser.add_argument(
        "--counterfactual",
        action="store_true",
        help=(
            "replay under configuration that differs from (or cannot be "
            "compared with) the configuration the session was recorded under, "
            "accepting that the run answers 'what would this market have done "
            "under these settings' rather than reproducing the session"
        ),
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
            "not an exact reproduction"
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
    if args.speed <= 0:
        parser.error("--speed must be positive")
    if not args.realtime and args.speed != 1.0:
        # Accepting it silently would suggest the replay was paced when it
        # was not. Saying so costs one line and removes the ambiguity.
        parser.error("--speed only applies with --realtime")
    if args.max_items < 0:
        parser.error("--max-items cannot be negative")
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
