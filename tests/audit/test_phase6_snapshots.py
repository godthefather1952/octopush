"""H28, H29 — the snapshot and the query vocabulary must agree with the OMS.

H28: ONE INSTANT, ONE ANSWER
============================
``execution_snapshot(now_ms)`` is the surface Phase 7 reconciliation consumes.
For one logical instant it must agree with the OMS, the registry and the
executor about open ids, outstanding ids, UNKNOWN ids, status counts, active
and unresolved plans, and the lifetime counters. Its ``created_at`` must be the
instant supplied, never one read from anywhere.

H29: THE VOCABULARY
===================
Five order queries with five different meanings::

    all_orders()          every resident order
    open_orders()         known to be working              (excludes UNKNOWN)
    outstanding_orders()  final truth not known            (includes UNKNOWN)
    unknown_orders()      exactly the UNKNOWN ones
    terminal_orders()     stopped moving                   (never UNKNOWN)

Two properties must hold across every state: no order disappears from all of
them, and no order appears terminal while it is UNKNOWN.
"""

from __future__ import annotations

from core.models.common import Side, TimeInForce
from core.models.execution import ExecutionPlanStatus, OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    fill_for,
    market_state,
    planned_order,
    price_levels,
    two_venue_market,
    venue_state,
)


def _latency(harness, venue: str = VENUE_A) -> int:
    return harness.settings.venue(venue).latency_ms


def _quiet(created_at: int = T0):
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=created_at,
        ),
        venue_state(
            venue=VENUE_B,
            bids=price_levels((90.0, 10.0)),
            asks=price_levels((110.0, 10.0)),
            as_of=created_at,
        ),
        created_at=created_at,
    )


async def _mixed_estate(harness):
    """One plan per interesting state: working, filled, cancelled, unknown."""
    harness.update_market(_quiet())
    latency = _latency(harness)

    working = execution_plan(
        planned_order(
            time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000,
            client_order_id="o-working",
        ),
        created_at=T0,
        plan_id="p-working",
    )
    unknown = execution_plan(
        planned_order(
            time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000,
            client_order_id="o-unknown",
        ),
        created_at=T0,
        plan_id="p-unknown",
    )
    cancelled = execution_plan(
        planned_order(
            time_in_force=TimeInForce.GTC, limit_price=95.0, ttl_ms=600_000,
            client_order_id="o-cancelled",
        ),
        created_at=T0,
        plan_id="p-cancelled",
    )
    for plan in (working, unknown, cancelled):
        await harness.veska.execute(plan, T0)

    harness.executor.inject_timeout("o-unknown")
    await harness.veska.poll(T0 + latency)

    cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
    await harness.veska.cancel("o-cancelled", T0 + latency + 1)
    await harness.veska.poll(T0 + latency + 1 + cancel_latency)
    harness.veska.refresh_all_plans(T0 + latency + 1 + cancel_latency)
    return T0 + latency + 1 + cancel_latency


class TestSnapshotTimestamp:
    """The instant is supplied, never read."""

    async def test_created_at_is_exactly_the_supplied_instant(self):
        harness = build_harness()
        harness.update_market(_quiet())
        for instant in (T0, T0 + 1, T0 + 999_999):
            snapshot = harness.veska.execution_snapshot(instant)
            assert snapshot.created_at == instant

    async def test_the_snapshot_does_not_move_when_the_clock_does(self):
        harness = build_harness()
        harness.update_market(_quiet())
        first = harness.veska.execution_snapshot(T0)
        harness.set_clock(T0 + 60_000)
        second = harness.veska.execution_snapshot(T0)
        assert first.created_at == second.created_at == T0


