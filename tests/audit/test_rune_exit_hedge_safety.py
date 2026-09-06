"""Phase 5 — H12: the paths that deliberately bypass RUNE's entry gates.

Exits, hedges and kill-switch flattening all skip MIN_EXPECTED_EDGE and
consensus, and that bypass is correct: refusing to close a position because
the trade is no longer attractive is how a platform ends up unable to get out.

The bypass is only safe if every action taken through it is
exposure-**reducing by construction**. This file audits that invariant. It does
not ask exits to be profitable, gated on consensus, or edge-checked.
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.models.common import Side
from tests.audit.rune_fixtures import VENUE_A, VENUE_B


class TestExitsCloseWhatIsHeld:
    """§34 — an exit must not open reverse exposure."""

    def test_exit_legs_are_sized_from_the_held_quantity(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "quantity = abs(position.quantity)" in source, (
            "sizing an exit from a notional estimate is how a 'closed' trade "
            "leaves a residual behind, or overshoots into a reverse position"
        )

    def test_exit_side_is_the_opposite_of_the_held_side(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert (
            "side=Side.SELL if position.quantity > 0 else Side.BUY" in source
        ), "an exit leg must always oppose the position it is closing"

    def test_a_flat_position_produces_no_exit_leg(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "if position is None or position.is_flat:" in source
        assert "continue" in source.split("if position is None or position.is_flat:")[1]

    def test_an_exit_with_no_legs_closes_the_record_rather_than_trading(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "if not legs or notional <= 0:" in source
        assert "StrategyState.CLOSED" in source

    def test_the_exit_reads_the_live_account_not_the_original_intent(self):
        """Closing what the original trade *asked* for rather than what it
        *got* would open reverse exposure whenever a leg partially filled."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "portfolio = self.veska.executor.account.snapshot()" in source
        assert "portfolio.positions.get(f\"{leg.venue}:{leg.symbol}\")" in source

    def test_an_exit_leg_carries_an_explicit_quantity(self):
        """VESKA sizes an entry leg from the authorised notional and an exit
        leg from its own quantity. If an exit leg arrived without one it would
        be re-sized from a notional and could overshoot."""
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.build_plan)
        assert "leg.quantity" in source
        assert "if leg.quantity is not None and leg.quantity > 0" in source

    def test_exits_are_retried_rather_than_abandoned(self):
        """An abandoned exit is residual exposure. Recorded as the mechanism
        that keeps the bypass honest."""
        source = inspect.getsource(Orchestrator._advance_exit)
        assert "residual and record.exit_attempts < self.max_exit_attempts" in source
        assert "EXIT_INCOMPLETE" in source, (
            "giving up must be recorded and handed to the standing hedge loop, "
            "not swallowed"
        )


class TestHedgesReduceTheDeltaTheyTarget:
    def test_the_hedge_side_opposes_the_unhedged_delta(self):
        from agents.okapi.agent import Okapi

        source = inspect.getsource(Okapi.build_hedges)
        assert "side = Side.SELL if report.unhedged_delta > 0 else Side.BUY" in source

    def test_a_delta_inside_tolerance_produces_no_hedge(self):
        from agents.okapi.agent import Okapi

        source = inspect.getsource(Okapi.build_hedges)
        assert "if report.within_tolerance or abs(report.unhedged_delta) <= 0:" in source

    def test_a_hedge_is_capped_by_the_per_order_notional_limit(self):
        """The bypass skips the gates, so the one bound that still applies has
        to be applied by the caller."""
        source = inspect.getsource(Orchestrator._hedge)
        assert "min(hedge.notional, self.settings.risk.max_order_notional)" in source

    def test_hedging_respects_the_execution_disable(self):
        source = inspect.getsource(Orchestrator._hedge)
        assert "if self.veska.executor.execution_disabled:" in source

    def test_a_hedge_already_in_flight_is_not_duplicated(self):
        """Two hedges for one delta would overshoot into reverse exposure."""
        source = inspect.getsource(Orchestrator._hedge)
        assert "if self._hedge_in_flight(hedge.symbol):" in source

    def test_the_hedge_venue_is_chosen_by_price_not_by_direction_reversal(self):
        from agents.okapi.agent import Okapi

        source = inspect.getsource(Okapi._hedge_venue)
        assert "min(candidates, key=lambda s: s.metrics.best_ask).venue" in source
        assert "max(candidates, key=lambda s: s.metrics.best_bid).venue" in source

    def test_a_hedge_with_no_usable_venue_is_skipped(self):
        from agents.okapi.agent import Okapi

        source = inspect.getsource(Okapi.build_hedges)
        assert "if venue is None:" in source


class TestFlattenOnlyCloses:
    def test_flatten_routes_every_visited_record_through_the_exit_path(self):
        source = inspect.getsource(Orchestrator._flatten)
        assert "StrategyState.EXITING" in source
        assert "await self._submit_exit(record, market)" in source
        assert "StrategyState.AUTHORIZED" not in source
        assert "self.veska.execute(" not in source, (
            "flatten must not build a new plan of its own; it goes through the "
            "exit path so the quantity-from-position rule applies"
        )

    def test_flatten_visits_every_state_that_can_hold_exposure(self):
        """EXECUTING joined the list in Remediation C (P5-6): a partly-filled
        entry holds a position too, and its remaining orders are exactly the
        ones a flatten needs to have cancelled out from under it."""
        source = inspect.getsource(Orchestrator._flatten)
        for state in ("EXECUTING", "MONITORING", "RECONCILING", "HEDGING"):
            assert f"StrategyState.{state}" in source

    def test_the_kill_switch_acknowledges_flatten_so_it_runs_once(self):
        """A flatten repeated every tick would keep re-submitting exits for
        positions already closing."""
        source = inspect.getsource(Orchestrator._protect)
        assert "self.kill_switch.acknowledge_flatten()" in source
        assert "self.kill_switch.acknowledge_cancel_all()" in source


