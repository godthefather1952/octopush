"""Phase 6 — H10, H11, H12, H22: what a plan is allowed to become.

``Veska.build_plan`` is the only place an authorised intent turns into orders,
so it is where RUNE's answer either survives or is lost. Three questions:

* **H11** — can an ENTRY leg carrying an explicit ``quantity`` bypass
  ``RiskDecision.approved_notional``? The quantity override exists for exits
  and hedges, which must close *that quantity* rather than a notional estimate.
  ``OpportunityLeg.quantity``'s own docstring says "Entries leave this unset".
  Nothing checks ``intent.is_exit``.
* **H12** — for an ordinary entry, does each planned leg carry the approved
  notional, and do N legs carry N times it?
* **H10** — ``ExecutionPlan.deadline_ms`` is documented on ``TradeIntent`` as
  the "absolute deadline after which the intent must not be executed".
  ``PaperExecutor.submit`` does not read it.
* **H22** — what a malformed persisted or replayed plan can do to the executor.

Exits and hedges are *not* under test here. Their quantity override is correct
and this audit changes nothing about it; the question is only whether the same
door is open to an entry.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import OrderType, Side, TimeInForce
from core.models.opportunity import CostBreakdown, ExecutionPlan, PlannedOrder, TradeIntent
from core.models.risk import GateCheck, GateResult, RiskDecision, RiskVerdict
from execution.veska.engine import Veska
from tests.audit.veska_fixtures import (
    SYMBOL,
    VENUE_A,
    VENUE_B,
    audit_settings,
    book,
    leg,
    market,
    one_venue_market,
    plan,
    planned,
    rig,
    venue_state,
)
from tests.conftest import START_MS

SETTINGS = audit_settings()

#: Well inside every RUNE limit, so nothing below is really a risk test.
APPROVED = 1_000.0


def two_venue_market(mid: float = 40_000.0):
    return market(
        venue_state(book(VENUE_A, mid=mid)),
        venue_state(book(VENUE_B, mid=mid)),
    )


def intent(
    *legs,
    notional: float = APPROVED,
    is_exit: bool = False,
    urgency: float = 1.0,
    deadline_ms: int = START_MS + 10_000,
    max_slippage_bps: float = 10.0,
) -> TradeIntent:
    return TradeIntent(
        created_at=START_MS,
        source_data_timestamp=START_MS,
        correlation_id="opp-audit",
        opportunity_id="opp-audit",
        strategy="cross_venue",
        symbol=SYMBOL,
        legs=list(legs) or [leg(VENUE_A, Side.BUY), leg(VENUE_B, Side.SELL)],
        notional=notional,
        gross_edge_bps=12.0,
        costs=CostBreakdown(),
        expected_net_edge_bps=8.0,
        consensus_score=0.8,
        consensus_agreement=0.8,
        max_slippage_bps=max_slippage_bps,
        deadline_ms=deadline_ms,
        urgency=urgency,
        is_exit=is_exit,
    )


def decision(approved: float = APPROVED, *, intent_id: str = "int-audit") -> RiskDecision:
    return RiskDecision(
        created_at=START_MS,
        correlation_id="opp-audit",
        decision_id="dec-audit",
        intent_id=intent_id,
        strategy="cross_venue",
        symbol=SYMBOL,
        verdict=RiskVerdict.APPROVED,
        approved_notional=approved,
        requested_notional=approved,
        gates=[GateCheck(name="AUDIT", result=GateResult.PASS)],
        reason_codes=[],
    )


def veska_for(built) -> Veska:
    return Veska(
        built.bus,
        built.clock,
        built.settings,
        _health(built),
        built.executor,
    )


def _health(built):
    from core.health import HealthRegistry

    return HealthRegistry(clock=built.clock)


def build(built, proposed: TradeIntent, approved: float = APPROVED):
    engine = veska_for(built)
    return engine.build_plan(
        proposed,
        decision(approved, intent_id=proposed.intent_id),
        two_venue_market(),
        START_MS,
    )


class TestPlanConservation:
    """H12. What RUNE approved is what gets planned."""

    def test_a_one_leg_entry_carries_the_approved_notional(self):
        built = rig(settings=SETTINGS)
        built_plan = build(built, intent(leg(VENUE_A, Side.BUY)))
        assert built_plan is not None
        assert len(built_plan.orders) == 1
        order = built_plan.orders[0]
        assert order.quantity * order.expected_price == pytest.approx(APPROVED)

    def test_a_two_leg_entry_carries_it_on_each_leg(self):
        built = rig(settings=SETTINGS)
        built_plan = build(built, intent())
        assert built_plan is not None
        assert len(built_plan.orders) == 2
        for order in built_plan.orders:
            assert order.quantity * order.expected_price == pytest.approx(APPROVED)

    def test_the_gross_across_legs_is_the_approved_notional_times_the_leg_count(self):
        """The unit RUNE's gross budgets are measured in (P5-2)."""
        built = rig(settings=SETTINGS)
        built_plan = build(built, intent())
        gross = sum(o.quantity * o.expected_price for o in built_plan.orders)
        assert gross == pytest.approx(APPROVED * len(built_plan.orders))

    def test_three_legs_including_a_duplicate_venue(self):
        built = rig(settings=SETTINGS)
        built_plan = build(
            built,
            intent(
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
                leg(VENUE_A, Side.SELL),
            ),
        )
        assert built_plan is not None and len(built_plan.orders) == 3
        for order in built_plan.orders:
            assert order.quantity * order.expected_price == pytest.approx(APPROVED)

    def test_legs_at_different_prices_still_each_carry_the_notional(self):
        built = rig(settings=SETTINGS)
        engine = veska_for(built)
        skewed = market(
            venue_state(book(VENUE_A, mid=40_000.0)),
            venue_state(book(VENUE_B, mid=41_500.0)),
        )
        built_plan = engine.build_plan(
            intent(), decision(), skewed, START_MS
        )
        assert built_plan is not None
        prices = {o.expected_price for o in built_plan.orders}
        assert len(prices) == 2, "the two legs must price against their own books"
        for order in built_plan.orders:
            assert order.quantity * order.expected_price == pytest.approx(APPROVED)

    def test_a_zero_approved_notional_produces_no_plan(self):
        built = rig(settings=SETTINGS)
        assert build(built, intent(), approved=0.0) is None

    def test_an_unroutable_leg_fails_the_whole_plan(self):
        """No partial plan: an authorised leg is never silently dropped."""
        built = rig(settings=SETTINGS)
        engine = veska_for(built)
        assert (
            engine.build_plan(
                intent(), decision(), one_venue_market(), START_MS
            )
            is None
        ), "a plan was built with one of its two authorised legs missing"