class TestSnapshotAgreesWithTheOms:
    """H28 — every field, against the book of record."""

    async def test_the_id_sets_match_the_oms(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)

        assert set(snapshot.open_order_ids) == {
            o.client_order_id for o in harness.oms.live_orders()
        }
        assert set(snapshot.outstanding_order_ids) == {
            o.client_order_id for o in harness.oms.outstanding_orders()
        }
        assert set(snapshot.unknown_order_ids) == {
            o.client_order_id for o in harness.oms.unknown_orders()
        }

    async def test_open_is_a_subset_of_outstanding(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert set(snapshot.open_order_ids) <= set(snapshot.outstanding_order_ids)

    async def test_the_difference_between_them_is_exactly_the_unknown_set(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        difference = set(snapshot.outstanding_order_ids) - set(
            snapshot.open_order_ids
        )
        assert difference == set(snapshot.unknown_order_ids)
        assert difference == {"o-unknown"}

    async def test_the_status_counts_match_the_resident_orders(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert snapshot.counts_by_status == harness.oms.counts_by_status()
        assert sum(snapshot.counts_by_status.values()) == len(
            harness.oms.all_orders()
        )

    async def test_the_summaries_cover_every_resident_order(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert {s.client_order_id for s in snapshot.orders} == {
            o.client_order_id for o in harness.oms.all_orders()
        }

    async def test_the_lifetime_counters_match_the_oms(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert snapshot.orders_created == harness.oms.orders_created
        assert snapshot.fills_applied == harness.oms.fills_applied
        assert snapshot.duplicate_fills == harness.oms.duplicate_fills
        assert snapshot.illegal_transitions == harness.oms.illegal_transitions
        assert snapshot.archived_orders == harness.oms.archived.count

    async def test_the_plan_id_sets_match_the_registry(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert set(snapshot.active_plan_ids) == {
            r.plan_id for r in harness.veska.active_plans()
        }
        assert set(snapshot.unresolved_plan_ids) == {
            r.plan_id for r in harness.veska.unresolved_plans()
        }
        assert "p-unknown" in snapshot.unresolved_plan_ids
        assert "p-working" in snapshot.active_plan_ids

    async def test_active_and_unresolved_plan_sets_are_disjoint(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert set(snapshot.active_plan_ids) & set(
            snapshot.unresolved_plan_ids
        ) == set()

    async def test_the_embedded_metrics_match_a_fresh_call(self):
        harness = build_harness()
        at = await _mixed_estate(harness)
        snapshot = harness.veska.execution_snapshot(at)
        assert snapshot.metrics == harness.veska.metrics()

    async def test_a_duplicate_fill_is_visible_in_the_snapshot(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC, limit_price=95.0,
                client_order_id="o-dup", ttl_ms=600_000,
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        order = harness.oms.get("o-dup")
        fill = fill_for(
            order, quantity=0.2, price=95.0, now_ms=T0 + 500, fill_id="f-dup"
        )
        harness.oms.apply_fill(fill)
        harness.oms.apply_fill(fill)

        snapshot = harness.veska.execution_snapshot(T0 + 600)
        assert snapshot.duplicate_fills == 1
        assert snapshot.fills_applied == 1


class TestQueryVocabulary:
    """H29 — the five surfaces, and what must always hold across them."""

    async def test_no_order_disappears_from_every_surface(self):
        harness = build_harness()
        await _mixed_estate(harness)

        everything = {o.client_order_id for o in harness.executor.all_orders()}
        covered = (
            {o.client_order_id for o in harness.oms.live_orders()}
            | {o.client_order_id for o in harness.oms.unknown_orders()}
            | {o.client_order_id for o in harness.oms.terminal_orders()}
        )
        assert everything == covered, (
            f"orders {everything - covered} are resident but appear in no "
            "state-specific query surface"
        )

    async def test_no_unknown_order_appears_terminal(self):
        harness = build_harness()
        await _mixed_estate(harness)
        terminal = {o.client_order_id for o in harness.oms.terminal_orders()}
        unknown = {o.client_order_id for o in harness.oms.unknown_orders()}
        assert terminal & unknown == set()

    async def test_live_and_terminal_are_disjoint(self):
        harness = build_harness()
        await _mixed_estate(harness)
        live = {o.client_order_id for o in harness.oms.live_orders()}
        terminal = {o.client_order_id for o in harness.oms.terminal_orders()}
        assert live & terminal == set()

    async def test_outstanding_is_live_plus_unknown_exactly(self):
        harness = build_harness()
        await _mixed_estate(harness)
        live = {o.client_order_id for o in harness.oms.live_orders()}
        unknown = {o.client_order_id for o in harness.oms.unknown_orders()}
        outstanding = {o.client_order_id for o in harness.oms.outstanding_orders()}
        assert outstanding == live | unknown

    async def test_orders_for_plan_returns_exactly_that_plans_orders(self):
        harness = build_harness()
        await _mixed_estate(harness)
        assert {o.client_order_id for o in harness.executor.orders_for_plan("p-working")} == {
            "o-working"
        }
        assert harness.executor.orders_for_plan("p-nonexistent") == []

    async def test_get_order_and_all_orders_agree(self):
        harness = build_harness()
        await _mixed_estate(harness)
        for order in harness.executor.all_orders():
            assert (
                harness.executor.get_order(order.client_order_id) is order
            )
        assert harness.executor.get_order("nope") is None

    async def test_veska_and_the_executor_answer_identically(self):
        """VESKA's queries must be pass-throughs, not a second opinion."""
        harness = build_harness()
        await _mixed_estate(harness)
        assert [o.client_order_id for o in harness.veska.open_orders()] == [
            o.client_order_id for o in harness.executor.open_orders()
        ]
        assert [o.client_order_id for o in harness.veska.outstanding_orders()] == [
            o.client_order_id for o in harness.executor.outstanding_orders()
        ]
        assert [o.client_order_id for o in harness.veska.unknown_orders()] == [
            o.client_order_id for o in harness.executor.unknown_orders()
        ]


class TestMetricsConsistency:
    """The counters a future operator surface would read."""

    async def test_order_and_plan_counts_agree_with_the_registries(self):
        harness = build_harness()
        await _mixed_estate(harness)
        metrics = harness.veska.metrics()

        assert metrics.orders_created == len(harness.executor.all_orders())
        assert metrics.orders_outstanding == len(
            harness.executor.outstanding_orders()
        )
        assert metrics.orders_unknown == len(harness.executor.unknown_orders())
        assert metrics.plans_created == harness.veska.registry.plans_registered

    async def test_orders_created_counts_residents_not_lifetime_creations(self):
        """A naming distinction worth pinning: it is not the OMS's counter.

        ``ExecutionMetrics.orders_created`` is computed as
        ``sum(1 for _ in resident)``, so it falls when orders are compacted
        while ``OrderManager.orders_created`` does not. Recorded so a reader
        of the metric knows which question it answers.
        """
        harness = build_harness()
        await _mixed_estate(harness)
        assert harness.veska.metrics().orders_created == len(
            harness.executor.all_orders()
        )
        assert harness.oms.orders_created >= harness.veska.metrics().orders_created


class TestSnapshotUnderEveryOrderState:
    """Sweep the state space, so no state is unrepresented."""

    async def test_a_rejected_order_appears_terminal_and_not_outstanding(self):
        harness = build_harness()
        harness.update_market(_quiet())
        harness.executor.execution_disabled = True
        plan = execution_plan(
            planned_order(client_order_id="o-rejected"), created_at=T0
        )
        await harness.veska.execute(plan, T0)

        snapshot = harness.veska.execution_snapshot(T0)
        assert "o-rejected" not in snapshot.outstanding_order_ids
        assert "o-rejected" not in snapshot.open_order_ids
        assert harness.oms.get("o-rejected").status is OrderStatus.REJECTED

    async def test_a_filled_order_leaves_the_outstanding_set(self):
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(
                quantity=0.5,
                side=Side.BUY,
                time_in_force=TimeInForce.IOC,
                limit_price=101.0,
                client_order_id="o-filled",
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))

        order = harness.oms.get("o-filled")
        snapshot = harness.veska.execution_snapshot(T0 + _latency(harness))
        if order.status is OrderStatus.FILLED:
            assert "o-filled" not in snapshot.outstanding_order_ids
        else:
            assert "o-filled" in snapshot.outstanding_order_ids

    async def test_a_plan_with_no_orders_is_neither_active_nor_unresolved_wrongly(
        self,
    ):
        harness = build_harness()
        harness.update_market(_quiet())
        harness.veska.registry.register_plan(
            execution_plan(planned_order(), created_at=T0, plan_id="p-empty"), T0
        )
        record = harness.veska.get_plan("p-empty")
        assert record.status is ExecutionPlanStatus.CREATED
        assert record.is_active
        assert not record.is_unresolved
        assert not record.is_terminal
