"""Replay: recording, deterministic ordering, and reproducibility.

The claim replay has to earn is that running the same recorded market through
the same code twice produces the same answer — and that changing the code is
therefore the only thing that can change the answer.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.events import MARKET_INPUT_TYPES, Event, EventType
from replay import ReplaySession, collect, config_digest
from simulation.market import default_market
from storage import InMemoryEventStore, SQLiteEventStore
from storage.recorder import Recorder
from tests.conftest import START_MS, run_platform


def summary(platform) -> dict:
    """Full state fingerprint, identifiers included.

    The identifiers matter. An earlier version of this helper omitted them,
    so `test_replay_is_repeatable` passed while every replay produced entirely
    different fill, order and opportunity ids — the exact property the test
    claimed to verify. Anything excluded here must be excluded deliberately
    and for a stated reason.
    """
    account = platform.account.snapshot()
    return {
        "opportunities": len(platform.state.opportunities),
        "orders": len(platform.oms.orders),
        "fills": len(platform.account.fill_log),
        "cash": round(account.cash, 9),
        "realized_pnl": round(account.realized_pnl, 9),
        "fees": round(account.fees_paid, 9),
        "positions": {
            key: [round(position.quantity, 12), round(position.average_entry_price, 9)]
            for key, position in sorted(account.positions.items())
        },
        # --- identity ---
        "fill_ids": [f.fill_id for f in platform.account.fill_log],
        "order_ids": sorted(platform.oms.orders),
        "opportunity_ids": sorted(platform.state.opportunities),
        "intent_ids": sorted(
            r.intent.intent_id for r in platform.state.opportunities.values() if r.intent
        ),
        "decision_ids": sorted(
            r.decision.decision_id
            for r in platform.state.opportunities.values()
            if r.decision
        ),
        # --- correlation chains ---
        "fill_correlations": [f.correlation_id or "" for f in platform.account.fill_log],
        "order_plans": sorted(
            (o.client_order_id, o.plan_id or "", o.intent_id or "")
            for o in platform.oms.orders.values()
        ),
    }


IDENTITY_FIELDS = (
    "fill_ids",
    "order_ids",
    "opportunity_ids",
    "intent_ids",
    "decision_ids",
    "fill_correlations",
    "order_plans",
)


def economic_summary(platform) -> dict:
    """The fingerprint with identity deliberately excluded.

    Used only to compare two *live* runs. Live runs mint random identifiers by
    design (see core/ids.py), so their ids cannot match and must not be
    asserted on. Every excluded field is named in IDENTITY_FIELDS rather than
    silently omitted — the omission being silent is what let the replay
    determinism defect survive.
    """
    return {k: v for k, v in summary(platform).items() if k not in IDENTITY_FIELDS}


def fresh_platform(settings, *, seed_start: int = START_MS, store=None):
    clock = ManualClock(seed_start)
    return build_platform(
        settings.model_copy(update={"venues": simulated_venues()}),
        clock=clock,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=store or InMemoryEventStore(),
        market=default_market(start_ms=seed_start),
        raise_on_handler_error=True,
    )


class TestRecording:
    async def test_a_session_records_market_inputs(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 40)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        assert events
        types = {e.type for e in events}
        assert types & MARKET_INPUT_TYPES
        assert EventType.MARKET_STATE in types

    async def test_every_event_is_ordered_deterministically(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 30)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        keys = [e.sort_key() for e in events]
        assert keys == sorted(keys)

    async def test_sequence_breaks_ties_within_a_millisecond(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 20)
        await platform.recorder.flush()
        events = await collect(platform.store, platform.session_id)
        same_ms = [e for e in events if e.ts_ms == events[0].ts_ms]
        sequences = [e.sequence for e in same_ms]
        assert len(set(sequences)) == len(sequences)

    async def test_session_metadata_records_the_config_digest(self, settings):
        platform = fresh_platform(settings)
        await run_platform(platform, 5)
        await platform.recorder.stop()
        info = await platform.store.session(platform.session_id)
        assert info is not None
        # model_dump(), not mode="json": the DSN is a SecretStr and the json
        # dump masks every secret to the same asterisks, so a digest taken
        # from it could not tell two different stores apart (P0-L2).
        assert info.config_hash == config_digest(platform.settings.model_dump())
        assert info.ended_at is not None

    async def test_sqlite_round_trips_events(self, settings, tmp_path):
        store = SQLiteEventStore(str(tmp_path / "events.db"))
        platform = fresh_platform(settings, store=store)
        await run_platform(platform, 25)
        await platform.recorder.flush()
        events = await collect(store, platform.session_id)
        assert len(events) > 10
        # Reopening reads the same data back from disk.
        await store.close()
        reopened = SQLiteEventStore(str(tmp_path / "events.db"))
        again = await collect(reopened, platform.session_id)
        assert [e.id for e in again] == [e.id for e in events]
        await reopened.close()

    async def test_recorder_survives_a_failing_store(self, clock):
        class BrokenStore(InMemoryEventStore):
            async def append_many(self, session_id, events):
                raise RuntimeError("disk on fire")

        recorder = Recorder(store=BrokenStore(), clock=clock, buffer_size=1)
        await recorder.start()
        await recorder.record(
            Event(type=EventType.SYSTEM_EVENT, ts_ms=clock.now_ms(), source="test")
        )
        # The platform keeps running, but the failure is visible.
        assert recorder.failures == 1
        assert recorder.healthy is False


class TestDeterminism:
    async def test_two_identical_live_runs_agree(self, settings):
        a = fresh_platform(settings)
        b = fresh_platform(settings)
        await run_platform(a, 120)
        await run_platform(b, 120)
        # Economic state only: two live runs mint independent random ids.
        assert economic_summary(a) == economic_summary(b)

    async def test_two_live_runs_do_not_share_identifiers(self, settings):
        """The converse guarantee: live ids are unique, not reproducible."""
        a = fresh_platform(settings)
        b = fresh_platform(settings)
        await run_platform(a, 60)
        await run_platform(b, 60)
        ids_a = {f.fill_id for f in a.account.fill_log}
        ids_b = {f.fill_id for f in b.account.fill_log}
        assert ids_a and ids_b
        assert not (ids_a & ids_b), "live runs must not reuse identifiers"

    async def test_a_different_seed_produces_a_different_market(self, settings):
        a = fresh_platform(settings)
        clock = ManualClock(START_MS)
        b = build_platform(
            settings.model_copy(update={"venues": simulated_venues()}),
            clock=clock,
            bus=InMemoryEventBus(raise_on_handler_error=True),
            store=InMemoryEventStore(),
            market=default_market(seed=999, start_ms=START_MS),
            raise_on_handler_error=True,
        )
        await run_platform(a, 120)
        await run_platform(b, 120)
        assert economic_summary(a) != economic_summary(b)


async def _record_session(settings, ticks: int = 150):
    platform = fresh_platform(settings)
    await run_platform(platform, ticks)
    await platform.recorder.flush()
    return platform


async def _replay_session(settings, store, session_id: str):
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    platform = build_platform(
        settings.model_copy(update={"venues": simulated_venues()}),
        clock=clock,
        bus=bus,
        store=InMemoryEventStore(),
        raise_on_handler_error=True,
    )
    # Feeds stay off: the recording is the market.
    await platform.start(record=False, feeds=False)
    session = ReplaySession(store=store, bus=bus, clock=clock, session_id=session_id)
    with session:
        await session.open()
        while True:
            event = await session.step()
            if event is None:
                break
            if event.type is EventType.ORCHESTRATOR_TICK:
                # Tick at the SAME logical boundaries the original run
                # ticked at -- never one tick per market event.
                await platform.orchestrator.tick()
    return platform, session


class TestReplaySession:
    async def _record(self, settings, ticks: int = 150):
        return await _record_session(settings, ticks)

    async def _replay(self, settings, store, session_id: str):
        return await _replay_session(settings, store, session_id)

    async def test_replay_reproduces_market_state(self, settings):
        recorded = await self._record(settings, ticks=100)
        replayed, session = await self._replay(settings, recorded.store, recorded.session_id)
        assert session.stats.events_published > 0
        # The replayed books reach the same prices as the recorded ones.
        for key, original in recorded.state.market.venues.items():
            reproduced = replayed.state.market.venues.get(key)
            assert reproduced is not None, key
            assert reproduced.metrics.best_bid == pytest.approx(original.metrics.best_bid)
            assert reproduced.metrics.best_ask == pytest.approx(original.metrics.best_ask)

    async def test_replay_is_repeatable(self, settings):
        recorded = await self._record(settings, ticks=120)
        first, _ = await self._replay(settings, recorded.store, recorded.session_id)
        second, _ = await self._replay(settings, recorded.store, recorded.session_id)
        assert summary(first) == summary(second)

    async def test_replay_clock_never_runs_backwards(self, settings):
        recorded = await self._record(settings, ticks=60)
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus()
        session = ReplaySession(
            store=recorded.store, bus=bus, clock=clock, session_id=recorded.session_id
        )
        await session.open()
        seen: list[int] = []
        while True:
            event = await session.step()
            if event is None:
                break
            seen.append(clock.now_ms())
        assert seen == sorted(seen)

    async def test_only_market_inputs_are_replayed(self, settings):
        recorded = await self._record(settings, ticks=40)
        clock = ManualClock(START_MS)
        bus = InMemoryEventBus()
        published: list[Event] = []
        bus.subscribe(lambda e: published.append(e) or _noop(), name="probe")
        session = ReplaySession(
            store=recorded.store, bus=bus, clock=clock, session_id=recorded.session_id
        )
        await session.run()
        # Derived state is recomputed, never replayed back at the platform.
        assert published
        assert all(e.type in MARKET_INPUT_TYPES for e in published)

    async def test_step_mode_advances_exactly_one_event(self, settings):
        """``step()`` always returns exactly one item -- but with
        input-visibility verified, a single call can now do more internal
        work than "read one stored item": every market input preceding the
        first tick marker is held back (deferred) until that marker's
        watermark releases it, and all of them become ready together the
        moment the first call reaches that marker. So the first ``step()``
        call can legitimately apply several inputs internally (each still
        one of THIS tick's own, per its watermark) before returning the
        first of them; ``events_published == 1`` no longer holds, but
        ``events_published <= events_read`` and "one call returns one
        event" both still do.
        """
        recorded = await self._record(settings, ticks=30)
        clock = ManualClock(START_MS)
        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=clock,
            session_id=recorded.session_id,
        )
        await session.open()
        first = await session.step()
        assert first is not None
        assert first.type not in (EventType.ORCHESTRATOR_TICK,)
        assert 1 <= session.stats.events_published <= session.stats.events_read

    async def test_replay_finishes_and_reports_its_span(self, settings):
        recorded = await self._record(settings, ticks=50)
        clock = ManualClock(START_MS)
        session = ReplaySession(
            store=recorded.store,
            bus=InMemoryEventBus(),
            clock=clock,
            session_id=recorded.session_id,
        )
        stats = await session.run()
        assert session.finished
        assert stats.span_ms > 0
        # events_read also includes ORCHESTRATOR_TICK boundary markers,
        # which are read but never published to the bus as data.
        assert stats.events_published + stats.ticks_read == stats.events_read
        assert stats.ticks_read == 50


def replay_equivalence_summary(platform) -> dict:
    """Structural fingerprint for an ORIGINAL-run-vs-REPLAY comparison.

    Normalizes away only what is intentionally nondeterministic between a
    live run and its replay: literal identifier VALUES. A live run mints
    random ids and a replay installs a deterministic generator derived from
    the session (see ``core/ids.py``) -- by design, neither is meant to
    match the other string-for-string, and asserting on the raw id strings
    here would be asserting on something nobody claims replay reproduces.

    Everything else -- every economic field, and the STRUCTURAL order
    opportunities/orders/fills occurred in -- is preserved and compared
    positionally rather than dropped: both runs process the same recorded
    market events in the same order, so the Nth order/fill/opportunity in
    one run corresponds exactly to the Nth in the other. Silently comparing
    only aggregate totals would hide a defect that reorders or substitutes
    individual trades while leaving sums unchanged.
    """
    portfolio = platform.account.snapshot()
    records = list(platform.state.opportunities.values())
    return {
        "ticks": platform.orchestrator.ticks,
        "opportunity_states": [r.state.value for r in records],
        "risk_verdicts": [
            r.decision.verdict.value for r in records if r.decision is not None
        ],
        "orders": [
            (o.venue, o.symbol, o.side.value, round(o.quantity, 9), o.status.value)
            for o in platform.oms.orders.values()
        ],
        "fills": [
            (
                f.venue,
                f.symbol,
                f.side.value,
                round(f.quantity, 9),
                round(f.price, 9),
                round(f.fee, 9),
            )
            for f in platform.account.fill_log
        ],
        "positions": {
            key: [
                round(p.quantity, 9),
                round(p.average_entry_price, 9),
                round(p.realized_pnl, 6),
            ]
            for key, p in sorted(portfolio.positions.items())
        },
        "cash": round(portfolio.cash, 6),
        "equity": round(portfolio.equity, 6),
        "realized_pnl": round(portfolio.realized_pnl, 6),
        "unrealized_pnl": round(portfolio.unrealized_pnl, 6),
        "gross_pnl": round(portfolio.gross_pnl, 6),
        "net_pnl": round(portfolio.net_pnl, 6),
        "drawdown": round(portfolio.drawdown, 6),
        "kill_switch_engaged": platform.kill_switch.state.engaged,
        "kill_switch_triggered_by": sorted(platform.kill_switch.state.triggered_by),
    }


class TestReplayEconomicEquivalence:
    """The invariant the mandate actually cares about.

    ``test_replay_is_repeatable`` (above) only proves replay is repeatable
    with ITSELF -- both runs share the exact same tick-cadence bug if one
    exists, so it cannot catch a wrong cadence. ``test_replay_reproduces_market_state``
    only proves market-DATA reconstruction, which is decision-independent
    (TIDAL applies book deltas the same way no matter how many orchestrator
    ticks ran in between). Neither is proof that replay reproduces what the
    ORIGINAL run actually decided. This test compares replay directly
    against the run it replayed.
    """

    async def test_replayed_run_reproduces_the_original_runs_decisions(self, settings):
        recorded = await _record_session(settings, ticks=800)
        replayed, session = await _replay_session(settings, recorded.store, recorded.session_id)

        assert session.stats.timeline_fidelity is None, (
            "a session recorded with tick markers must replay as verified, "
            "not fall back to legacy semantics"
        )
        assert session.stats.ticks_read == recorded.orchestrator.ticks
        assert session.stats.ticks_read == replayed.orchestrator.ticks

        original = replay_equivalence_summary(recorded)
        reproduced = replay_equivalence_summary(replayed)
        assert original["fills"], (
            "the scenario must actually produce trades for this to prove anything"
        )
        assert reproduced == original


async def _noop() -> None:
    return None
