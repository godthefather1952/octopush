"""H11, H12, H23, H32 — planning must not size beyond what RUNE authorised.

H11: THE SIZING BRANCH
======================
Batch A makes execution role part of sizing. ENTRY is risk-increasing and is
always derived from ``approved_notional / expected_price``, so an explicit leg
quantity cannot enlarge RUNE's grant. EXIT and HEDGE remain allowed to carry
the exact quantity needed to neutralise exposure that already exists.

H12: CONSERVATION
=================
For an entry, per-leg planned notional should be approximately
``approved_notional``, and the gross across N legs approximately
``approved_notional * N`` — which is the meaning every existing reader already
gives ``ExecutionPlan.notional`` (P5-2).

H32: PREFLIGHT
==============
``preflight_plan`` checks structure and capability claims. It does **not**
check deadlines, sizes, notional conservation or venue reachability, and
``Veska.execute`` does not consult it at all. Both facts are asserted, because
a check nobody calls is worth exactly as much as its callers.
"""

from __future__ import annotations

import inspect

from core.models.common import OrderType, Side, TimeInForce
from core.models.execution import ExecutionRole, OrderStatus
from execution.paper.executor import PAPER_CAPABILITIES
from execution.veska.preflight import preflight_plan
from tests.audit.veska_fixtures import (
    T0,
    VENUE_A,
    VENUE_B,
    build_harness,
    execution_plan,
    leg,
    market_state,
    planned_order,
    price_levels,
    risk_decision,
    trade_intent,
    two_venue_market,
    venue_state,
)

#: Well below the crossing threshold in ``VenueRouter.route``, so planning
#: takes the aggressive branch and ``expected_price`` is the touch.
CROSSING_URGENCY = 0.9


