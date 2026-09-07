"""H25 — a deterministic production-like execution probe.

WHAT THIS IS FOR
================
Every other module in this suite isolates one variable. This one does the
opposite: it runs a meaningful population of plans through the real execution
stack and *counts what happened*, so the audit has a description of ordinary
behaviour rather than only a list of edge cases.

WHAT IS NOT ALLOWED TO CHANGE
=============================
**No strategy default is altered to generate more trades.** The venue set, the
fee schedules, the latencies, the fill-simulator constants and the slippage
model are the shipped ones. Only the simulator's *dice* are pinned, exactly as
in ``deterministic_settings``, so the probe reports one reproducible sample
rather than a different story on every run.

The probe is a measurement, not a threshold. It asserts only invariants that
must hold whatever the sample turns out to be — conservation, accounting,
liveness, boundedness — and prints the rest through ``ProbeResult`` for the
audit document to quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models.common import Liquidity, Side, TimeInForce
from core.models.execution import OrderStatus
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    market_state,
    planned_order,
    price_levels,
    venue_state,
)

#: Enough plans that every outcome class is populated, few enough to stay fast.
PLANS = 120
#: How many polls each plan's orders are advanced through.
POLL_STEPS = 4


@dataclass
class ProbeResult:
    """Everything H25 asks the probe to record."""

    plans: int = 0
    orders: int = 0
    full_fills: int = 0
    partial_fills: int = 0
    ioc_orders: int = 0
    post_only_orders: int = 0
    cancel_requests: int = 0
    cancel_wins: int = 0
    cancel_losses: int = 0
    expired: int = 0
    unknown: int = 0
    rejected: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    requested_notional: float = 0.0
    approved_notional: float = 0.0
    planned_notional: float = 0.0
    filled_notional: float = 0.0
    fees: float = 0.0
    slippage_bps: list[float] = field(default_factory=list)
    time_to_ack: list[int] = field(default_factory=list)
    time_to_first_fill: list[int] = field(default_factory=list)
    time_to_terminal: list[int] = field(default_factory=list)
    residual_positions: int = 0

    def as_dict(self) -> dict[str, float | int]:
        def mean(values: list[float]) -> float:
            return sum(values) / len(values) if values else 0.0

        return {
            "plans": self.plans,
            "orders": self.orders,
            "full_fills": self.full_fills,
            "partial_fills": self.partial_fills,
            "ioc_orders": self.ioc_orders,
            "post_only_orders": self.post_only_orders,
            "cancel_requests": self.cancel_requests,
            "cancel_wins": self.cancel_wins,
            "cancel_losses": self.cancel_losses,
            "expired": self.expired,
            "unknown": self.unknown,
            "rejected": self.rejected,
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "requested_notional": round(self.requested_notional, 2),
            "approved_notional": round(self.approved_notional, 2),
            "planned_notional": round(self.planned_notional, 2),
            "filled_notional": round(self.filled_notional, 2),
            "fees": round(self.fees, 4),
            "mean_slippage_bps": round(mean(self.slippage_bps), 4),
            "max_slippage_bps": round(max(self.slippage_bps, default=0.0), 4),
            "mean_time_to_ack_ms": round(mean([float(v) for v in self.time_to_ack]), 1),
            "mean_time_to_first_fill_ms": round(
                mean([float(v) for v in self.time_to_first_fill]), 1
            ),
            "mean_time_to_terminal_ms": round(
                mean([float(v) for v in self.time_to_terminal]), 1
            ),
            "residual_positions": self.residual_positions,
        }


def _book(ts: int, *, drift: float = 0.0):
    """A book that walks slowly, so outcomes vary without randomness."""
    base = 100.0 * (1.0 + drift)
    return market_state(
        venue_state(
            venue=VENUE_A,
            bids=price_levels((base - 0.05, 20.0), (base - 0.25, 200.0)),
            asks=price_levels((base + 0.05, 20.0), (base + 0.25, 200.0)),
            as_of=ts,
            buy_volume=50.0,
            sell_volume=50.0,
        ),
        venue_state(
            venue=VENUE_B,
            bids=price_levels((base + 0.10, 20.0), (base - 0.10, 200.0)),
            asks=price_levels((base + 0.20, 20.0), (base + 0.40, 200.0)),
            as_of=ts,
            buy_volume=50.0,
            sell_volume=50.0,
        ),
        created_at=ts,
    )


async def run_probe() -> tuple[ProbeResult, object]:
    """Drive a population of plans through the real stack and count."""
    harness = build_harness(seed=20240607)
    latency = harness.settings.venue(VENUE_A).latency_ms
    cancel_latency = harness.settings.venue(VENUE_A).cancel_latency_ms
    result = ProbeResult()

    for index in range(PLANS):
        submitted_at = T0 + index * 1_000
        harness.update_market(_book(submitted_at, drift=index * 0.0002))

        # A deliberate mixture, cycling so every path is exercised: crossing
        # IOC entries, patient POST_ONLY entries, and two-leg trades.
        variant = index % 4
        approved = 1_000.0
        requested = 1_500.0
        if variant == 0:
            orders = [
                planned_order(
                    venue=VENUE_A,
                    side=Side.BUY,
                    quantity=approved / 100.0,
                    time_in_force=TimeInForce.IOC,
                    limit_price=100.5 * (1 + index * 0.0002),
                    expected_price=100.05 * (1 + index * 0.0002),
                    client_order_id=f"probe-{index}-a",
                )
            ]
        elif variant == 1:
            orders = [
                planned_order(
                    venue=VENUE_A,
                    side=Side.BUY,
                    quantity=approved / 100.0,
                    time_in_force=TimeInForce.POST_ONLY,
                    limit_price=99.95 * (1 + index * 0.0002),
                    expected_price=99.95 * (1 + index * 0.0002),
                    ttl_ms=latency + 2_000,
                    client_order_id=f"probe-{index}-a",
                )
            ]
        elif variant == 2:
            orders = [
                planned_order(
                    venue=VENUE_A,
                    side=Side.BUY,
                    quantity=approved / 100.0,
                    time_in_force=TimeInForce.IOC,
                    limit_price=100.5 * (1 + index * 0.0002),
                    expected_price=100.05 * (1 + index * 0.0002),
                    client_order_id=f"probe-{index}-a",
                ),
                planned_order(
                    venue=VENUE_B,
                    side=Side.SELL,
                    quantity=approved / 100.0,
                    time_in_force=TimeInForce.IOC,
                    limit_price=99.5 * (1 + index * 0.0002),
                    expected_price=100.10 * (1 + index * 0.0002),
                    client_order_id=f"probe-{index}-b",
                ),
            ]
        else:
            # A resting order the probe will try to cancel.
            orders = [
                planned_order(
                    venue=VENUE_A,
                    side=Side.BUY,
                    quantity=approved / 100.0,
                    time_in_force=TimeInForce.GTC,
                    limit_price=99.0 * (1 + index * 0.0002),
                    expected_price=99.0 * (1 + index * 0.0002),
                    ttl_ms=latency + 5_000,
                    client_order_id=f"probe-{index}-a",
                )
            ]

        plan = execution_plan(
            *orders,
            created_at=submitted_at,
            plan_id=f"probe-plan-{index}",
            notional=approved,
            approved_notional=approved,
            requested_notional=requested,
            max_slippage_bps=50.0,
            deadline_ms=submitted_at + 30_000,
        )
        await harness.veska.execute(plan, submitted_at)

        result.plans += 1
        result.requested_notional += requested * len(orders)
        result.approved_notional += approved * len(orders)
        result.planned_notional += sum(
            o.quantity * o.expected_price for o in plan.orders
        )
        result.ioc_orders += sum(
            1 for o in plan.orders if o.time_in_force is TimeInForce.IOC
        )
        result.post_only_orders += sum(
            1 for o in plan.orders if o.time_in_force is TimeInForce.POST_ONLY
        )

        # Every twelfth plan has its venue truth taken away.
        if index % 12 == 11:
            harness.executor.inject_timeout(f"probe-{index}-a")

        for step in range(1, POLL_STEPS + 1):
            at = submitted_at + step * max(latency, cancel_latency)
            harness.update_market(_book(at, drift=index * 0.0002))
            await harness.veska.poll(at)
            if variant == 3 and step == 1:
                await harness.veska.cancel(f"probe-{index}-a", at + 1)
                result.cancel_requests += 1
        harness.veska.refresh_plan(plan.plan_id, submitted_at + 30_000)

    # -- tally -------------------------------------------------------------
    for order in harness.executor.all_orders():
        result.orders += 1
        if order.status is OrderStatus.FILLED:
            result.full_fills += 1
        elif 0 < order.filled_quantity < order.quantity:
            result.partial_fills += 1
        if order.status is OrderStatus.EXPIRED:
            result.expired += 1
        if order.status is OrderStatus.UNKNOWN:
            result.unknown += 1
        if order.status is OrderStatus.REJECTED:
            result.rejected += 1
        if order.status is OrderStatus.CANCELLED:
            result.cancel_wins += 1

        if order.submitted_at is not None and order.acknowledged_at is not None:
            result.time_to_ack.append(order.acknowledged_at - order.submitted_at)
        if order.fills and order.submitted_at is not None:
            result.time_to_first_fill.append(
                order.fills[0].created_at - order.submitted_at
            )
        if order.terminal_at is not None and order.submitted_at is not None:
            result.time_to_terminal.append(order.terminal_at - order.submitted_at)

        for fill in order.fills:
            result.filled_notional += fill.notional
            result.fees += fill.fee
            result.slippage_bps.append(fill.slippage_bps)
            if fill.liquidity is Liquidity.MAKER:
                result.maker_fills += 1
            else:
                result.taker_fills += 1

    result.cancel_losses = max(0, result.cancel_requests - result.cancel_wins)
    result.residual_positions = sum(
        1 for p in harness.account.positions.values() if not p.is_flat
    )
    return result, harness


class TestProbeInvariants:
    """Only what must hold whatever the sample looks like."""

    async def test_the_probe_exercises_every_outcome_class(self):
        """A probe that produced nothing would assert nothing."""
        result, _ = await run_probe()
        summary = result.as_dict()
        assert result.plans == PLANS
        assert result.orders > 0, f"the probe produced no orders: {summary}"
        assert result.ioc_orders > 0
        assert result.post_only_orders > 0
        assert result.cancel_requests > 0
        assert result.unknown > 0, (
            f"no UNKNOWN order was produced, so the probe says nothing about "
            f"unresolved truth: {summary}"
        )

    async def test_planned_notional_never_exceeds_what_was_approved(self):
        """Conservation, aggregated over the whole population."""
        result, _ = await run_probe()
        assert result.planned_notional <= result.approved_notional * 1.01, (
            f"the probe planned {result.planned_notional:.0f} against "
            f"{result.approved_notional:.0f} approved"
        )

    async def test_filled_notional_never_exceeds_planned_notional(self):
        result, _ = await run_probe()
        assert result.filled_notional <= result.planned_notional * 1.01, (
            f"filled {result.filled_notional:.0f} against a planned "
            f"{result.planned_notional:.0f}"
        )

    async def test_every_fill_is_within_the_plans_slippage_budget(self):
        result, _ = await run_probe()
        breaches = [s for s in result.slippage_bps if s > 50.0 + 1e-6]
        assert not breaches, (
            f"{len(breaches)} fill(s) exceeded the 50 bps budget: "
            f"worst {max(breaches):.4f}"
        )

    async def test_the_account_and_the_oms_agree_about_fills(self):
        result, harness = await run_probe()
        oms_fills = sum(len(o.fills) for o in harness.executor.all_orders())
        assert harness.account.fills_applied == oms_fills, (
            f"the account applied {harness.account.fills_applied} fills and "
            f"the OMS holds {oms_fills}"
        )
        assert result.maker_fills + result.taker_fills == oms_fills

    async def test_no_order_overfilled(self):
        _, harness = await run_probe()
        for order in harness.executor.all_orders():
            assert order.filled_quantity <= order.quantity + 1e-9, (
                f"{order.client_order_id} filled {order.filled_quantity} of "
                f"{order.quantity}"
            )

    async def test_no_illegal_transition_occurred(self):
        _, harness = await run_probe()
        assert harness.oms.illegal_transitions == 0

    async def test_no_duplicate_fill_was_absorbed(self):
        _, harness = await run_probe()
        assert harness.oms.duplicate_fills == 0

    async def test_every_unknown_order_remains_outstanding(self):
        _, harness = await run_probe()
        unknown = {o.client_order_id for o in harness.executor.unknown_orders()}
        outstanding = {
            o.client_order_id for o in harness.executor.outstanding_orders()
        }
        assert unknown <= outstanding
        assert unknown & {
            o.client_order_id for o in harness.oms.terminal_orders()
        } == set()

    async def test_fees_are_non_negative_and_proportionate(self):
        result, harness = await run_probe()
        assert result.fees >= 0.0
        taker_bps = harness.settings.venue(VENUE_A).fees.fee_bps(False)
        ceiling = result.filled_notional * taker_bps / 10_000 * 1.01
        assert result.fees <= ceiling, (
            f"fees of {result.fees:.4f} exceed the taker-rate ceiling "
            f"{ceiling:.4f} for {result.filled_notional:.2f} of fills"
        )

    async def test_the_probe_is_reproducible(self):
        """The whole tally, twice."""
        first, _ = await run_probe()
        second, _ = await run_probe()
        assert first.as_dict() == second.as_dict()

    async def test_the_snapshot_agrees_with_the_probe_tally(self):
        result, harness = await run_probe()
        snapshot = harness.veska.execution_snapshot(T0 + PLANS * 1_000 + 60_000)
        assert snapshot.orders_created == result.orders
        assert len(snapshot.unknown_order_ids) == result.unknown
        assert snapshot.fills_applied == result.maker_fills + result.taker_fills


class TestProbeObservations:
    """Measurements the audit document quotes. No thresholds are asserted."""

    async def test_the_probe_records_a_full_tally(self):
        """Fails only if a field the audit reports on cannot be produced."""
        result, _ = await run_probe()
        summary = result.as_dict()
        for key in (
            "plans",
            "orders",
            "full_fills",
            "partial_fills",
            "maker_fills",
            "taker_fills",
            "mean_time_to_ack_ms",
            "mean_time_to_terminal_ms",
            "filled_notional",
            "fees",
            "max_slippage_bps",
        ):
            assert key in summary

    async def test_post_only_orders_report_their_liquidity_tier(self):
        """The tier a POST_ONLY order was billed at, over a real population."""
        result, harness = await run_probe()
        post_only_fills = [
            fill
            for order in harness.executor.all_orders()
            if order.time_in_force is TimeInForce.POST_ONLY
            for fill in order.fills
        ]
        assert all(f.liquidity is Liquidity.MAKER for f in post_only_fills), (
            "a POST_ONLY order was billed at the taker tier, which contradicts "
            "the simulator's hard-coded MAKER label"
        )
        assert result.post_only_orders > 0

    async def test_ioc_orders_that_did_not_fill_are_still_countable(self):
        """Which terminal state an unfilled IOC reaches, over the population."""
        _, harness = await run_probe()
        ioc = [
            o
            for o in harness.executor.all_orders()
            if o.time_in_force is TimeInForce.IOC and o.filled_quantity == 0.0
        ]
        states = {o.status for o in ioc}
        assert states <= {
            OrderStatus.OPEN,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
            OrderStatus.SUBMITTING,
        }, f"unfilled IOC orders reached unexpected states: {states}"
