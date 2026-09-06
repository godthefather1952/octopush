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

    def test_the_detail_separates_every_term(self):
        """An operator reading a rejection must be able to tell which term
        consumed the budget; the gate is conservative and says so.

        Four terms since Remediation D2 — the recovery reserve is named even
        when it is zero, so the format is stable and nothing is hidden.
        """
        check = gates.gate_unhedged(
            intent(notional=1_000.0),
            2_000.0,
            ONLY_UNHEDGED,
            committed=CommittedExposure(unhedged_fill_risk=3_000.0),
        )
        assert "2,000.00 actual" in check.detail
        assert "3,000.00 pending" in check.detail
        assert "1,001.00 incoming" in check.detail
        assert "0.00 recovery reserve" in check.detail
        assert "6,001.00 stressed" in check.detail
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


# ======================================================================
# P5-18 part B — recovery headroom (Remediation D2)
# ======================================================================

#: ``Settings.hedge_tolerance_notional``'s shipped value. Pinned against the
#: real configuration by ``TestTheReserveComesFromConfiguration`` below rather
#: than assumed here.
RESERVE = 500.0

#: What an entry may actually consume once the reserve is held back.
EFFECTIVE = UNHEDGED - RESERVE          # 9,500


class TestWhyTheReserveExists:
    """The measurement that produced this change, restated as a test.

    External validation ran the shipped platform and captured the first
    emergency trigger: MAX_UNHEDGED_EXPOSURE alone, actual 10,022.9337 against
    a 10,000 limit, with pending fill risk 0.0 and every other dimension
    comfortably inside. The dominant residual was ETH-USD on VENUE_A, quantity
    -2.4292661940053453, entry price 4,108.63747827, mark 4,125.91.

    So the position did NOT fill above the limit — its entry notional was
    9,980.9741, inside it. It was MARKED to 10,022.9337 while still one-sided,
    42.04 bps of ordinary movement, with the exit already CANCEL_PENDING and
    OKAPI's hedge already SUBMITTING. Recovery was working; it simply had no
    room between itself and the emergency ceiling.

    The asynchronous fill-sequence projection was right. Sizing that temporary
    leg flush against the hard limit was the remaining half.
    """

    QUANTITY = 2.4292661940053453
    ENTRY_PRICE = 4_108.63747827
    MARK_PRICE = 4_125.91

    def test_the_observed_entry_notional_was_inside_the_limit(self):
        entry_notional = self.QUANTITY * self.ENTRY_PRICE
        assert entry_notional == pytest.approx(9_980.9741, abs=0.05)
        assert entry_notional < UNHEDGED

    def test_the_observed_mark_notional_was_outside_it(self):
        mark_notional = self.QUANTITY * self.MARK_PRICE
        assert mark_notional == pytest.approx(10_022.9337, abs=0.05)
        assert mark_notional > UNHEDGED

    def test_the_drift_that_crossed_the_limit_was_ordinary(self):
        drift_bps = (self.MARK_PRICE / self.ENTRY_PRICE - 1.0) * 10_000.0
        assert drift_bps == pytest.approx(42.0395, abs=0.1)

    def test_the_reserve_is_far_larger_than_that_drift(self):
        """Not a guarantee that markets cannot move more — they can, and the
        backstop is what catches it. The reserve only stops normal recovery
        from starting with zero headroom."""
        drift_notional = self.QUANTITY * (self.MARK_PRICE - self.ENTRY_PRICE)
        assert drift_notional == pytest.approx(41.9596, abs=0.05)
        assert drift_notional < RESERVE


class TestTheReserveComesFromConfiguration:
    """§6 / §21 — no new invented buffer, and exactly one source."""

    def test_it_is_okapis_own_hedge_tolerance(self):
        from core.config import load_settings

        settings = load_settings()
        assert settings.hedge_tolerance_notional == pytest.approx(RESERVE)

    def test_okapi_measures_against_the_same_number(self):
        """The reserve ties entry sizing to the mechanism that unwinds the
        exposure: it is the delta OKAPI already tolerates before hedging."""
        import inspect

        from agents.okapi.agent import Okapi

        source = inspect.getsource(Okapi.delta_reports)
        assert "self.settings.hedge_tolerance_notional" in source

    def test_the_orchestrator_passes_it_straight_through(self):
        """§21. One value, read from settings, not copied or recomputed."""
        import inspect

        import apps.orchestrator.orchestrator as orch
        from apps.orchestrator.orchestrator import Orchestrator

        risk_check = inspect.getsource(Orchestrator._risk_check)
        assert (
            "hedge_tolerance_notional=self.settings.hedge_tolerance_notional,"
            in risk_check
        )
        mentions = [
            line.strip()
            for line in inspect.getsource(orch).splitlines()
            if "hedge_tolerance_notional" in line
        ]
        assert mentions == [
            "hedge_tolerance_notional=self.settings.hedge_tolerance_notional,"
        ], (
            "the reserve must be read from settings in exactly one place; "
            f"a second copy is how two safety layers come to disagree: {mentions}"
        )

    def test_the_hard_limit_itself_is_unchanged(self):
        """§7. The reserve changes what an ENTRY may consume, never the
        ceiling the emergency layer watches."""
        assert SHIPPED.max_unhedged_notional == pytest.approx(10_000.0)