class TestEntrySizingIsBoundedByAuthorisation:
    """H11 — the hostile entry."""

    async def test_an_entry_leg_without_a_quantity_is_sized_from_approval(self):
        """The shipped shape, pinned so the hostile case has a baseline."""
        harness = build_harness()
        market = two_venue_market()
        intent = trade_intent(
            leg(venue=VENUE_A, side=Side.BUY),
            urgency=CROSSING_URGENCY,
        )
        decision = risk_decision(
            intent_id=intent.intent_id, approved_notional=1_000.0
        )

        plan = harness.veska.build_plan(intent, decision, market, T0)

        assert plan is not None
        order = plan.orders[0]
        planned_notional = order.quantity * order.expected_price
        assert abs(planned_notional - 1_000.0) < 1e-6

    async def test_an_entry_leg_with_an_explicit_quantity_cannot_exceed_approval(
        self,
    ):
        """The invariant: risk-increasing activity is bounded by authorisation.

        A hostile or buggy entry intent carries a quantity representing far
        more than RUNE allowed. Nothing about ``execution_role`` is consulted,
        so the explicit quantity wins outright.
        """
        harness = build_harness()
        market = two_venue_market()
        approved = 1_000.0
        # 1,000 units at ~100 is ~100,000 notional: a hundred times the grant.
        hostile_quantity = 1_000.0
        intent = trade_intent(
            leg(venue=VENUE_A, side=Side.BUY, quantity=hostile_quantity),
            urgency=CROSSING_URGENCY,
        )
        assert intent.execution_role is ExecutionRole.ENTRY
        decision = risk_decision(
            intent_id=intent.intent_id, approved_notional=approved
        )

        plan = harness.veska.build_plan(intent, decision, market, T0)

        assert plan is not None
        order = plan.orders[0]
        planned_notional = order.quantity * order.expected_price
        assert planned_notional <= approved * 1.01, (
            f"an ENTRY plan was built for {planned_notional:.0f} notional "
            f"against an approved_notional of {approved:.0f} — "
            f"{planned_notional / approved:.0f}x the authorisation — ENTRY "
            "sizing is no longer bounded by the approved-notional rule"
        )

    def test_the_sizing_branch_explicitly_protects_entry_authorisation(self):
        """ENTRY uses approved notional; exact quantities remain risk-reducing only."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.build_plan)
        assert "intent.execution_role is ExecutionRole.ENTRY" in source
        assert "quantity = notional / routing.expected_price" in source
        assert "leg.quantity" in source

    def test_the_shipped_detector_leaves_entry_quantities_unset(self):
        """Reachability evidence, which bounds the severity honestly."""
        from strategies.cross_venue import detector as detector_module

        source = inspect.getsource(detector_module)
        leg_blocks = source.count("OpportunityLeg(")
        assert leg_blocks >= 2
        assert "quantity=" not in source.split("OpportunityLeg(")[1].split(")")[0]

    def test_exit_and_hedge_legs_legitimately_carry_a_quantity(self):
        """Recorded so remediation does not remove the branch wholesale."""
        from apps.orchestrator import orchestrator as orch_module

        exit_source = inspect.getsource(orch_module.Orchestrator._submit_exit)
        assert "quantity=quantity" in exit_source
        hedge_source = inspect.getsource(orch_module.Orchestrator._hedge)
        assert "quantity=quantity" in hedge_source


class TestPlanConservation:
    """H12 — planned notional against authorisation, across leg counts."""

    async def _plan_for(self, legs, approved: float, market=None):
        harness = build_harness()
        market = market or two_venue_market()
        intent = trade_intent(*legs, urgency=CROSSING_URGENCY)
        decision = risk_decision(
            intent_id=intent.intent_id, approved_notional=approved
        )
        return harness.veska.build_plan(intent, decision, market, T0)

    async def test_one_leg_plans_the_approved_notional(self):
        plan = await self._plan_for([leg(venue=VENUE_A, side=Side.BUY)], 1_000.0)
        assert plan is not None
        assert len(plan.orders) == 1
        order = plan.orders[0]
        assert abs(order.quantity * order.expected_price - 1_000.0) < 1e-6

    async def test_two_legs_each_plan_the_approved_notional(self):
        """Per-leg is the canonical meaning; gross is approved x leg count."""
        plan = await self._plan_for(
            [
                leg(venue=VENUE_A, side=Side.BUY),
                leg(venue=VENUE_B, side=Side.SELL),
            ],
            1_000.0,
        )
        assert plan is not None
        assert len(plan.orders) == 2
        for order in plan.orders:
            per_leg = order.quantity * order.expected_price
            assert abs(per_leg - 1_000.0) < 1e-6, (
                f"leg on {order.venue} planned {per_leg:.2f} against an "
                "approved per-leg notional of 1000.00"
            )
        gross = sum(o.quantity * o.expected_price for o in plan.orders)
        assert abs(gross - 2_000.0) < 1e-6

    async def test_three_legs_including_a_duplicate_venue(self):
        """Two legs on one venue must not collapse or double-size."""
        plan = await self._plan_for(
            [
                leg(venue=VENUE_A, side=Side.BUY),
                leg(venue=VENUE_A, side=Side.BUY),
                leg(venue=VENUE_B, side=Side.SELL),
            ],
            500.0,
        )
        assert plan is not None
        assert len(plan.orders) == 3
        gross = sum(o.quantity * o.expected_price for o in plan.orders)
        assert abs(gross - 1_500.0) < 1e-6

    async def test_very_different_prices_do_not_change_the_notional(self):
        """Conservation is about notional, not quantity."""
        market = market_state(
            venue_state(
                venue=VENUE_A,
                bids=price_levels((10.0, 1_000.0)),
                asks=price_levels((10.01, 1_000.0)),
            ),
            venue_state(
                venue=VENUE_B,
                bids=price_levels((50_000.0, 10.0)),
                asks=price_levels((50_010.0, 10.0)),
            ),
        )
        plan = await self._plan_for(
            [
                leg(venue=VENUE_A, side=Side.BUY, reference_price=10.0),
                leg(venue=VENUE_B, side=Side.SELL, reference_price=50_000.0),
            ],
            1_000.0,
            market=market,
        )
        assert plan is not None
        for order in plan.orders:
            per_leg = order.quantity * order.expected_price
            assert abs(per_leg - 1_000.0) < 1e-6, (
                f"{order.venue} at {order.expected_price} planned {per_leg:.2f}"
            )

    async def test_a_zero_approval_produces_no_plan(self):
        plan = await self._plan_for([leg(venue=VENUE_A, side=Side.BUY)], 0.0)
        assert plan is None

    async def test_the_plan_carries_both_notionals_it_was_built_from(self):
        harness = build_harness()
        intent = trade_intent(
            leg(venue=VENUE_A, side=Side.BUY),
            notional=5_000.0,
            urgency=CROSSING_URGENCY,
        )
        decision = risk_decision(
            intent_id=intent.intent_id,
            approved_notional=1_000.0,
            requested_notional=5_000.0,
        )
        plan = harness.veska.build_plan(intent, decision, two_venue_market(), T0)
        assert plan is not None
        assert plan.approved_notional == 1_000.0
        assert plan.requested_notional == 5_000.0
        assert plan.notional == 1_000.0


class TestUnknownVenue:
    """H23 — a plan naming a venue the configuration does not have."""

    def test_the_latency_lookup_has_a_silent_fallback(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor._latency)
        assert "except KeyError" in source
        assert "return 40" in source

    async def test_the_router_cannot_produce_an_unknown_venue_plan(self):
        """The primary path is structurally blocked, which bounds severity."""
        harness = build_harness()
        market = two_venue_market()
        intent = trade_intent(
            leg(venue="VENUE_NOWHERE", side=Side.BUY), urgency=CROSSING_URGENCY
        )
        decision = risk_decision(intent_id=intent.intent_id)

        plan = harness.veska.build_plan(intent, decision, market, T0)

        assert plan is None, (
            "the router built a plan for a venue with no market state; the "
            "unknown-venue path is reachable through planning after all"
        )

    async def test_a_hand_built_unknown_venue_plan_does_not_execute_silently(
        self,
    ):
        """``execute`` accepts any plan, including a replayed or malformed one.

        Whatever happens, it must not be a quiet execution against a
        made-up 40ms latency. Either the submission is refused, or the failure
        is loud.
        """
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(venue="VENUE_NOWHERE", limit_price=101.0),
            created_at=T0,
            plan_id="plan-nowhere",
        )

        report = await harness.veska.execute(plan, T0)
        order = harness.orders_of(plan.plan_id)[0]

        refused = order.status is OrderStatus.REJECTED or bool(report.notes)
        assert refused, (
            "a plan naming an unconfigured venue was accepted and given a "
            f"default 40ms latency: the order is {order.status.value} with "
            f"ack at {harness.executor._pending[order.client_order_id].ack_at}"
        )


class TestPreflightContract:
    """H32 — what preflight does, and what nothing does with it."""

    def test_it_flags_an_empty_plan(self):
        plan = execution_plan(created_at=T0)
        result = preflight_plan(plan)
        assert result.blocked
        assert "EMPTY_PLAN" in result.reason_codes

    def test_it_flags_a_missing_venue_and_symbol(self):
        plan = execution_plan(created_at=T0)
        broken = plan.model_copy(
            update={
                "orders": [
                    planned_order(venue="", symbol="", client_order_id="x")
                ]
            }
        )
        result = preflight_plan(broken)
        assert "MISSING_VENUE" in result.reason_codes
        assert "MISSING_SYMBOL" in result.reason_codes

    def test_it_flags_an_unsupported_order_type(self):
        plan = execution_plan(
            planned_order(order_type=OrderType.MARKET, limit_price=None),
            created_at=T0,
        )
        result = preflight_plan(plan, capabilities=PAPER_CAPABILITIES)
        assert "UNSUPPORTED_ORDER_TYPE" in result.reason_codes

    def test_it_flags_an_unsupported_time_in_force(self):
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.FOK), created_at=T0
        )
        result = preflight_plan(plan, capabilities=PAPER_CAPABILITIES)
        assert "UNSUPPORTED_TIME_IN_FORCE" in result.reason_codes

    def test_a_well_formed_plan_passes(self):
        plan = execution_plan(planned_order(), created_at=T0)
        result = preflight_plan(plan, capabilities=PAPER_CAPABILITIES)
        assert result.ok
        assert result.reason_codes == ()

    def test_it_does_not_check_the_deadline(self):
        """Named absences, so the contract is unambiguous."""
        plan = execution_plan(
            planned_order(), created_at=T0, deadline_ms=T0 - 100_000
        )
        assert preflight_plan(plan, capabilities=PAPER_CAPABILITIES).ok

    def test_it_does_not_check_notional_conservation(self):
        plan = execution_plan(
            planned_order(quantity=1_000.0, expected_price=100.0),
            created_at=T0,
            notional=1.0,
            approved_notional=1.0,
        )
        assert preflight_plan(plan, capabilities=PAPER_CAPABILITIES).ok

    def test_it_does_not_check_venue_reachability(self):
        plan = execution_plan(
            planned_order(venue="VENUE_NOWHERE"), created_at=T0
        )
        assert preflight_plan(plan, capabilities=PAPER_CAPABILITIES).ok

    def test_execute_does_not_consult_preflight(self):
        """The claim Phase 6 makes about itself, verified rather than trusted."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.execute)
        assert "preflight" not in source, (
            "execute now consults preflight; the plan-submission gate has "
            "changed and every finding downstream of it needs re-deriving"
        )

    async def test_a_plan_preflight_rejects_is_still_submitted(self):
        """The behavioural consequence of the seam being unwired."""
        harness = build_harness()
        harness.update_market(two_venue_market())
        plan = execution_plan(
            planned_order(time_in_force=TimeInForce.FOK, limit_price=101.0),
            created_at=T0,
        )
        assert preflight_plan(plan, capabilities=PAPER_CAPABILITIES).blocked

        report = await harness.veska.execute(plan, T0)

        assert report.orders, "the audit's premise is wrong: nothing was submitted"
        assert report.orders[0].status is not OrderStatus.REJECTED, (
            "the plan was rejected after all; preflight may now be wired in"
        )
