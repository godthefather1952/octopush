"""Phase 5 — H2: are strategy-exposure units consistent?

Three places name the same quantity:

* **The reservation.** ``Orchestrator.working_notional[opportunity_id] =
  decision.approved_notional`` — stored once per opportunity, and
  ``approved_notional`` is a *per-leg* notional (VESKA sizes every leg as
  ``notional / expected_price``).
* **The gate.** ``gate_strategy_exposure`` projects
  ``strategy_exposure + intent.notional * len(intent.legs)`` where
  ``strategy_exposure`` is ``sum(working_notional.values())``.
* **The dashboard.** ``Orchestrator._refresh_risk_utilization`` reports
  ``{STRATEGY: sum(working_notional.values())}``.

So the *incoming* intent is counted at ``notional x legs`` while every
*already-working* opportunity is counted at ``notional x 1``. If that is a
real mismatch, a strategy's live gross exposure can exceed its configured
maximum by a factor approaching the leg count, and the gate will not notice
because it is comparing a per-leg sum against a per-trade projection.

The tests below state the safety invariant — a strategy's true gross exposure
must never be authorised past ``max_strategy_exposure`` — rather than pinning
whatever number production currently produces.
"""

from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import RiskVerdict
from tests.audit.rune_fixtures import (
    VENUE_A,
    VENUE_B,
    VENUE_C,
    ExposureAccounting,
    blocking_names,
    context,
    core,
    gate_named,
    intent,
    leg,
    strategy_exposure_as_gate_sees_it,
    true_strategy_exposure,
)
from tests.conftest import START_MS

#: The configuration §11 describes: a 100,000 strategy budget and a 25,000
#: per-order cap, so a two-leg trade authorised at the cap consumes 50,000 of
#: real gross exposure.
LIMITS = RiskLimits(
    max_strategy_exposure=100_000.0,
    max_order_notional=25_000.0,
    max_position_notional=50_000.0,
    max_gross_exposure=1_000_000.0,
    max_venue_exposure=1_000_000.0,
    max_net_exposure=1_000_000.0,
    max_leverage=100.0,
)

PER_LEG = 25_000.0


def two_leg(oid: str, venues: tuple[str, str] = (VENUE_A, VENUE_B)):
    """A distinct two-leg opportunity, sized at the per-order cap."""
    return intent(
        opportunity_id=oid,
        correlation_id=oid,
        legs=[leg(venues[0], Side.BUY), leg(venues[1], Side.SELL)],
        notional=PER_LEG,
    )


class TestTheUnitsAsProductionUsesThem:
    """Establish the arithmetic before asserting anything about safety."""

    def test_the_intent_notional_is_per_leg(self):
        """The premise the whole finding rests on.

        ``gate_gross_exposure`` projects ``notional * len(legs)``, which is
        only correct if each leg trades ``notional``. Production's own gross
        projection is therefore the authority on the unit.
        """
        decision = core(LIMITS).evaluate(two_leg("opp-1"), context(), START_MS)
        gross = gate_named(decision, "MAX_GROSS_EXPOSURE")
        assert gross.observed == pytest.approx(PER_LEG * 2), (
            "RUNE's own gross projection treats notional as per-leg"
        )

    def test_one_two_leg_trade_consumes_twice_its_notional(self):
        accounting = ExposureAccounting(per_leg_notional=PER_LEG, leg_count=2)
        assert accounting.gross_contribution == pytest.approx(50_000.0)
        assert accounting.strategy_contribution == pytest.approx(50_000.0)

    def test_the_reservation_stores_only_the_per_leg_notional(self):
        """Read off the orchestrator statically, so the audit does not depend
        on driving a platform to observe it."""
        import inspect

        from apps.orchestrator.orchestrator import Orchestrator

        source = inspect.getsource(Orchestrator._decide)
        assert (
            "self.working_notional[opportunity.opportunity_id] = "
            "decision.approved_notional" in source
        ), "the reservation's unit changed; this audit's premise needs rechecking"

        risk_check = inspect.getsource(Orchestrator._risk_check)
        assert "strategy_exposure=sum(self.working_notional.values())" in risk_check


class TestStrategyExposureCannotBeAuthorisedPastItsLimit:
    """The safety invariant, stated directly.

    Each opportunity is authorised in turn, exactly as the orchestrator does
    it: the reservation from every previous approval is the context for the
    next one, and the portfolio stays empty because nothing has filled yet.
    """

    @staticmethod
    def _authorise_sequence(count: int):
        """Authorise ``count`` two-leg opportunities back to back.

        Returns the decisions and the working-notional map as the orchestrator
        would have built it.
        """
        engine = core(LIMITS)
        working: dict[str, float] = {}
        decisions = []
        for index in range(count):
            oid = f"opp-{index}"
            proposed = two_leg(oid)
            decision = engine.evaluate(
                proposed,
                context(strategy_exposure=sum(working.values())),
                START_MS,
            )
            decisions.append(decision)
            if decision.approved:
                working[oid] = decision.approved_notional
        return decisions, working

    def test_the_first_trade_is_authorised(self):
        decisions, working = self._authorise_sequence(1)
        assert decisions[0].verdict is RiskVerdict.APPROVED
        assert working == {"opp-0": pytest.approx(PER_LEG)}

    def test_two_working_trades_already_consume_the_whole_budget(self):
        """50,000 of real gross each; the budget is 100,000."""
        _decisions, working = self._authorise_sequence(2)
        realised = {oid: (notional, 2) for oid, notional in working.items()}
        consumed = sum(n * legs for n, legs in realised.values())
        assert consumed == pytest.approx(100_000.0)
        assert consumed == pytest.approx(LIMITS.max_strategy_exposure)

    def test_a_third_trade_must_not_be_authorised(self):
        decisions, working = self._authorise_sequence(3)
        third = decisions[2]
        realised_before = {
            oid: (notional, 2) for oid, notional in working.items() if oid != "opp-2"
        }
        projected = true_strategy_exposure(realised_before, PER_LEG, 2)
        as_gate_sees_it = strategy_exposure_as_gate_sees_it(
            {oid: n for oid, n in working.items() if oid != "opp-2"}, PER_LEG, 2
        )
        assert third.verdict is RiskVerdict.REJECTED, (
            "authorising a third two-leg trade puts the strategy's real gross "
            "exposure past its configured maximum. "
            f"requested={PER_LEG} per leg x 2 legs "
            f"approved={third.approved_notional} "
            f"reserved_true={projected - PER_LEG * 2} "
            f"projected_true={projected} "
            f"projected_as_gated={as_gate_sees_it} "
            f"limit={LIMITS.max_strategy_exposure} "
            f"blocking={blocking_names(third)}"
        )

    @pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
    def test_authorised_strategy_exposure_never_exceeds_the_limit(self, count):
        """The invariant over a whole sequence, not just at the third trade."""
        decisions, _working = self._authorise_sequence(count)
        approved = [d for d in decisions if d.approved]
        realised = sum(d.approved_notional * 2 for d in approved)
        assert realised <= LIMITS.max_strategy_exposure + 1e-6, (
            f"{len(approved)} of {count} two-leg trades were authorised, "
            f"holding {realised} of real strategy gross exposure against a "
            f"{LIMITS.max_strategy_exposure} limit"
        )


