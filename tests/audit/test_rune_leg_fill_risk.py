"""Phase 5 Remediation D — pre-trade worst-case unhedged leg risk (P5-18).

An intent's FINAL delta and its worst INTERMEDIATE delta are different numbers.
A cross-venue BUY/SELL pair is delta-neutral once both legs have filled; between
the first fill and the second the book holds one whole leg of one-sided
exposure. MAX_UNHEDGED_EXPOSURE was written against the first number and
enforced against a limit that governs the second.

The consequence was not hypothetical. With the shipped defaults —
``max_order_notional`` 25,000, ``max_unhedged_notional`` 10,000 — RUNE could
authorise 25,000 per leg on the ordinary two-leg strategy, and the post-fill
backstop added in Remediation C then engaged during normal operation, correctly
and predictably, at roughly START_MS + 15.2s.

The backstop is right. The pre-trade projection was the incomplete half. These
tests pin the projection, the sizing it now drives, and the boundary between
them.

Nothing here relaxes the hard limit or the emergency response; RUNE simply
sizes the entry to fit the budget the platform is configured to hold.
"""

from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import CommittedExposure, RiskVerdict
from risk import limits as gates
from tests.audit.rune_fixtures import (
    SYMBOL,
    VENUE_A,
    VENUE_B,
    VENUE_C,
    context,
    core,
    gate_named,
    intent,
    leg,
)
from tests.conftest import START_MS

ETH = "ETH-USD"

#: The shipped numbers, restated so the scenarios below read as the platform's
#: own configuration rather than as invented ones.
SHIPPED = RiskLimits()
UNHEDGED = SHIPPED.max_unhedged_notional      # 10,000
ORDER = SHIPPED.max_order_notional            # 25,000

#: Every other size-sensitive limit wide open, so only the unhedged budget can
#: bind. ``max_order_notional`` and ``max_position_notional`` move together
#: because RiskLimits refuses a config where one permitted order would breach
#: the position limit.
ONLY_UNHEDGED = RiskLimits(
    max_unhedged_notional=UNHEDGED,
    max_order_notional=ORDER,
    max_position_notional=1_000_000.0,
    max_gross_exposure=10_000_000.0,
    max_net_exposure=10_000_000.0,
    max_venue_exposure=1_000_000.0,
    max_strategy_exposure=10_000_000.0,
    max_leverage=1_000_000.0,
)
OPEN_CTX = {"max_economical_notional": 10_000_000.0}


def decide(proposed, **ctx_kwargs):
    return core(ONLY_UNHEDGED).evaluate(
        proposed, context(**{**OPEN_CTX, **ctx_kwargs}), START_MS
    )


# ======================================================================
# §23 D-F — the worst-case fill factor
# ======================================================================


class TestTheFillFactor:
    """How many per-leg notionals of residual an intent can transiently hold.

    Grouped by symbol, ``max(buy_legs, sell_legs)`` per symbol, summed across
    symbols — the same aggregation ``Okapi.total_unhedged`` performs, so the
    bound is expressed in the units the limit is measured in.
    """

    def test_a_single_leg_is_one(self):
        assert gates.unhedged_fill_factor(intent(legs=[leg(VENUE_A, Side.BUY)])) == 1

    def test_a_balanced_pair_on_one_symbol_is_one(self):
        """The case the old classification got wrong. Its final delta is zero;
        its worst intermediate delta is one whole leg."""
        assert gates.unhedged_fill_factor(intent()) == 1

    def test_two_buys_and_one_sell_on_one_symbol_is_two(self):
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.BUY),
                leg(VENUE_C, Side.SELL),
            ]
        )
        assert gates.unhedged_fill_factor(proposed) == 2

    def test_two_symbols_each_one_sided_is_two(self):
        proposed = intent(
            legs=[leg(VENUE_A, Side.BUY), leg(VENUE_B, Side.SELL, symbol=ETH)]
        )
        assert gates.unhedged_fill_factor(proposed) == 2

    def test_two_balanced_pairs_on_two_symbols_is_two(self):
        """Each symbol contributes its own worst side, and both can be
        one-sided at the same moment."""
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
                leg(VENUE_A, Side.BUY, symbol=ETH),
                leg(VENUE_B, Side.SELL, symbol=ETH),
            ]
        )
        assert gates.unhedged_fill_factor(proposed) == 2

    def test_the_factor_does_not_depend_on_the_notional(self):
        """It is a leg-shape property. Size enters the projection by
        multiplication, not by changing the factor."""
        shape = [leg(VENUE_A, Side.BUY), leg(VENUE_B, Side.SELL, symbol=ETH)]
        assert gates.unhedged_fill_factor(intent(legs=shape, notional=1.0)) == (
            gates.unhedged_fill_factor(intent(legs=shape, notional=1_000_000.0))
        )


