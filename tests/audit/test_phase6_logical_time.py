"""H1 — one economic action must carry one logical timeline.

PaperExecutor has always promised to make execution decisions from caller-
supplied logical time. Batch C now carries that instant through the OMS write
boundary as well: creation, transitions, UNKNOWN state, rejection and fill-
driven transitions all accept explicit ``now_ms``.

OrderManager keeps a wired-clock fallback for compatibility with direct
non-execution callers, but every PaperExecutor economic mutation supplies the
logical action instant. The tests below therefore assert behavior and the
handoff itself rather than requiring the OMS to be clock-free globally.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from core.models.execution import OrderStatus
from execution import oms as oms_module
from execution.paper import executor as executor_module
from tests.audit.veska_fixtures import (
    T0,
    build_harness,
    execution_plan,
    planned_order,
    two_venue_market,
)

#: How far the wired clock is driven past logical execution time. Large enough
#: that no rounding or latency default could mask the divergence.
SKEW_MS = 5_000


class TestLogicalTimeHandoff:
    """Execution owns its time and supplies it to the OMS."""

    def test_the_executor_source_contains_no_clock_read(self):
        source = inspect.getsource(executor_module.PaperExecutor)
        assert "now_ms()" not in source
        assert "self.clock" not in source.replace("clock: Clock", "")

    def test_every_oms_execution_write_path_accepts_explicit_time(self):
        methods = (
            "create",
            "from_plan",
            "transition",
            "mark_unknown",
            "resolve_unknown",
            "reject",
            "apply_fill",
        )
        for name in methods:
            signature = inspect.signature(getattr(oms_module.OrderManager, name))
            assert "now_ms" in signature.parameters, name

    def test_the_executor_passes_time_into_every_oms_mutation_family(self):
        source = inspect.getsource(executor_module.PaperExecutor)
        assert "now_ms=now" in source
        assert "now_ms=now_ms" in source
        assert "self.oms.from_plan(" in source
        assert "self.oms.apply_fill(fill, now_ms=now_ms)" in source
        assert "self.oms.resolve_unknown(" in source


class TestOneActionOneTimeline:
    """A submission at T must be reconstructible from T."""

    async def test_created_at_matches_submitted_at(self):
        """The two halves of one submission must agree about when it happened."""
        harness = build_harness(clock_ms=T0)
        harness.update_market(two_venue_market(created_at=T0))
        plan = execution_plan(planned_order(), created_at=T0)

        harness.set_clock(T0 + SKEW_MS)
        await harness.veska.execute(plan, T0)

        order = harness.orders_of(plan.plan_id)[0]
        assert order.submitted_at == T0
        assert order.created_at == T0, (
            "the order was created by a submission performed at T, so its "
            f"created_at must be T ({T0}), not the clock's {order.created_at}"
        )

    async def test_the_ttl_is_measured_from_logical_submission_time(self):
        """``expires_at`` must be ``T + ttl_ms``, not ``clock + ttl_ms``.

        This is the economically load-bearing one. ``poll`` compares
        ``expires_at`` against the caller's ``now_ms``; if the deadline was
        set from a different clock, the order's real lifetime is neither the
        configured TTL nor anything replay can reproduce.
        """
        harness = build_harness(clock_ms=T0)
        harness.update_market(two_venue_market(created_at=T0))
        ttl = 5_000
        plan = execution_plan(planned_order(ttl_ms=ttl), created_at=T0)

        harness.set_clock(T0 + SKEW_MS)
        await harness.veska.execute(plan, T0)

        order = harness.orders_of(plan.plan_id)[0]
        assert order.expires_at == T0 + ttl, (
            f"an order submitted at {T0} with a {ttl}ms TTL must expire at "
            f"{T0 + ttl}; it expires at {order.expires_at}, which is "
            f"{order.expires_at - (T0 + ttl)}ms later because the deadline "
            "came from the wired clock rather than from execution time"
        )

    async def test_history_timestamps_do_not_precede_or_exceed_the_action(self):
        """Every history entry for one action must carry that action's instant."""
        harness = build_harness(clock_ms=T0)
        harness.update_market(two_venue_market(created_at=T0))
        plan = execution_plan(planned_order(), created_at=T0)

        harness.set_clock(T0 + SKEW_MS)
        await harness.veska.execute(plan, T0)

        order = harness.orders_of(plan.plan_id)[0]
        stamps = [ts for ts, _ in order.history]
        assert all(ts == T0 for ts in stamps), (
            "one submission at T produced history entries at "
            f"{sorted(set(stamps))}, so the order's own record disagrees "
            "about when it was created"
        )

    async def test_acknowledgement_and_transition_stamps_agree(self):
        """``acknowledged_at`` is explicit; the OPEN transition is not."""
        harness = build_harness(clock_ms=T0)
        harness.update_market(two_venue_market(created_at=T0))
        plan = execution_plan(planned_order(), created_at=T0)
        await harness.veska.execute(plan, T0)

        ack_time = T0 + 1_000
        harness.set_clock(T0 + SKEW_MS)
        await harness.veska.poll(ack_time)

        order = harness.orders_of(plan.plan_id)[0]
        if order.acknowledged_at is None:
            # Latency has not elapsed; nothing to compare, and the test says so
            # rather than passing vacuously.
            raise AssertionError(
                "the order was not acknowledged by ack_time; the audit's "
                "latency assumption is wrong and the case below is untested"
            )
        opened = [ts for ts, status in order.history if status is OrderStatus.OPEN]
        assert opened, "the order never reached OPEN"
        assert opened[-1] == order.acknowledged_at == ack_time, (
            f"acknowledged_at is {order.acknowledged_at} but the OPEN "
            f"transition is stamped {opened[-1]}"
        )

    async def test_fill_time_and_fill_transition_agree(self):
        """A fill's own timestamp and the order transition it causes."""
        harness = build_harness(clock_ms=T0)
        harness.update_market(two_venue_market(created_at=T0))
        plan = execution_plan(
            planned_order(quantity=0.5, limit_price=101.0), created_at=T0
        )
        await harness.veska.execute(plan, T0)

        fill_time = T0 + 2_000
        harness.set_clock(T0 + SKEW_MS)
        fills = await harness.veska.poll(fill_time)
        if not fills:
            raise AssertionError(
                "no fill was produced; the audit's book assumption is wrong "
                "and the timestamp comparison below is untested"
            )
        order = harness.orders_of(plan.plan_id)[0]
        assert fills[0].created_at == fill_time
        filled = [
            ts
            for ts, status in order.history
            if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        ]
        assert filled, "the fill produced no order transition"
        assert filled[-1] == fill_time, (
            f"the fill is stamped {fills[0].created_at} but the transition it "
            f"caused is stamped {filled[-1]}"
        )


