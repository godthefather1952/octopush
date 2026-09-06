"""Phase 5 — H2: are strategy-exposure units consistent?

Three places name the same quantity, and they must all name it in the same
unit:

* **The reservation.** ``Orchestrator.working_notional[opportunity_id]`` — one
  entry per working opportunity.
* **The gate.** ``gate_strategy_exposure`` projects
  ``strategy_exposure + intent.notional * len(intent.legs)``.
* **The dashboard.** ``Orchestrator._refresh_risk_utilization`` reports the
  same quantity against the same limit.

The audit found them disagreeing. The reservation and the dashboard stored
``approved_notional`` — a *per-leg* notional, since VESKA sizes every leg as
``notional / expected_price`` — while the gate projected the incoming intent at
``notional x legs``. So the incoming trade was counted at ``notional x legs``
and every already-working one at ``notional x 1``, and a strategy's live gross
exposure could be authorised past its configured maximum by a factor
approaching the leg count (P5-2), with the dashboard understating it by the
same factor (P5-13).

The P5-2 remediation makes the reservation gross across all legs, and routes
both readers through ``Orchestrator._current_strategy_exposure()`` so they
cannot drift apart again. The tests below are unchanged in what they assert:
they state the safety invariant — a strategy's true gross exposure must never
be authorised past ``max_strategy_exposure`` — rather than pinning whatever
number production happens to produce. What changed is the harness's model of
the reservation, which has to match the production it is modelling.
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
    # Opened with the rest: MAX_UNHEDGED_EXPOSURE became
    # size-sensitive in Remediation D (P5-18), and this
    # fixture isolates a different limit.
    max_unhedged_notional=1_000_000.0,
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

    def test_the_reservation_stores_gross_exposure_across_all_legs(self):
        """Read off the orchestrator statically, so the audit does not depend
        on driving a platform to observe it.

        The reservation must be multiplied by the leg count. Storing the bare
        ``approved_notional`` is the P5-2 defect, and it is invisible in any
        single-trade scenario — it only shows once a second trade is judged
        against the first.
        """
        import inspect

        from apps.orchestrator.orchestrator import Orchestrator

        source = inspect.getsource(Orchestrator._decide)
        assert "self.working_notional[opportunity.opportunity_id] = (" in source
        assert "decision.approved_notional * len(intent.legs)" in source, (
            "the reservation must be gross across the opportunity's legs, or "
            "the gate compares a per-leg sum against a per-trade projection"
        )

    def test_both_readers_go_through_one_helper(self):
        """P5-13. A gate and a dashboard computing the same quantity from two
        separate expressions is how they came to disagree."""
        import inspect

        from apps.orchestrator.orchestrator import Orchestrator

        risk_check = inspect.getsource(Orchestrator._risk_check)
        refresh = inspect.getsource(Orchestrator._refresh_risk_utilization)
        assert "strategy_exposure=self._current_strategy_exposure()" in risk_check
        assert "self._current_strategy_exposure()" in refresh
        for source in (risk_check, refresh):
            assert "sum(self.working_notional.values())" not in source


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
                # Gross across the opportunity's legs, exactly as
                # ``Orchestrator._decide`` now stores it.
                working[oid] = decision.approved_notional * len(proposed.legs)
        return decisions, working

    def test_the_first_trade_is_authorised(self):
        decisions, working = self._authorise_sequence(1)
        assert decisions[0].verdict is RiskVerdict.APPROVED
        assert decisions[0].approved_notional == pytest.approx(PER_LEG)
        assert working == {"opp-0": pytest.approx(PER_LEG * 2)}

    def test_two_working_trades_already_consume_the_whole_budget(self):
        """50,000 of real gross each; the budget is 100,000."""
        _decisions, working = self._authorise_sequence(2)
        consumed = sum(working.values())
        assert consumed == pytest.approx(100_000.0)
        assert consumed == pytest.approx(LIMITS.max_strategy_exposure)

    def test_a_third_trade_must_not_be_authorised(self):
        decisions, working = self._authorise_sequence(3)
        third = decisions[2]
        before = {oid: gross for oid, gross in working.items() if oid != "opp-2"}
        realised_before = {oid: (gross / 2, 2) for oid, gross in before.items()}
        projected = true_strategy_exposure(realised_before, PER_LEG, 2)
        as_gate_sees_it = strategy_exposure_as_gate_sees_it(before, PER_LEG, 2)
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
                working[oid] = decision.approved_notional * 3
                approved_gross += decision.approved_notional * 3
        assert approved_gross <= LIMITS.max_strategy_exposure + 1e-6, (
            f"three-leg trades holding {approved_gross} of strategy gross "
            f"exposure against a {LIMITS.max_strategy_exposure} limit; "
            f"the gate's own view was {sum(working.values())}"
        )


class TestRiskUtilizationReportsTheSameUnitTheGateEnforces:
    """H14. The dashboard is how an operator decides whether to intervene.

    ``RuneCore.utilization`` is handed ``{STRATEGY:
    _current_strategy_exposure()}`` by the orchestrator and reports it against
    ``max_strategy_exposure`` — the limit the gate enforces in ``notional x
    legs`` units. Both readings must therefore be gross across all legs.

    ``WORKING`` below is the reservation map as ``Orchestrator._decide`` now
    builds it: two two-leg opportunities authorised at 25,000 per leg, each
    stored as its 50,000 gross contribution.
    """

    WORKING = {"opp-0": PER_LEG * 2, "opp-1": PER_LEG * 2}
    #: What those two trades actually hold: 25,000 on each of four legs.
    REALISED_GROSS = PER_LEG * 4

    def test_the_reported_strategy_exposure_matches_real_gross(self):
        from tests.audit.rune_fixtures import portfolio

        utilization = core(LIMITS).utilization(
            portfolio(), 0.0, {"cross_venue": sum(self.WORKING.values())}
        )
        reported = utilization.strategy_exposure["cross_venue"]
        assert reported == pytest.approx(self.REALISED_GROSS), (
            "the dashboard reports a strategy consuming "
            f"{reported} of a {utilization.max_strategy_exposure} budget while "
            f"the trades behind it actually hold {self.REALISED_GROSS}"
        )

    def test_the_dashboard_shows_the_budget_fully_consumed(self):
        """The operator-visible consequence: two trades fill a 100,000 budget,
        and the dashboard must say so rather than showing it half used."""
        from tests.audit.rune_fixtures import portfolio

        utilization = core(LIMITS).utilization(
            portfolio(), 0.0, {"cross_venue": sum(self.WORKING.values())}
        )
        assert utilization.strategy_exposure["cross_venue"] == pytest.approx(
            utilization.max_strategy_exposure
        )

    def test_utilization_and_the_gate_agree_on_the_same_working_set(self):
        """Whatever the unit is, both must use it. A gate that projects in one
        unit and a dashboard that reports in another cannot both be right.

        One working trade rather than two, so the incoming one still has
        headroom and the full gate list runs — with the budget already
        exhausted the decision short-circuits at MIN_TRADE_NOTIONAL and there
        is no MAX_STRATEGY_EXPOSURE check to compare against.
        """
        from tests.audit.rune_fixtures import portfolio

        working = {"opp-0": PER_LEG * 2}
        engine = core(LIMITS)
        decision = engine.evaluate(
            two_leg("opp-1"),
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
        gate_believes_working = gate.observed - decision.approved_notional * 2
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
