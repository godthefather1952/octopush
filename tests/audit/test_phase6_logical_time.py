"""H1 — one economic action must not carry two timelines.

THE CLAIM UNDER TEST
====================
``PaperExecutor``'s module docstring states, at length, that it never reads a
clock: every time-dependent decision uses the logical instant its caller
supplied, because a recorded session preserves one timestamp per tick and a
clock read is not that instant (P2-14).

That claim is about the *executor*. The ``OrderManager`` it writes through
makes no such claim and reads ``self.clock.now_ms()`` on every ``create``,
``transition``, ``mark_unknown``, ``resolve_unknown``, ``reject`` and
``apply_fill``.

So one submission at logical instant T produces:

* ``submitted_at`` = T                        (executor, explicit)
* ``_pending.ack_at`` = T + venue latency     (executor, explicit)
* ``created_at`` = clock                      (OMS, read)
* ``expires_at`` = clock + ttl_ms             (OMS, read)
* every ``history`` entry = clock             (OMS, read)

In production the orchestrator passes ``tick_time`` and the clock is usually
close to it, so the divergence is small and invisible. Under a live feed it is
not: the clock advances while the tick runs, which is the exact scenario
P2-14 was raised for.

WHY THIS MATTERS ECONOMICALLY
=============================
``expires_at`` is the one that costs money. It is computed from a clock read
and then compared against ``now_ms`` in ``poll``. Two different clocks decide
one deadline, so an order's real lifetime is ``ttl_ms + (clock - T)`` — longer
or shorter than the TTL that was asked for, by however far the two have
drifted. Replay, which drives ``now_ms`` from the recorded tick marker but
constructs the OMS clock separately, cannot reconstruct it.
"""

from __future__ import annotations

import ast
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


class TestExecutorReadsNoClock:
    """The claim the executor's docstring actually makes."""

    def test_the_executor_source_contains_no_clock_read(self):
        """``PaperExecutor`` must not call ``now_ms()`` anywhere.

        Asserted over the source rather than by observing timestamps, because
        a timestamp that happens to match proves nothing about where it came
        from.
        """
        source = inspect.getsource(executor_module.PaperExecutor)
        assert "now_ms()" not in source
        assert "self.clock" not in source.replace("clock: Clock", "")

    def test_the_order_manager_does_read_a_clock(self):
        """Stated as a fact, not a complaint — it is what makes H1 reachable.

        If this ever stops being true the divergence below closes on its own,
        and this test is what will say so.
        """
        source = inspect.getsource(oms_module.OrderManager)
        assert "self.clock.now_ms()" in source

    def test_every_order_manager_write_path_reads_the_clock(self):
        """Enumerate the read sites, so a partial fix is visible as a partial fix."""
        tree = ast.parse(inspect.getsource(oms_module.OrderManager))
        reading: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.dump(node)
            if "now_ms" in body and "clock" in body:
                reading.add(node.name)
        assert reading == {
            "create",
            "transition",
            "mark_unknown",
            "resolve_unknown",
            "reject",
            "apply_fill",
        }


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

    def test_the_oms_module_makes_no_such_claim(self):
        """Stated so the asymmetry is on the record rather than inferred."""
        text = Path(oms_module.__file__).read_text()
        assert "NO CLOCK READS" not in text
