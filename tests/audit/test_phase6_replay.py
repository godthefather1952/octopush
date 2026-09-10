"""H36, H37 — determinism, and execution truth that survives a replay.

WHAT DETERMINISM MEANS HERE
===========================
Given the same plan, the same market sequence, the same seed and the same
logical instants, execution must produce the same order statuses, the same
history timestamps, the same fill quantities and prices, the same fees, the
same slippage, the same account state, the same registry state and the same
snapshot.

Only identifiers proven to be intentionally variable are normalised.
``PaperOrder.client_order_id`` and ``ExecutionPlan.plan_id`` default to
``new_id(...)``, which ``core.ids`` allows replay to make reproducible — so the
audit pins them explicitly rather than normalising them away, and normalises
``FillEvent.fill_id`` only where the fixture cannot pin it.

**Nothing economic and no timestamp is normalised.** A test that normalised a
price would be testing that two runs agree about nothing in particular.

WHAT THIS MODULE DOES NOT DO
============================
It does not drive the replay engine. Phase 2 validated that separately, and
this pass may not modify it. What it does is exercise the property replay
depends on: that execution driven from recorded logical instants reconstructs
identically, whatever the wall clock is doing.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.models.common import Side, TimeInForce
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    two_venue_market,
    venue_state,
)

#: The wall-clock offsets an "original" and a "replay" run are given. Any
#: difference between the two runs is something replay cannot reproduce.
ORIGINAL_OFFSET = 0
REPLAY_OFFSET = 37_000


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


def _fingerprint(harness, plan_id: str, at: int) -> dict[str, Any]:
    """Everything about execution truth that must be reproducible."""
    orders = sorted(
        harness.orders_of(plan_id), key=lambda o: o.client_order_id
    )
    snapshot = harness.veska.execution_snapshot(at)
    record = harness.veska.get_plan(plan_id)
    return {
        "orders": [
            {
                "id": o.client_order_id,
                "status": o.status.value,
                "created_at": o.created_at,
                "submitted_at": o.submitted_at,
                "acknowledged_at": o.acknowledged_at,
                "expires_at": o.expires_at,
                "terminal_at": o.terminal_at,
                "history": [(ts, s.value) for ts, s in o.history],
                "quantity": o.quantity,
                "filled_quantity": o.filled_quantity,
                "average_price": o.average_price,
                "fees_paid": o.fees_paid,
                "fills": [
                    {
                        "created_at": f.created_at,
                        "quantity": f.quantity,
                        "price": f.price,
                        "fee": f.fee,
                        "liquidity": f.liquidity.value,
                        "slippage_bps": f.slippage_bps,
                        "source_data_timestamp": f.source_data_timestamp,
                    }
                    for f in o.fills
                ],
            }
            for o in orders
        ],
        "plan": {
            "status": record.status.value if record else None,
            "order_ids": sorted(record.order_ids) if record else [],
            "terminal_at": record.terminal_at if record else None,
        },
        "account": {
            "cash": harness.account.cash,
            "realized_pnl": harness.account.realized_pnl,
            "fees_paid": harness.account.fees_paid,
            "fills_applied": harness.account.fills_applied,
            "positions": {
                key: (position.quantity, position.average_entry_price)
                for key, position in sorted(harness.account.positions.items())
            },
        },
        "snapshot": {
            "created_at": snapshot.created_at,
            "open": sorted(snapshot.open_order_ids),
            "outstanding": sorted(snapshot.outstanding_order_ids),
            "unknown": sorted(snapshot.unknown_order_ids),
            "counts": dict(sorted(snapshot.counts_by_status.items())),
            "fills_applied": snapshot.fills_applied,
            "orders_created": snapshot.orders_created,
            "duplicate_fills": snapshot.duplicate_fills,
            "illegal_transitions": snapshot.illegal_transitions,
            "active_plans": sorted(snapshot.active_plan_ids),
            "unresolved_plans": sorted(snapshot.unresolved_plan_ids),
        },
    }


# ======================================================================
# scenarios — one per execution outcome Phase 6 can produce
# ======================================================================


def _book_full_fill(ts: int):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((99.0, 100.0)),
            asks=price_levels((100.0, 100.0)),
            as_of=ts,
        ),
        created_at=ts,
    )


def _book_thin(ts: int):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((99.0, 100.0)),
            asks=price_levels((100.0, 0.2), (200.0, 100.0)),
            as_of=ts,
        ),
        created_at=ts,
    )


def _book_away(ts: int):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=ts,
        ),
        created_at=ts,
    )


SCENARIOS: dict[str, dict[str, Any]] = {
    "full_fill": {
        "book": _book_full_fill,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.IOC,
            "limit_price": 105.0,
        },
    },
    "partial_fill": {
        "book": _book_thin,
        "order": {
            "quantity": 5.0,
            "time_in_force": TimeInForce.IOC,
            "limit_price": 100.5,
        },
    },
    "no_fill": {
        "book": _book_away,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.IOC,
            "limit_price": 95.0,
        },
    },
    "ioc": {
        "book": _book_full_fill,
        "order": {
            "quantity": 2.0,
            "time_in_force": TimeInForce.IOC,
            "limit_price": 101.0,
        },
    },
    "post_only": {
        "book": _book_away,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.POST_ONLY,
            "limit_price": 95.0,
            "expected_price": 95.0,
            "ttl_ms": 600_000,
        },
    },
    "expiry": {
        "book": _book_away,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.GTC,
            "limit_price": 95.0,
            "ttl_ms": 300,
        },
        "polls": (300, 5_000),
    },
    "cancel_before_ack": {
        "book": _book_full_fill,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.IOC,
            "limit_price": 105.0,
        },
        "cancel_at": 1,
    },
    "cancel_race": {
        "book": _book_full_fill,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.GTC,
            "limit_price": 105.0,
            "ttl_ms": 600_000,
        },
        "cancel_after_ack": True,
    },
    "unknown": {
        "book": _book_away,
        "order": {
            "quantity": 1.0,
            "time_in_force": TimeInForce.GTC,
            "limit_price": 95.0,
            "ttl_ms": 600_000,
        },
        "inject_unknown": True,
    },
}


async def _run(name: str, *, clock_offset: int) -> dict[str, Any]:
    """One deterministic execution run, driven entirely from logical time."""
    scenario = SCENARIOS[name]
    harness = build_harness(clock_ms=T0 + clock_offset, seed=4242)
    latency = _latency(harness)

    harness.update_market(scenario["book"](T0))
    plan = execution_plan(
        planned_order(client_order_id="ord-fixed", **scenario["order"]),
        created_at=T0,
        plan_id="plan-fixed",
        max_slippage_bps=1_000.0,
    )
    await harness.veska.execute(plan, T0)

    if "cancel_at" in scenario:
        await harness.veska.cancel("ord-fixed", T0 + scenario["cancel_at"])
    if scenario.get("inject_unknown"):
        harness.executor.inject_timeout("ord-fixed")

    polls = scenario.get("polls", (latency, latency + 1_000))
    for index, offset in enumerate(polls):
        harness.update_market(scenario["book"](T0 + offset))
        await harness.veska.poll(T0 + offset)
        if index == 0 and scenario.get("cancel_after_ack"):
            await harness.veska.cancel("ord-fixed", T0 + offset + 1)

    at = T0 + polls[-1] + 10
    harness.veska.refresh_plan("plan-fixed", at)
    return _fingerprint(harness, "plan-fixed", at)


class TestDeterminism:
    """H36 — the same inputs must produce the same execution truth."""

    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    async def test_two_identical_runs_agree_exactly(self, scenario: str):
        first = await _run(scenario, clock_offset=ORIGINAL_OFFSET)
        second = await _run(scenario, clock_offset=ORIGINAL_OFFSET)
        assert first == second, (
            f"two identical runs of {scenario!r} disagreed, so nothing "
            "downstream can be reproducible either"
        )

    @pytest.mark.parametrize("scenario", sorted(SCENARIOS))
    async def test_a_replay_under_a_different_wall_clock_agrees(
        self, scenario: str
    ):
        """H37 — replay in miniature.

        Same plan, same market sequence, same seed, same logical instants;
        only the wired clock differs, exactly as it does between an original
        run and a replay of it. Any divergence is something replay cannot
        reconstruct.
        """
        original = await _run(scenario, clock_offset=ORIGINAL_OFFSET)
        replayed = await _run(scenario, clock_offset=REPLAY_OFFSET)
        assert original == replayed, (
            f"{scenario!r} produced different execution truth when the wired "
            f"clock was {REPLAY_OFFSET}ms ahead, with identical logical times"
        )

    async def test_the_seed_is_what_makes_the_simulator_reproducible(self):
        """Different seeds may differ; identical seeds must not.

        Asserted so a passing determinism suite cannot be explained by the
        simulator having become deterministic for some other reason.
        """
        from execution.paper.simulator import FillSimulator

        settings = build_harness().settings
        a = FillSimulator(settings.execution, seed=1)
        b = FillSimulator(settings.execution, seed=1)
        c = FillSimulator(settings.execution, seed=2)
        draws_a = [a.rng.random() for _ in range(20)]
        draws_b = [b.rng.random() for _ in range(20)]
        draws_c = [c.rng.random() for _ in range(20)]
        assert draws_a == draws_b
        assert draws_a != draws_c


class TestMultiVenueDeterminism:
    """A two-leg plan across venues with different latencies."""

    async def _run_two_leg(self, clock_offset: int) -> dict[str, Any]:
        harness = build_harness(clock_ms=T0 + clock_offset, seed=99)
        latency = max(_latency(harness, VENUE_A), _latency(harness, VENUE_B))
        harness.update_market(two_venue_market(created_at=T0))
        plan = execution_plan(
            planned_order(
                venue=VENUE_A,
                side=Side.BUY,
                quantity=0.5,
                limit_price=101.0,
                client_order_id="leg-a",
            ),
            planned_order(
                venue=VENUE_B,
                side=Side.SELL,
                quantity=0.5,
                limit_price=99.0,
                client_order_id="leg-b",
            ),
            created_at=T0,
            plan_id="plan-two",
            max_slippage_bps=1_000.0,
        )
        await harness.veska.execute(plan, T0)
        for offset in (latency, latency + 500, latency + 2_000):
            harness.update_market(two_venue_market(created_at=T0 + offset))
            await harness.veska.poll(T0 + offset)
        at = T0 + latency + 2_010
        harness.veska.refresh_plan("plan-two", at)
        return _fingerprint(harness, "plan-two", at)

    async def test_a_multi_venue_plan_replays_identically(self):
        original = await self._run_two_leg(ORIGINAL_OFFSET)
        replayed = await self._run_two_leg(REPLAY_OFFSET)
        assert original == replayed


class TestOrderOfEvaluationIsStable:
    """Poll iterates a dict; its order must not decide an economic outcome."""

    async def test_several_orders_competing_for_thin_liquidity_are_stable(self):
        """The same three orders, the same thin book, twice."""

        async def run() -> list[tuple[str, float]]:
            harness = build_harness(seed=7)
            harness.update_market(_book_thin(T0))
            plan = execution_plan(
                *[
                    planned_order(
                        quantity=2.0,
                        time_in_force=TimeInForce.IOC,
                        limit_price=100.5,
                        client_order_id=f"ord-{index}",
                    )
                    for index in range(3)
                ],
                created_at=T0,
                plan_id="plan-race",
                max_slippage_bps=1_000.0,
            )
            await harness.veska.execute(plan, T0)
            await harness.veska.poll(T0 + _latency(harness))
            return sorted(
                (o.client_order_id, o.filled_quantity)
                for o in harness.orders_of("plan-race")
            )

        assert await run() == await run()