class TestRiskApprovedSizeCannotBeBypassed:
    """H11. The quantity override, and who is allowed to use it."""

    def test_the_override_exists_for_exits_and_hedges(self):
        """The premise. This behaviour is correct and is not under attack."""
        source = inspect.getsource(Veska.build_plan)
        assert "leg.quantity" in source
        assert "notional / routing.expected_price" in source

    def test_an_exit_leg_closes_the_quantity_it_names(self):
        """The control: exits must keep this exact behaviour."""
        built = rig(settings=SETTINGS)
        held = 0.5
        exit_plan = build(
            built,
            intent(
                leg(VENUE_A, Side.SELL, quantity=held),
                is_exit=True,
                notional=held * 40_000.0,
            ),
            approved=held * 40_000.0,
        )
        assert exit_plan is not None
        assert exit_plan.orders[0].quantity == pytest.approx(held)

    def test_an_entry_leg_quantity_cannot_exceed_the_approved_notional(self):
        """The finding.

        An entry authorised for 1,000 of notional, whose leg carries an
        explicit quantity worth 100,000. Nothing in ``build_plan`` consults
        ``is_exit``, so the override applies to entries too — and RUNE's answer
        is discarded by the component whose job is to carry it out.
        """
        built = rig(settings=SETTINGS)
        oversized = 2.5  # ~100,000 at a 40,000 mid
        entry_plan = build(
            built,
            intent(
                leg(VENUE_A, Side.BUY, quantity=oversized),
                leg(VENUE_B, Side.SELL, quantity=oversized),
                is_exit=False,
            ),
            approved=APPROVED,
        )
        assert entry_plan is not None

        worst = max(o.quantity * o.expected_price for o in entry_plan.orders)
        assert worst <= APPROVED * 1.001, (
            f"an ENTRY intent was planned at {worst:.2f} per leg against an "
            f"approved notional of {APPROVED:.2f} — a factor of "
            f"{worst / APPROVED:.1f} — because the exit/hedge quantity "
            "override is not restricted to exits and hedges"
        )

    def test_the_planner_consults_is_exit_before_honouring_a_quantity(self):
        """Structural statement of the same gap."""
        source = inspect.getsource(Veska.build_plan)
        assert "is_exit" in source, (
            "build_plan honours leg.quantity without ever asking whether the "
            "intent is an exit; the override is documented as belonging to "
            "exits and hedges only"
        )

    def test_the_plan_notional_still_reports_the_approved_figure(self):
        """Why the bypass is quiet: the plan's own header looks right.

        ``ExecutionPlan.notional`` is set from ``approved_notional`` regardless
        of what the orders were sized to, so an oversized plan advertises the
        approved number to every downstream reader.
        """
        built = rig(settings=SETTINGS)
        entry_plan = build(
            built,
            intent(
                leg(VENUE_A, Side.BUY, quantity=2.5),
                leg(VENUE_B, Side.SELL, quantity=2.5),
            ),
        )
        assert entry_plan.notional == pytest.approx(APPROVED)
        planned_gross = sum(
            o.quantity * o.expected_price for o in entry_plan.orders
        )
        assert planned_gross == pytest.approx(
            entry_plan.notional * len(entry_plan.orders)
        ), (
            f"the plan reports notional={entry_plan.notional} while its orders "
            f"total {planned_gross}"
        )