class TestTheEffectiveEntryBudget:
    """§20-A/B — the reserve comes off the top of the entry budget."""

    def test_a_25k_request_is_sized_to_the_effective_budget(self):
        """§20-A. Approved size uses 9,500, not 10,000."""
        decision = decide(intent(notional=ORDER), hedge_tolerance_notional=RESERVE)
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(
            EFFECTIVE / gates.execution_multiplier(intent())
        )
        assert decision.approved_notional == pytest.approx(9_490.51, abs=0.01)

    def test_the_stressed_projection_lands_on_the_hard_limit(self):
        """§11. ``observed`` is the stressed total including the reserve, so
        the gate's ``observed <= limit`` contract still reads against the real
        ceiling rather than a second, quieter one."""
        decision = decide(intent(notional=ORDER), hedge_tolerance_notional=RESERVE)
        check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
        assert check.observed == pytest.approx(UNHEDGED)
        assert check.limit == pytest.approx(UNHEDGED)
        assert not check.blocking

    def test_the_detail_names_the_reserve(self):
        """§11 — do not hide it."""
        decision = decide(intent(notional=ORDER), hedge_tolerance_notional=RESERVE)
        detail = gate_named(decision, "MAX_UNHEDGED_EXPOSURE").detail
        assert "0.00 actual" in detail
        assert "0.00 pending" in detail
        assert "9,500.00 incoming" in detail
        assert "500.00 recovery reserve" in detail
        assert "10,000.00 stressed" in detail

    def test_zero_reserve_reproduces_the_previous_behaviour(self):
        """§20-B. A caller that reserves nothing gets exactly what Remediation
        D gave it, so the change is additive rather than a re-tuning."""
        decision = decide(intent(notional=ORDER), hedge_tolerance_notional=0.0)
        assert decision.approved_notional == pytest.approx(
            UNHEDGED / gates.execution_multiplier(intent())
        )
        assert decision.approved_notional == pytest.approx(9_990.01, abs=0.01)

    def test_the_default_context_reserves_nothing(self):
        """The field defaults to zero, so every existing direct caller and
        audit fixture is unaffected."""
        assert context().hedge_tolerance_notional == pytest.approx(0.0)

    def test_a_negative_reserve_cannot_enlarge_the_budget(self):
        """Clamped at zero: a reserve is something held back, never handed
        out."""
        decision = decide(intent(notional=ORDER), hedge_tolerance_notional=-5_000.0)
        assert decision.approved_notional == pytest.approx(
            UNHEDGED / gates.execution_multiplier(intent())
        )


class TestTheReserveComposesWithWhatIsAlreadyThere:
    """§17, §18, §19 — it is in addition to actual and pending, not instead."""

    def test_actual_residual_and_the_reserve_both_come_off(self):
        """§17 / §20-C. 10,000 hard, 500 reserved, 2,000 already held leaves
        7,500 for pending plus incoming — not 8,000."""
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=2_000.0,
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.approved_notional == pytest.approx(
            7_500.0 / gates.execution_multiplier(intent())
        )

    def test_the_four_thousand_case(self):
        """§20-C as stated: 4,000 actual and a 500 reserve leave roughly 5,500
        of incoming budget before the slippage multiplier."""
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=4_000.0,
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.approved_notional == pytest.approx(
            5_500.0 / gates.execution_multiplier(intent())
        )
        assert gate_named(
            decision, "MAX_UNHEDGED_EXPOSURE"
        ).observed == pytest.approx(UNHEDGED)

    def test_pending_entry_risk_and_the_reserve_both_come_off(self):
        """§18 / §20-D. 7,000 already working plus a 500 reserve leave 2,500."""
        decision = decide(
            intent(notional=ORDER),
            committed_exposure=CommittedExposure(unhedged_fill_risk=7_000.0),
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.approved_notional == pytest.approx(
            2_500.0 / gates.execution_multiplier(intent())
        )

    def test_all_three_compose(self):
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=1_000.0,
            committed_exposure=CommittedExposure(unhedged_fill_risk=2_000.0),
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.approved_notional == pytest.approx(
            6_500.0 / gates.execution_multiplier(intent())
        )

    @pytest.mark.parametrize(
        ("actual", "pending"),
        [(9_500.0, 0.0), (0.0, 9_500.0), (5_000.0, 4_500.0), (9_600.0, 0.0)],
    )
    def test_a_consumed_effective_budget_leaves_no_headroom(self, actual, pending):
        """§19. ``actual + pending >= hard - reserve`` authorises nothing, and
        rejects early at MIN_TRADE_NOTIONAL — the same shape leverage and the
        rest of the size-sensitive limits already take. The gate is not forced
        to appear merely for diagnostics."""
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=actual,
            committed_exposure=CommittedExposure(unhedged_fill_risk=pending),
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == pytest.approx(0.0)
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]

    def test_just_inside_the_effective_budget_still_trades(self):
        """The boundary is not blanket-conservative: at 9,000 of actual there
        is still 500 of room, which is above ``min_trade_notional``."""
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=9_000.0,
            hedge_tolerance_notional=RESERVE,
        )
        assert decision.approved
        assert decision.approved_notional == pytest.approx(
            500.0 / gates.execution_multiplier(intent())
        )


