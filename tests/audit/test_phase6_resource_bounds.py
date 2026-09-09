"""H19, H20 — resident execution state must be bounded without forgetting UNKNOWN.

Batch C releases PaperExecutor timing records under the same compaction decision
that archives terminal OMS orders. A terminal order with no unsealed fills
loses both resident OMS state and its ``_pending`` timing record.

UNKNOWN remains deliberately different: it is non-terminal, so its order,
pending timing and unresolved plan all survive compaction. The retention fix
therefore bounds completed-session history without turning memory cleanup into
a route for forgetting unresolved venue truth.
"""

from __future__ import annotations

import inspect

from core.models.common import TimeInForce
from core.models.execution import ExecutionPlanStatus, OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    build_harness,
    execution_plan,
    fill_for,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)

#: Enough completed orders that a linear structure is unmistakable, and few
#: enough that the suite stays fast.
MANY = 2_000


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
        created_at=created_at,
    )


async def _churn(harness, count: int) -> None:
    """Submit and terminate ``count`` orders, one plan each."""
    harness.update_market(_quiet())
    latency = _latency(harness)
    for index in range(count):
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                ttl_ms=latency + 1,
                client_order_id=f"o-{index}",
            ),
            created_at=T0,
            plan_id=f"p-{index}",
        )
        await harness.veska.execute(plan, T0)
    # One poll past every TTL retires the lot.
    await harness.veska.poll(T0 + latency + 10)


class TestWhatTheCodeClaims:
    """Static inventory of the retention story."""

    def test_the_oms_claims_a_bounded_resident_set(self):
        from execution.oms import OrderManager

        doc = inspect.getdoc(OrderManager) or ""
        assert "bounded by concurrent activity" in doc

    def test_the_executor_compacts_pending_under_the_oms_decision(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.compact_terminal_state)
        assert "compactable_order_ids" in source
        assert "self._pending.pop" in source

    def test_the_dedupe_window_is_explicitly_bounded(self):
        from execution.oms import DEFAULT_DEDUPE_FILLS, OrderManager

        assert DEFAULT_DEDUPE_FILLS == 10_000
        source = inspect.getsource(OrderManager._trim_dedupe)
        assert "popleft" in source