class TestDeadlineEnforcement:
    """H10. ``deadline_ms`` is carried, but is it honoured?"""

    def test_the_plan_carries_the_intent_deadline(self):
        built = rig(settings=SETTINGS)
        built_plan = build(built, intent(deadline_ms=START_MS + 250))
        assert built_plan.deadline_ms == START_MS + 250

    def test_the_executor_never_reads_it(self):
        from execution.paper.executor import PaperExecutor

        source = inspect.getsource(PaperExecutor.submit)
        assert "deadline" in source, (
            "PaperExecutor.submit does not consult plan.deadline_ms; a stale "
            "plan can begin new risk at any time after its absolute execution "
            "deadline"
        )

    @pytest.mark.parametrize("offset", [-1, 0, 1, 60_000])
    async def test_submission_relative_to_the_deadline(self, offset):
        """A/B/C. Before, exactly at, and after the deadline.

        The invariant asserted is the minimum the brief states: a stale plan
        must not begin NEW risk after its absolute execution deadline. Before
        and at the deadline, submission is expected to succeed.
        """
        built = rig(settings=SETTINGS, market_state=one_venue_market())
        deadline = START_MS + 1_000
        stale = plan(
            planned(limit_price=1.0),
            created_at=START_MS,
            deadline_ms=deadline,
        )

        report = await built.executor.submit(stale, deadline + offset)
        order = built.only_order()
        accepted = order.status.value != "REJECTED"

        if offset <= 0:
            assert accepted, "a plan inside its deadline was refused"
        else:
            assert not accepted, (
                f"a plan whose absolute deadline was {deadline} was submitted "
                f"at {deadline + offset} ({offset}ms late) and accepted: "
                f"status={order.status.value} notes={report.notes}"
            )

    async def test_an_order_may_acknowledge_and_fill_after_the_deadline(self):
        """D/E. The deadline does not survive into the order's own lifetime.

        Submitted 1ms inside the deadline, the order acknowledges after it and
        fills from a book that appeared later still. ``ttl_ms`` is a lifetime
        measured from submission and has no relationship to the deadline the
        intent set.
        """
        built = rig(settings=SETTINGS, market_state=one_venue_market(mid=60_000.0))
        deadline = START_MS + 10
        await built.executor.submit(
            plan(
                planned(limit_price=40_100.0, ttl_ms=5_000),
                created_at=START_MS,
                deadline_ms=deadline,
            ),
            START_MS,
        )
        order = built.only_order()

        built.executor.update_market(one_venue_market(mid=30_000.0))
        fills = await built.executor.poll(deadline + 2_000)

        assert not fills, (
            f"an order filled at {deadline + 2_000}, "
            f"{2_000}ms past its plan's absolute deadline of {deadline}: "
            f"{[(f.quantity, f.price) for f in fills]}"
        )
        assert order.filled_quantity == 0.0

    def test_the_orchestrator_cancels_late_orders_but_only_for_entries(self):
        """The mitigation that exists, recorded so the finding is scoped.

        ``_advance_execution`` cancels still-live orders once ``tick_time``
        passes ``record.intent.deadline_ms``. That covers entries whose record
        is still tracked; it is not a property of the execution boundary, and
        it does not cover a plan submitted late in the first place.
        """
        import apps.orchestrator.orchestrator as orchestrator

        source = inspect.getsource(orchestrator.Orchestrator._advance_execution)
        assert "deadline" in source and "self.veska.cancel(" in source