class TestTheExecutionMultiplier:
    """VESKA sizes from the expected price; a marketable order may fill worse,
    up to the slippage budget the intent already carries."""

    @pytest.mark.parametrize(
        ("bps", "expected"), [(0.0, 1.0), (10.0, 1.001), (50.0, 1.005)]
    )
    def test_it_is_the_intents_own_slippage_budget(self, bps, expected):
        assert gates.execution_multiplier(
            intent(max_slippage_bps=bps)
        ) == pytest.approx(expected)

    def test_a_negative_budget_never_shrinks_the_projection(self):
        """Clamped at 1.0: a slippage allowance cannot be used to claim a fill
        will land better than expected."""
        assert gates.execution_multiplier(intent(max_slippage_bps=-100.0)) == 1.0


# ======================================================================
# §23 A-C — the gate, the boundary, and the sizing it drives
# ======================================================================


class TestTheDefaultStrategyIsSizedToFit:
    """§18. The headline behaviour change."""

    def test_a_25k_two_leg_request_is_reduced_not_authorised(self):
        """§23-A. The exact configuration that tripped the backstop in CI."""
        decision = decide(intent(notional=ORDER))
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional < ORDER
        assert decision.approved_notional == pytest.approx(
            UNHEDGED / gates.execution_multiplier(intent())
        )
        assert "SIZE_REDUCED_BY_HEADROOM" in decision.reason_codes

    def test_the_sized_trade_lands_exactly_on_the_limit(self):
        """§23-B. Sized so the projection is at the boundary, not under it:
        the platform gives up no capacity it is entitled to."""
        decision = decide(intent(notional=ORDER))
        check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
        assert check.observed == pytest.approx(UNHEDGED)
        assert check.observed <= check.limit + 1e-9
        assert not check.blocking

    def test_a_request_already_inside_the_budget_is_untouched(self):
        """The gate must not become blanket-conservative."""
        decision = decide(intent(notional=5_000.0))
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.approved_notional == pytest.approx(5_000.0)

    def test_a_two_symbol_intent_is_sized_by_its_larger_factor(self):
        """§23-F applied. Factor 2 halves the permitted per-leg size, because
        both symbols can be transiently one-sided at once."""
        proposed = intent(
            legs=[leg(VENUE_A, Side.BUY), leg(VENUE_B, Side.SELL, symbol=ETH)],
            notional=ORDER,
        )
        decision = decide(proposed)
        assert decision.approved_notional == pytest.approx(
            UNHEDGED / (2 * gates.execution_multiplier(proposed))
        )
        assert gate_named(
            decision, "MAX_UNHEDGED_EXPOSURE"
        ).observed == pytest.approx(UNHEDGED)


class TestTheProjectedBoundary:
    """§23-B and §23-C, asked of the gate directly so the boundary is exact
    rather than mediated by whatever the sizing path chose."""

    def test_exactly_on_the_boundary_passes(self):
        exact = UNHEDGED / gates.execution_multiplier(intent())
        check = gates.gate_unhedged(intent(notional=exact), 0.0, ONLY_UNHEDGED)
        assert check.observed == pytest.approx(UNHEDGED)
        assert not check.blocking

    def test_one_step_beyond_it_fails(self):
        over = UNHEDGED / gates.execution_multiplier(intent()) + 1.0
        check = gates.gate_unhedged(intent(notional=over), 0.0, ONLY_UNHEDGED)
        assert check.observed > UNHEDGED
        assert check.blocking

    def test_the_detail_separates_the_three_terms(self):
        """An operator reading a rejection must be able to tell which term
        consumed the budget; the gate is conservative and says so."""
        check = gates.gate_unhedged(
            intent(notional=1_000.0),
            2_000.0,
            ONLY_UNHEDGED,
            committed=CommittedExposure(unhedged_fill_risk=3_000.0),
        )
        assert "2,000.00 actual" in check.detail
        assert "3,000.00 pending" in check.detail
        assert "1,001.00 incoming" in check.detail
        assert check.observed == pytest.approx(6_001.0)


# ======================================================================
# §19, §20 — what else consumes the budget
# ======================================================================


class TestCurrentResidualConsumesHeadroom:
    """§19. 4,000 of actual residual leaves roughly 6,000 of per-leg room."""

    def test_the_remaining_budget_is_what_is_left_of_the_limit(self):
        decision = decide(intent(notional=ORDER), unhedged_notional=4_000.0)
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(
            (UNHEDGED - 4_000.0) / gates.execution_multiplier(intent())
        )
        assert decision.approved_notional == pytest.approx(5_994.0, abs=1.0)

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_direction_does_not_matter_to_the_actual_term(self, sign):
        decision = decide(intent(notional=ORDER), unhedged_notional=4_000.0 * sign)
        assert decision.approved_notional == pytest.approx(
            (UNHEDGED - 4_000.0) / gates.execution_multiplier(intent())
        )

    def test_a_residual_at_the_limit_leaves_nothing(self):
        """No reduction can help, so the decision short-circuits — the same
        shape a fully consumed gross or venue budget already takes."""
        decision = decide(intent(notional=ORDER), unhedged_notional=UNHEDGED)
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == pytest.approx(0.0)
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]