class TestGrowthAfterManyTerminalOrders:
    """H19 — what is still resident once the session has run."""

    async def test_terminal_orders_are_compactable_out_of_memory(self):
        harness = build_harness()
        await _churn(harness, MANY)

        assert len(harness.oms.orders) == MANY
        released = harness.executor.compact_terminal_state(unsealed_fills=set())

        assert released == MANY
        assert len(harness.oms.orders) == 0
        assert harness.oms.archived.count == MANY

    async def test_lifetime_counters_survive_compaction(self):
        """Aggregates must not be lost with the records."""
        harness = build_harness()
        await _churn(harness, 200)
        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert harness.oms.orders_created == 200
        assert harness.oms.archived.count == 200
        assert sum(harness.oms.archived.by_status.values()) == 200

    async def test_the_pending_map_is_released_with_compacted_terminal_orders(self):
        """Fast-loop timing state must scale with outstanding work, not history."""
        harness = build_harness()
        await _churn(harness, MANY)
        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert len(harness.oms.orders) == 0
        assert len(harness.executor._pending) == 0, (
            f"{len(harness.executor._pending)} venue-timing records remain "
            f"for {MANY} orders that are all terminal and all compacted out "
            "of the OMS: the fast-loop structure scales with session history"
        )

    async def test_the_registry_still_holds_every_plan_after_compaction(self):
        """Recorded, not condemned: the hook exists and the policy is deferred.

        With ``keep_terminal`` at its default the registry releases nothing,
        which is the documented construction-phase choice. The measurement is
        what that costs over a session.
        """
        harness = build_harness()
        await _churn(harness, MANY)
        harness.veska.refresh_all_plans(T0 + _latency(harness) + 10)
        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert harness.veska.registry.resident_plans == MANY
        assert harness.veska.registry.compact() == 0
        assert len(harness.veska.registry.plans) == MANY

    async def test_the_registry_releases_terminal_plans_when_asked(self):
        harness = build_harness()
        await _churn(harness, 200)
        harness.veska.refresh_all_plans(T0 + _latency(harness) + 10)

        released = harness.veska.registry.compact(keep_terminal=False)

        assert released > 0
        assert harness.veska.registry.resident_plans == 200 - released

    async def test_the_fill_dedupe_window_stays_bounded(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                quantity=100.0,
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                ttl_ms=600_000,
                client_order_id="o-many-fills",
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        order = harness.oms.get("o-many-fills")
        harness.oms.dedupe_fills = 50

        for index in range(200):
            harness.oms.apply_fill(
                fill_for(
                    order,
                    quantity=0.01,
                    price=95.0,
                    now_ms=T0 + index,
                    fill_id=f"f-{index}",
                )
            )

        assert len(harness.oms._applied_fills) <= 50
        assert len(harness.oms._dedupe_order) <= 50


class TestUnknownSurvivesEverything:
    """H20 — retention must never be a route to forgetting."""

    async def _one_unknown_among_many(self, harness, count: int = 200):
        await _churn(harness, count)
        harness.update_market(_quiet(T0 + 1_000))
        plan = execution_plan(
            planned_order(
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                ttl_ms=600_000,
                client_order_id="o-unknown",
            ),
            created_at=T0 + 1_000,
            plan_id="p-unknown",
        )
        await harness.veska.execute(plan, T0 + 1_000)
        harness.executor.inject_timeout("o-unknown")
        await harness.veska.poll(T0 + 1_000 + _latency(harness))
        harness.veska.refresh_all_plans(T0 + 1_000 + _latency(harness))
        return plan

    async def test_compaction_keeps_the_unknown_order(self):
        harness = build_harness()
        await self._one_unknown_among_many(harness)

        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert harness.oms.get("o-unknown") is not None
        assert harness.oms.get("o-unknown").status is OrderStatus.UNKNOWN
        assert "o-unknown" in {
            o.client_order_id for o in harness.executor.outstanding_orders()
        }

    async def test_compaction_keeps_the_unknown_orders_pending_record(self):
        """Its arrival and cancel schedule are still needed if it resolves."""
        harness = build_harness()
        await self._one_unknown_among_many(harness)

        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert "o-unknown" in harness.executor._pending

    async def test_aggressive_registry_compaction_keeps_the_unresolved_plan(self):
        harness = build_harness()
        await self._one_unknown_among_many(harness)

        harness.veska.registry.compact(keep_terminal=False)

        record = harness.veska.get_plan("p-unknown")
        assert record is not None
        assert record.status is ExecutionPlanStatus.UNKNOWN
        assert "p-unknown" in {
            r.plan_id for r in harness.veska.unresolved_plans()
        }

    async def test_an_unsealed_fill_keeps_its_order_resident(self):
        """The condition that keeps the OMS and the ledger in step."""
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                ttl_ms=_latency(harness) + 1,
                client_order_id="o-sealed",
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        order = harness.oms.get("o-sealed")
        harness.oms.apply_fill(
            fill_for(
                order, quantity=1.0, price=95.0, now_ms=T0 + 1, fill_id="f-unsealed"
            )
        )
        assert order.is_terminal

        released = harness.executor.compact_terminal_state(
            unsealed_fills={"f-unsealed"}
        )

        assert released == 0
        assert harness.oms.get("o-sealed") is not None
        assert "o-sealed" in harness.executor._pending

    async def test_the_same_order_is_released_once_its_fill_is_sealed(self):
        harness = build_harness()
        harness.update_market(_quiet())
        plan = execution_plan(
            planned_order(
                quantity=1.0,
                time_in_force=TimeInForce.GTC,
                limit_price=95.0,
                ttl_ms=_latency(harness) + 1,
                client_order_id="o-sealed-2",
            ),
            created_at=T0,
        )
        await harness.veska.execute(plan, T0)
        await harness.veska.poll(T0 + _latency(harness))
        order = harness.oms.get("o-sealed-2")
        harness.oms.apply_fill(
            fill_for(
                order, quantity=1.0, price=95.0, now_ms=T0 + 1, fill_id="f-sealed"
            )
        )

        released = harness.executor.compact_terminal_state(unsealed_fills=set())

        assert released == 1
        assert harness.oms.get("o-sealed-2") is None
        assert "o-sealed-2" not in harness.executor._pending
        assert harness.oms.archived.fills == 1


class TestFastLoopWorkDoesNotScaleWithHistory:
    """The property the retention story exists to deliver."""

    async def test_the_accrual_loop_walks_only_live_orders(self):
        """``_accrue_trade_flow`` iterates ``oms.live_orders()``.

        That is the right set — but ``live_orders`` itself is a full scan of
        the resident map, so the per-update cost is linear in residents rather
        than in live orders. Recorded as a measurement, not a verdict.
        """
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor._accrue_trade_flow)
        assert "self.oms.live_orders()" in source

        from execution.oms import OrderManager

        live = inspect.getsource(OrderManager.live_orders)
        assert "for o in self.orders.values()" in live

    async def test_poll_iterates_every_resident_order(self):
        """The fast loop's per-tick cost, stated plainly."""
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.poll)
        assert "for order in list(self.oms.orders.values()):" in source

    async def test_a_compacted_session_leaves_the_poll_loop_short(self):
        """After compaction the loop is bounded by what is still working."""
        harness = build_harness()
        await _churn(harness, 500)
        harness.executor.compact_terminal_state(unsealed_fills=set())

        assert len(harness.oms.orders) == 0
        await harness.veska.poll(T0 + 100_000)
        assert len(harness.oms.orders) == 0
