"""Replay identity determinism — the regression for P0-C2.

The audit ran one recorded session through three replays and found byte-
identical economic state with completely different identifiers on every run,
because every id was `uuid4`. That made a replay impossible to diff against
its own recording, entity by entity.

These tests assert the property the old `test_replay_is_repeatable` claimed
but did not check.
"""

from __future__ import annotations

import hashlib
import json

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import simulated_venues
from core.ids import (
    DeterministicIdGenerator,
    RandomIdGenerator,
    current_generator,
    deterministic_ids,
    new_id,
)
from replay import ReplaySession
from simulation.market import default_market
from storage import InMemoryEventStore
from tests.conftest import START_MS, run_platform


def fingerprint(platform) -> str:
    """Everything, identifiers and correlation chains included."""
    account = platform.account
    state = {
        "cash": round(account.cash, 9),
        "realized": round(account.realized_pnl, 9),
        "fees": round(account.fees_paid, 9),
        "positions": {
            k: [round(v.quantity, 12), round(v.average_entry_price, 9)]
            for k, v in sorted(account.positions.items())
        },
        "fills": [
            [
                f.fill_id,
                f.client_order_id,
                f.correlation_id or "",
                f.venue,
                f.symbol,
                f.side.value,
                round(f.quantity, 12),
                round(f.price, 9),
                round(f.fee, 9),
                f.created_at,
            ]
            for f in account.fill_log
        ],
        "orders": sorted(
            [o.client_order_id, o.plan_id or "", o.intent_id or "", o.status.value]
            for o in platform.oms.orders.values()
        ),
        "opportunities": sorted(
            [
                r.opportunity.opportunity_id,
                r.state.value,
                r.rejected_reason or "",
                r.intent.intent_id if r.intent else "",
                r.decision.decision_id if r.decision else "",
            ]
            for r in platform.state.opportunities.values()
        ),
        "ticks": platform.orchestrator.ticks,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def record_session(settings, ticks: int = 220):
    clock = ManualClock(START_MS)
    store = InMemoryEventStore()
    platform = build_platform(
        settings.model_copy(update={"venues": simulated_venues()}),
        clock=clock,
        bus=InMemoryEventBus(raise_on_handler_error=True),
        store=store,
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )
    await run_platform(platform, ticks)
    # Finalise: exact replay refuses a session that was never closed out,
    # since an unfinalised session is missing whatever the recorder still
    # held (Phase 2 Batch 2).
    await platform.recorder.stop()
    return platform, store


async def replay_once(settings, store, session_id: str, *, id_seed: str = ""):
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    platform = build_platform(
        settings.model_copy(update={"venues": simulated_venues()}),
        clock=clock,
        bus=bus,
        store=InMemoryEventStore(),
        raise_on_handler_error=True,
    )
    await platform.start(record=False, feeds=False)
    session = ReplaySession(
        store=store, bus=bus, clock=clock, session_id=session_id, id_seed=id_seed
    )
    with session:
        await session.open()
        while True:
            if await session.step() is None:
                break
            await platform.orchestrator.tick()
    return platform


class TestIdGenerator:
    def test_random_ids_are_full_width_and_unique(self):
        generator = RandomIdGenerator()
        ids = {generator.new_id("fill") for _ in range(5_000)}
        assert len(ids) == 5_000
        # 128 bits of uuid4 hex, not the previous 48-bit truncation.
        sample = next(iter(ids))
        assert len(sample.split("-", 1)[1]) == 32

    def test_deterministic_ids_reproduce_for_one_seed(self):
        a = DeterministicIdGenerator("session-x")
        b = DeterministicIdGenerator("session-x")
        assert [a.new_id("fill") for _ in range(10)] == [
            b.new_id("fill") for _ in range(10)
        ]

    def test_different_seeds_give_different_ids(self):
        a = DeterministicIdGenerator("session-x")
        b = DeterministicIdGenerator("session-y")
        assert a.new_id("fill") != b.new_id("fill")

    def test_namespaces_have_independent_counters(self):
        """Adding a call in one namespace must not shift another's ids."""
        a = DeterministicIdGenerator("s")
        first_opp = a.new_id("opp")
        b = DeterministicIdGenerator("s")
        b.new_id("fill")
        b.new_id("fill")
        assert b.new_id("opp") == first_opp

    def test_reset_restarts_the_sequence(self):
        generator = DeterministicIdGenerator("s")
        first = generator.new_id("fill")
        generator.reset()
        assert generator.new_id("fill") == first

    def test_scope_restores_the_previous_generator(self):
        before = current_generator()
        with deterministic_ids("scoped"):
            assert current_generator().deterministic
        assert current_generator() is before
        assert not current_generator().deterministic

    def test_module_new_id_follows_the_installed_generator(self):
        with deterministic_ids("abc"):
            first = [new_id("ord") for _ in range(3)]
        with deterministic_ids("abc"):
            second = [new_id("ord") for _ in range(3)]
        assert first == second


class TestReplayIdentityDeterminism:
    async def test_three_replays_are_byte_identical_including_identity(
        self, settings, store
    ):
        """The headline regression: identity, not just economics."""
        _, recorded = await record_session(settings)
        session_id = (await recorded.sessions())[0].session_id

        fingerprints = []
        for _ in range(3):
            platform = await replay_once(settings, recorded, session_id)
            fingerprints.append(fingerprint(platform))

        assert len(set(fingerprints)) == 1, (
            "replays diverged: " + " ".join(f[:16] for f in fingerprints)
        )

    async def test_replay_identifiers_are_reproducible_entity_by_entity(
        self, settings, store
    ):
        _, recorded = await record_session(settings)
        session_id = (await recorded.sessions())[0].session_id

        a = await replay_once(settings, recorded, session_id)
        b = await replay_once(settings, recorded, session_id)

        assert [f.fill_id for f in a.account.fill_log] == [
            f.fill_id for f in b.account.fill_log
        ]
        assert sorted(a.oms.orders) == sorted(b.oms.orders)
        assert sorted(a.state.opportunities) == sorted(b.state.opportunities)

    async def test_correlation_chains_are_reproducible(self, settings, store):
        """A fill must trace back to the same opportunity on every replay."""
        _, recorded = await record_session(settings)
        session_id = (await recorded.sessions())[0].session_id

        a = await replay_once(settings, recorded, session_id)
        b = await replay_once(settings, recorded, session_id)

        chain_a = [(f.fill_id, f.client_order_id, f.correlation_id) for f in a.account.fill_log]
        chain_b = [(f.fill_id, f.client_order_id, f.correlation_id) for f in b.account.fill_log]
        assert chain_a == chain_b

    async def test_an_explicit_seed_separates_two_comparison_runs(
        self, settings, store
    ):
        """Comparing two code versions side by side needs distinguishable ids."""
        _, recorded = await record_session(settings, ticks=120)
        session_id = (await recorded.sessions())[0].session_id

        a = await replay_once(settings, recorded, session_id, id_seed="v1")
        b = await replay_once(settings, recorded, session_id, id_seed="v2")
        if a.account.fill_log:
            assert a.account.fill_log[0].fill_id != b.account.fill_log[0].fill_id

    async def test_replay_does_not_leak_its_generator_into_the_process(
        self, settings, store
    ):
        _, recorded = await record_session(settings, ticks=60)
        session_id = (await recorded.sessions())[0].session_id
        before = current_generator()
        await replay_once(settings, recorded, session_id)
        assert current_generator() is before
        assert not current_generator().deterministic

    async def test_live_runs_still_use_random_identifiers(self, platform):
        """Determinism must not weaken live uniqueness."""
        await run_platform(platform, 120)
        assert not current_generator().deterministic
        ids = [f.fill_id for f in platform.account.fill_log]
        assert len(ids) == len(set(ids))