class TestReplayReconstructibility:
    """The property P2-14 exists to protect."""

    async def test_two_runs_at_the_same_logical_time_agree_under_different_clocks(
        self,
    ):
        """Same logical times, different wall clocks, identical order record.

        This is replay in miniature: the original ran with the clock somewhere,
        the replay runs with it somewhere else, and both drive execution from
        the recorded instant. Anything that differs is something replay cannot
        reproduce.
        """
        def run_fields(clock_offset: int) -> dict[str, object]:
            return {"offset": clock_offset}

        async def execute_at(clock_offset: int) -> dict[str, object]:
            harness = build_harness(clock_ms=T0 + clock_offset)
            harness.update_market(two_venue_market(created_at=T0))
            plan = execution_plan(
                planned_order(client_order_id="ord-fixed"),
                created_at=T0,
                plan_id="plan-fixed",
            )
            await harness.veska.execute(plan, T0)
            await harness.veska.poll(T0 + 1_000)
            order = harness.orders_of(plan.plan_id)[0]
            return {
                "created_at": order.created_at,
                "submitted_at": order.submitted_at,
                "expires_at": order.expires_at,
                "history": list(order.history),
                "status": order.status,
                "filled_quantity": order.filled_quantity,
            }

        original = await execute_at(0)
        replayed = await execute_at(SKEW_MS)
        assert original == replayed, (
            "the same plan executed at the same logical instants produced a "
            "different order record because the wired clock differed: "
            f"{run_fields(0)} vs {run_fields(SKEW_MS)}"
        )


class TestSourceInventory:
    """A record of where execution time comes from, for the finding's evidence."""

    def test_paper_executor_module_documents_the_no_clock_rule(self):
        text = Path(executor_module.__file__).read_text()
        assert "NO CLOCK READS" in text
        assert "P2-14" in text

    def test_the_oms_source_exposes_explicit_time_compatibility(self):
        text = Path(oms_module.__file__).read_text()
        assert "now_ms: Millis | None = None" in text