class TestPendingEntriesConsumeHeadroom:
    """§20. A balanced pair already working, 7,000 remaining a side, bounds at
    7,000 of pending fill risk — so the next opportunity gets roughly 3,000,
    not another 10,000 and certainly not another 25,000."""

    PENDING = CommittedExposure(
        gross_exposure=14_000.0,
        net_exposure=0.0,
        unhedged_fill_risk=7_000.0,
    )

    def test_the_second_opportunity_is_sized_against_what_is_working(self):
        decision = decide(intent(notional=ORDER), committed_exposure=self.PENDING)
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(
            (UNHEDGED - 7_000.0) / gates.execution_multiplier(intent())
        )
        assert decision.approved_notional == pytest.approx(2_997.0, abs=1.0)

    def test_the_projection_still_lands_on_the_limit(self):
        decision = decide(intent(notional=ORDER), committed_exposure=self.PENDING)
        check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
        assert check.observed == pytest.approx(UNHEDGED)
        assert "7,000.00 pending" in check.detail

    def test_actual_and_pending_compose(self):
        """Neither alone exhausts the budget; together they leave 1,000."""
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=2_000.0,
            committed_exposure=self.PENDING,
        )
        assert decision.approved_notional == pytest.approx(
            1_000.0 / gates.execution_multiplier(intent())
        )

    def test_a_fully_committed_budget_authorises_nothing_further(self):
        full = CommittedExposure(unhedged_fill_risk=UNHEDGED)
        decision = decide(intent(notional=ORDER), committed_exposure=full)
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]


# ======================================================================
# The sizing/gating contract, on this dimension
# ======================================================================


class TestHeadroomAndGateAgree:
    """``evaluate`` promises a size-based gate can only fail if the reduction
    could not make it pass. That holds only while the solver and the gate use
    the same three terms."""

    @pytest.mark.parametrize("actual", [0.0, 1_000.0, 5_000.0, 9_000.0])
    @pytest.mark.parametrize("pending", [0.0, 500.0, 4_000.0])
    def test_a_sized_intent_always_clears_the_gate(self, actual, pending):
        reserved = CommittedExposure(unhedged_fill_risk=pending)
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=actual,
            committed_exposure=reserved,
        )
        if decision.approved:
            check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
            assert not check.blocking, (
                f"sized to {decision.approved_notional} against actual={actual} "
                f"pending={pending}, and the gate still failed at "
                f"{check.observed}"
            )

    @pytest.mark.parametrize("factor_legs", [1, 2])
    def test_the_solver_reproduces_the_gates_boundary(self, factor_legs):
        """Solve, then substitute back: the projection must land on the limit,
        never past it."""
        legs = [leg(VENUE_A, Side.BUY)]
        if factor_legs == 2:
            legs.append(leg(VENUE_B, Side.BUY, symbol=ETH))
        proposed = intent(legs=legs, notional=ORDER)
        allowed = gates.unhedged_headroom(proposed, 1_500.0, ONLY_UNHEDGED)
        at_bound = proposed.model_copy(update={"notional": allowed})
        check = gates.gate_unhedged(at_bound, 1_500.0, ONLY_UNHEDGED)
        assert check.observed == pytest.approx(UNHEDGED)
        assert not check.blocking

    def test_headroom_is_never_negative(self):
        assert gates.unhedged_headroom(intent(), 50_000.0, ONLY_UNHEDGED) == 0.0

    def test_headroom_never_enlarges_a_request(self):
        """It is a ceiling, not a target: ``_headroom`` takes a min over every
        candidate including the requested notional."""
        for requested in (250.0, 1_000.0, 5_000.0, ORDER):
            decision = decide(intent(notional=requested))
            assert decision.approved_notional <= requested + 1e-9


class TestNothingElseMoved:
    """Guards on what this pass must not have changed."""

    def test_the_configured_limits_are_untouched(self):
        assert SHIPPED.max_unhedged_notional == pytest.approx(10_000.0)
        assert SHIPPED.max_order_notional == pytest.approx(25_000.0)
        assert SHIPPED.min_trade_notional == pytest.approx(250.0)

    def test_the_kill_switch_still_reads_the_actual_residual_only(self):
        """The backstop fires on state that exists, not on state an order
        could create. Feeding pending fill risk into it would engage the
        emergency layer merely because a trade is in flight."""
        import inspect

        from risk.kill_switch import live_risk_breaches

        source = inspect.getsource(live_risk_breaches)
        assert "inputs.unhedged_notional" in source
        assert "unhedged_fill_risk" not in source

    def test_risk_utilization_keeps_reporting_the_actual_residual(self):
        """The dashboard field means filled-book residual and must keep
        meaning it; the projection is reported beside it, not folded in."""
        engine = core(ONLY_UNHEDGED)
        reserved = CommittedExposure(unhedged_fill_risk=7_000.0)
        util = engine.utilization(
            context().portfolio, 2_000.0, {SYMBOL: 0.0}, reserved
        )
        assert util.unhedged_notional == pytest.approx(2_000.0)
        assert util.pending_unhedged_fill_risk == pytest.approx(7_000.0)