class TestGateAndHeadroomStillMirrorEachOther:
    """The sizing contract has to survive the fourth term."""

    @pytest.mark.parametrize("reserve", [0.0, 250.0, RESERVE, 2_000.0])
    @pytest.mark.parametrize("actual", [0.0, 1_500.0, 6_000.0])
    def test_a_sized_intent_always_clears_the_gate(self, reserve, actual):
        decision = decide(
            intent(notional=ORDER),
            unhedged_notional=actual,
            hedge_tolerance_notional=reserve,
        )
        if decision.approved:
            check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
            assert not check.blocking, (
                f"sized to {decision.approved_notional} with actual={actual} "
                f"reserve={reserve}, and the gate still failed at "
                f"{check.observed}"
            )

    @pytest.mark.parametrize("reserve", [0.0, RESERVE, 2_000.0])
    def test_the_solver_reproduces_the_gates_boundary(self, reserve):
        proposed = intent(notional=ORDER)
        allowed = gates.unhedged_headroom(
            proposed, 1_500.0, ONLY_UNHEDGED, recovery_reserve=reserve
        )
        at_bound = proposed.model_copy(update={"notional": allowed})
        check = gates.gate_unhedged(
            at_bound, 1_500.0, ONLY_UNHEDGED, recovery_reserve=reserve
        )
        assert check.observed == pytest.approx(UNHEDGED)
        assert not check.blocking

    def test_a_reserve_larger_than_the_limit_yields_zero(self):
        assert gates.unhedged_headroom(
            intent(), 0.0, ONLY_UNHEDGED, recovery_reserve=UNHEDGED * 2
        ) == 0.0

    def test_headroom_never_enlarges_a_request(self):
        for requested in (250.0, 1_000.0, 5_000.0, ORDER):
            decision = decide(
                intent(notional=requested), hedge_tolerance_notional=RESERVE
            )
            assert decision.approved_notional <= requested + 1e-9


class TestTheReserveIsPreTradeOnly:
    """§15, §16 — it must not leak into the emergency layer or the dashboard."""

    def test_the_kill_switch_knows_nothing_about_it(self):
        """§15 / §20-F. The backstop watches the actual hard limit at 10,000.
        Moving it to 9,500 would turn a safety margin into a second, hidden
        ceiling and defeat the point of having a margin at all."""
        import inspect

        from risk.kill_switch import KillSwitchInputs, live_risk_breaches

        source = inspect.getsource(live_risk_breaches)
        assert "hedge_tolerance" not in source
        assert "recovery_reserve" not in source
        assert "inputs.unhedged_notional" in source
        assert not hasattr(
            KillSwitchInputs(portfolio=context().portfolio, health=None),
            "hedge_tolerance_notional",
        )

    def test_the_backstop_still_fires_only_at_the_hard_limit(self):
        """A residual inside the hard limit but past the effective entry
        budget is not an emergency — it is exactly the room the reserve was
        set aside to provide."""
        from core.config import load_settings, simulated_venues
        from risk.kill_switch import KillSwitchInputs, live_risk_breaches

        settings = load_settings().model_copy(
            update={"venues": simulated_venues()}
        )
        inside = KillSwitchInputs(
            portfolio=context().portfolio,
            health=None,
            unhedged_notional=EFFECTIVE + 100.0,
        )
        assert live_risk_breaches(inside, settings) == []

        outside = KillSwitchInputs(
            portfolio=context().portfolio,
            health=None,
            unhedged_notional=UNHEDGED + 0.01,
        )
        assert live_risk_breaches(outside, settings) == ["MAX_UNHEDGED_EXPOSURE"]

    def test_utilization_reports_neither_the_reserve_nor_a_reduced_limit(self):
        """§16 / §20-G. ``unhedged_notional`` stays the actual filled-book
        residual and ``max_unhedged_notional`` stays the real ceiling."""
        engine = core(ONLY_UNHEDGED)
        util = engine.utilization(
            context().portfolio,
            2_000.0,
            {SYMBOL: 0.0},
            CommittedExposure(unhedged_fill_risk=7_000.0),
        )
        assert util.unhedged_notional == pytest.approx(2_000.0)
        assert util.pending_unhedged_fill_risk == pytest.approx(7_000.0)
        assert util.max_unhedged_notional == pytest.approx(UNHEDGED)