class TestRouteSelectionCannotReverseIntent:
    def test_the_router_returns_the_legs_own_venue(self):
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter.route)
        assert "venue=leg.venue" in source
        assert source.count("venue=") == source.count("venue=leg.venue"), (
            "the router must not substitute a different venue; an exit routed "
            "elsewhere would open exposure rather than close it"
        )

    def test_the_router_reads_the_legs_side_and_never_assigns_one(self):
        from execution.router import VenueRouter

        source = inspect.getsource(VenueRouter)
        assert "leg.side is Side.BUY" in source
        assert "side=" not in source

    def test_the_plan_carries_the_legs_own_side(self):
        from execution.veska.engine import Veska

        source = inspect.getsource(Veska.build_plan)
        assert "side=leg.side" in source, (
            "the planned order's side must come from the leg, not be re-derived"
        )

    @pytest.mark.parametrize(
        ("side", "opposite"), [(Side.BUY, Side.SELL), (Side.SELL, Side.BUY)]
    )
    def test_side_signs_are_exact_opposites(self, side, opposite):
        assert side.sign == -opposite.sign


class TestBypassScope:
    """What the bypass does NOT skip."""

    def test_exits_are_not_gated_on_edge_or_consensus_by_design(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "gross_edge_bps=0.0" in source
        assert "expected_net_edge_bps=0.0" in source
        assert "Exits are not gated on edge or consensus" in source

    def test_an_exit_still_produces_a_recorded_risk_decision(self):
        """The bypass skips the gates, not the record: RUNE's decision is
        copied so the exit is attributable."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "decision.model_copy(" in source
        assert "EXIT_AUTHORISED" in source

    def test_an_exit_without_a_prior_decision_does_not_trade(self):
        """Fail-closed: no entry decision means no exit authorisation to copy."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "decision = record.decision" in source
        assert "if decision is None:" in source
        assert "return" in source.split("if decision is None:")[1]

    def test_an_exit_widens_its_slippage_budget_rather_than_its_size(self):
        """Getting out matters more than the last basis point — but only the
        price tolerance moves, never the quantity."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "slippage_budget = self.settings.risk.min_expected_edge_bps * 5" in source
        assert "record.exit_attempts" in source

    def test_the_exit_intent_is_marked_as_an_exit(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "is_exit=True" in source

    def test_exit_freshness_is_still_measured_from_its_own_legs(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "market.source_data_timestamp_for(" in source


class TestWorkingExposureLifecycle:
    """§35 — when does a reservation appear and disappear?"""

    def test_the_reservation_is_taken_at_authorisation(self):
        source = inspect.getsource(Orchestrator._decide)
        assert "self.working_notional[opportunity.opportunity_id] = (" in source
        assert "decision.approved_notional * len(intent.legs)" in source, (
            "gross across the legs, per the P5-2 unit fix"
        )

    def test_the_reservation_is_released_only_on_a_terminal_transition(self):
        source = inspect.getsource(Orchestrator.transition)
        assert "if target in (StrategyState.CLOSED, StrategyState.REJECTED):" in source
        assert "self.working_notional.pop(record.opportunity.opportunity_id, None)" in (
            source
        )

    def test_terminal_is_the_only_release_point(self):
        """A second release elsewhere could free a reservation while exposure
        is still live."""
        import apps.orchestrator.orchestrator as orch

        source = inspect.getsource(orch)
        pops = [
            line.strip()
            for line in source.splitlines()
            if "working_notional.pop" in line
        ]
        assert len(pops) == 1, f"more than one release point: {pops}"

    def test_a_planning_failure_releases_the_reservation(self):
        """PLANNING_FAILED rejects, and rejection is terminal, so the
        reservation cannot be stranded by a plan that could not be built."""
        source = inspect.getsource(Orchestrator._decide)
        planning = source.split("plan = self.veska.build_plan(")[1]
        assert 'self._reject(record, "PLANNING_FAILED")' in planning
        reservation_at = source.index("self.working_notional[")
        planning_at = source.index('"PLANNING_FAILED"')
        assert planning_at < reservation_at, (
            "the reservation is taken after planning succeeds, so a planning "
            "failure never leaves one behind"
        )

    def test_a_zero_fill_close_releases_the_reservation(self):
        source = inspect.getsource(Orchestrator._advance_execution)
        assert "if filled <= 0:" in source
        assert "StrategyState.CLOSED" in source

    def test_the_reservation_survives_every_non_terminal_state(self):
        """AUTHORIZED, EXECUTING, HEDGING, RECONCILING, MONITORING and EXITING
        all hold or may hold exposure, and none of them releases."""
        source = inspect.getsource(Orchestrator.transition)
        released_block = source.split(
            "if target in (StrategyState.CLOSED, StrategyState.REJECTED):"
        )[1]
        for held in ("EXECUTING", "HEDGING", "RECONCILING", "MONITORING", "EXITING"):
            assert f"StrategyState.{held}" not in released_block


class TestVenuesAreNotRetargetedOnExit:
    """The position exists where it was opened."""

    def test_the_exit_iterates_the_original_legs(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "for leg in record.opportunity.legs:" in source
        assert "OpportunityLeg(" in source
        assert "venue=leg.venue," in source
        assert "symbol=leg.symbol," in source

    def test_no_venue_selection_happens_on_the_exit_path(self):
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "_hedge_venue" not in source
        assert "detector" not in source
        for venue in (VENUE_A, VENUE_B):
            assert venue not in source