class TestPlanNumericSafety:
    """H22. A malformed persisted or replayed plan must fail safely."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("quantity", 0.0),
            ("quantity", -1.0),
            ("quantity", float("nan")),
            ("quantity", float("inf")),
            ("expected_price", 0.0),
            ("expected_price", -1.0),
            ("expected_price", float("nan")),
            ("expected_price", float("inf")),
            ("ttl_ms", 0),
            ("ttl_ms", -1),
        ],
    )
    def test_a_planned_order_refuses_meaningless_numbers(self, field, value):
        import pydantic

        fields = {
            "venue": VENUE_A,
            "symbol": SYMBOL,
            "side": Side.BUY,
            "quantity": 0.10,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.IOC,
            "expected_price": 40_000.0,
            "expected_fee_bps": 5.0,
            field: value,
        }
        with pytest.raises(pydantic.ValidationError):
            PlannedOrder(**fields)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("notional", 0.0),
            ("notional", -1.0),
            ("notional", float("nan")),
            ("notional", float("inf")),
            ("max_slippage_bps", -1.0),
            ("max_slippage_bps", float("nan")),
            ("max_slippage_bps", float("inf")),
        ],
    )
    def test_an_execution_plan_refuses_meaningless_numbers(self, field, value):
        import pydantic

        fields = {
            "created_at": START_MS,
            "intent_id": "int-audit",
            "strategy": "cross_venue",
            "symbol": SYMBOL,
            "orders": [planned()],
            "deadline_ms": START_MS + 1_000,
            "max_slippage_bps": 10.0,
            "notional": 1_000.0,
            field: value,
        }
        with pytest.raises(pydantic.ValidationError):
            ExecutionPlan(**fields)

    def test_a_limit_price_may_not_be_non_finite(self):
        """``limit_price`` is optional, which is not the same as unconstrained:
        an infinite BUY limit makes every level marketable."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            planned(limit_price=float("inf"))

    def test_a_paper_order_refuses_a_non_finite_price_too(self):
        """The executor's own schema, since a PaperOrder can be rebuilt from a
        persisted payload without ever passing through ``build_plan``."""
        import pydantic

        from core.models.execution import PaperOrder

        for field in ("expected_price", "limit_price"):
            with pytest.raises(pydantic.ValidationError):
                PaperOrder(
                    created_at=START_MS,
                    venue=VENUE_A,
                    symbol=SYMBOL,
                    side=Side.BUY,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.IOC,
                    quantity=0.10,
                    **{"expected_price": 40_000.0, field: float("inf")},
                )