class TestTheMismatchScalesWithLegCount:
    """A generic hard-risk layer must not depend on the current strategy's
    shape. A three-leg strategy would compound the same discrepancy."""

    @staticmethod
    def _three_leg(oid: str):
        return intent(
            opportunity_id=oid,
            correlation_id=oid,
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
                leg(VENUE_C, Side.BUY),
            ],
            notional=PER_LEG,
        )

    def test_three_leg_authorisations_stay_within_the_strategy_budget(self):
        engine = core(LIMITS)
        working: dict[str, float] = {}
        approved_gross = 0.0
        for index in range(4):
            oid = f"opp3-{index}"
            decision = engine.evaluate(
                self._three_leg(oid),
                context(strategy_exposure=sum(working.values())),
                START_MS,
            )
            if decision.approved:
                working[oid] = decision.approved_notional
                approved_gross += decision.approved_notional * 3
        assert approved_gross <= LIMITS.max_strategy_exposure + 1e-6, (
            f"three-leg trades holding {approved_gross} of strategy gross "
            f"exposure against a {LIMITS.max_strategy_exposure} limit; "
            f"the gate's own view was {sum(working.values())}"
        )


class TestRiskUtilizationReportsTheSameUnitTheGateEnforces:
    """H14. The dashboard is how an operator decides whether to intervene.

    ``RuneCore.utilization`` is handed ``{STRATEGY: sum(working_notional)}``
    by the orchestrator and reports it against ``max_strategy_exposure`` — the
    limit the gate enforces in ``notional x legs`` units.
    """

    def test_the_reported_strategy_exposure_matches_real_gross(self):
        from tests.audit.rune_fixtures import portfolio

        working = {"opp-0": PER_LEG, "opp-1": PER_LEG}
        realised_gross = sum(n * 2 for n in working.values())
        utilization = core(LIMITS).utilization(
            portfolio(), 0.0, {"cross_venue": sum(working.values())}
        )
        reported = utilization.strategy_exposure["cross_venue"]
        assert reported == pytest.approx(realised_gross), (
            "the dashboard reports a strategy consuming "
            f"{reported} of a {utilization.max_strategy_exposure} budget while "
            f"the trades behind it actually hold {realised_gross}"
        )

    def test_utilization_and_the_gate_agree_on_the_same_working_set(self):
        """Whatever the unit is, both must use it. A gate that projects in one
        unit and a dashboard that reports in another cannot both be right."""
        from tests.audit.rune_fixtures import portfolio

        working = {"opp-0": PER_LEG, "opp-1": PER_LEG}
        engine = core(LIMITS)
        decision = engine.evaluate(
            two_leg("opp-2"),
            context(strategy_exposure=sum(working.values())),
            START_MS,
        )
        gate = gate_named(decision, "MAX_STRATEGY_EXPOSURE")
        utilization = engine.utilization(
            portfolio(), 0.0, {"cross_venue": sum(working.values())}
        )
        reported = utilization.strategy_exposure["cross_venue"]
        # The gate's projection is "already working" + "this trade". Removing
        # this trade's own contribution leaves what it believes is working.
        gate_believes_working = gate.observed - PER_LEG * 2
        assert reported == pytest.approx(gate_believes_working), (
            f"the gate believes {gate_believes_working} is already working "
            f"while the dashboard reports {reported}"
        )


class TestGrossExposureIsCountedConsistently:
    """A control. MAX_GROSS_EXPOSURE reads a *filled* portfolio, so both sides
    of its comparison are in realised gross notional. This is what a
    consistent gate looks like."""

    def test_the_gross_gate_compares_like_with_like(self):
        from tests.audit.rune_fixtures import portfolio_with, position

        # 50,000 already filled: two 25,000 legs.
        book = portfolio_with(
            position(VENUE_A, quantity=250.0),
            position(VENUE_B, quantity=-250.0),
        )
        assert book.gross_exposure == pytest.approx(50_000.0)
        decision = core(LIMITS).evaluate(
            two_leg("opp-x"), context(portfolio=book), START_MS
        )
        gross = gate_named(decision, "MAX_GROSS_EXPOSURE")
        assert gross.observed == pytest.approx(100_000.0), (
            "filled exposure and projected exposure are both gross notional"
        )
