"""Phase 5 Remediation A — exact boundaries for the repaired primitives.

The audit tests state safety invariants ("exposure must never exceed the
limit"). Those are the right shape for finding a defect, but they do not pin
the *edge*: an implementation that reduced every trade to zero would satisfy
every one of them.

This module pins the boundary from both sides. For each repaired constraint:
one size that must be permitted, and the next one up that must not — so a
future change that is merely conservative is as visible as one that is unsafe.

Scope is exactly the five findings remediated in this pass:

* **P5-2 / P5-13** strategy exposure counted in one gross unit end to end
* **P5-7** ``_headroom`` bounds net exposure and leverage
* **P5-4** open-order capacity counts the orders the intent will create
* **P5-11** venue and position projections aggregate duplicate legs
"""

from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import GateResult, RiskVerdict
from risk import limits as gates
from tests.audit.rune_fixtures import (
    VENUE_A,
    VENUE_B,
    VENUE_C,
    context,
    core,
    gate_named,
    intent,
    leg,
    portfolio,
    portfolio_with,
    position,
)
from tests.conftest import START_MS

#: Wide everywhere, so each test below narrows exactly one limit and nothing
#: else binds first.
OPEN = dict(
    max_order_notional=1_000_000.0,
    max_position_notional=1_000_000.0,
    max_gross_exposure=10_000_000.0,
    max_net_exposure=10_000_000.0,
    max_venue_exposure=1_000_000.0,
    max_strategy_exposure=10_000_000.0,
    max_leverage=1_000_000.0,
)
OPEN_CTX = {"max_economical_notional": 10_000_000.0}


def limits(**overrides) -> RiskLimits:
    return RiskLimits(**{**OPEN, **overrides})


def decide(limit_overrides: dict, intent_kwargs: dict, **ctx_kwargs):
    return core(limits(**limit_overrides)).evaluate(
        intent(**intent_kwargs), context(**{**OPEN_CTX, **ctx_kwargs}), START_MS
    )


# ======================================================================
# P5-2 / P5-13 — strategy exposure
# ======================================================================


class TestStrategyExposureBoundary:
    """``projected == limit`` passes; anything above is reduced or rejected."""

    LIMIT = 100_000.0
    PER_LEG = 25_000.0

    def test_projected_exactly_at_the_limit_passes(self):
        # 50,000 already working + 25,000 x 2 legs = exactly 100,000.
        decision = decide(
            {"max_strategy_exposure": self.LIMIT},
            {"notional": self.PER_LEG},
            strategy_exposure=50_000.0,
        )
        assert decision.verdict is RiskVerdict.APPROVED
        check = gate_named(decision, "MAX_STRATEGY_EXPOSURE")
        assert check.observed == pytest.approx(self.LIMIT)
        assert check.result is GateResult.PASS

    def test_projected_just_above_the_limit_is_reduced_to_fit(self):
        decision = decide(
            {"max_strategy_exposure": self.LIMIT},
            {"notional": self.PER_LEG},
            strategy_exposure=50_001.0,
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        # (100,000 - 50,001) / 2 legs.
        assert decision.approved_notional == pytest.approx(24_999.5)
        assert gate_named(
            decision, "MAX_STRATEGY_EXPOSURE"
        ).observed == pytest.approx(self.LIMIT)

    def test_an_exhausted_budget_rejects(self):
        decision = decide(
            {"max_strategy_exposure": self.LIMIT},
            {"notional": self.PER_LEG},
            strategy_exposure=self.LIMIT,
        )
        assert decision.verdict is RiskVerdict.REJECTED

    def test_the_reservation_unit_makes_two_trades_fill_the_budget(self):
        """The P5-2 arithmetic, end to end: two two-leg trades at the 25,000
        per-leg cap consume the whole 100,000 gross budget, and a third gets
        nothing."""
        engine = core(limits(max_strategy_exposure=self.LIMIT))
        working: dict[str, float] = {}
        verdicts = []
        for index in range(3):
            proposed = intent(
                opportunity_id=f"opp-{index}",
                correlation_id=f"opp-{index}",
                notional=self.PER_LEG,
            )
            decision = engine.evaluate(
                proposed,
                context(strategy_exposure=sum(working.values()), **OPEN_CTX),
                START_MS,
            )
            verdicts.append(decision.verdict)
            if decision.approved:
                working[proposed.opportunity_id] = decision.approved_notional * len(
                    proposed.legs
                )
        assert verdicts == [
            RiskVerdict.APPROVED,
            RiskVerdict.APPROVED,
            RiskVerdict.REJECTED,
        ]
        assert sum(working.values()) == pytest.approx(self.LIMIT)

    def test_utilization_reports_the_same_gross_unit(self):
        """P5-13: the dashboard shows the budget consumed, not half of it."""
        working = {"opp-0": self.PER_LEG * 2, "opp-1": self.PER_LEG * 2}
        utilization = core(limits(max_strategy_exposure=self.LIMIT)).utilization(
            portfolio(), 0.0, {"cross_venue": sum(working.values())}
        )
        assert utilization.strategy_exposure["cross_venue"] == pytest.approx(
            self.LIMIT
        )


# ======================================================================
# P5-7 — net exposure headroom
# ======================================================================


class TestNetExposureHeadroomBoundary:
    """``abs(current + coefficient * n) <= limit``, solved exactly."""

    LIMIT = 10_000.0

    @staticmethod
    def _one_leg(side: Side, notional: float, venue: str = VENUE_A):
        return {"legs": [leg(venue, side)], "notional": notional}

    def test_a_request_exactly_at_the_bound_is_not_reduced(self):
        decision = decide(
            {"max_net_exposure": self.LIMIT}, self._one_leg(Side.BUY, self.LIMIT)
        )
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.approved_notional == pytest.approx(self.LIMIT)

    def test_a_request_just_above_the_bound_is_reduced_to_it(self):
        decision = decide(
            {"max_net_exposure": self.LIMIT},
            self._one_leg(Side.BUY, self.LIMIT + 1.0),
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(self.LIMIT)
        assert gate_named(decision, "MAX_NET_EXPOSURE").observed == pytest.approx(
            self.LIMIT
        )

    def test_direction_matters_against_an_existing_position(self):
        """The whole reason ``limit - abs(current)`` is wrong.

        With +8,000 already held, a BUY has 2,000 of room and a SELL has
        18,000 — the naive form would give both 2,000.
        """
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=80.0))
        assert book.net_exposure == pytest.approx(8_000.0)

        buying = decide(
            {"max_net_exposure": self.LIMIT},
            self._one_leg(Side.BUY, 50_000.0),
            portfolio=book,
        )
        selling = decide(
            {"max_net_exposure": self.LIMIT},
            self._one_leg(Side.SELL, 50_000.0),
            portfolio=book,
        )
        assert buying.approved_notional == pytest.approx(2_000.0)
        assert selling.approved_notional == pytest.approx(18_000.0)

    def test_a_reducing_trade_may_cross_through_zero(self):
        """Selling 18,000 against +8,000 lands on -10,000, exactly the limit on
        the other side. The solver's feasible interval spans the origin."""
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=80.0))
        decision = decide(
            {"max_net_exposure": self.LIMIT},
            self._one_leg(Side.SELL, 50_000.0),
            portfolio=book,
        )
        check = gate_named(decision, "MAX_NET_EXPOSURE")
        assert check.observed == pytest.approx(self.LIMIT)
        assert check.result is GateResult.PASS

    def test_a_balanced_two_leg_trade_is_unbounded_by_net_exposure(self):
        """Coefficient zero: no size changes the projection, so this
        constraint must not cap the trade at all."""
        proposed = intent(notional=500_000.0)
        assert gates.net_exposure_coefficient(proposed) == 0.0
        assert gates.net_exposure_headroom(
            proposed, portfolio(), limits(max_net_exposure=self.LIMIT)
        ) == float("inf")

    def test_a_balanced_trade_against_a_breached_book_still_rejects(self):
        """Unbounded headroom is not a bypass: if the book alone is already
        past the limit, no size helps and the gate is the authority."""
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=500.0))
        assert book.net_exposure == pytest.approx(50_000.0)
        decision = decide(
            {"max_net_exposure": self.LIMIT}, {"notional": 1_000.0}, portfolio=book
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert "MAX_NET_EXPOSURE" in decision.reason_codes

    def test_a_book_past_the_limit_gives_a_one_sided_trade_no_room(self):
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=500.0))
        assert gates.net_exposure_headroom(
            intent(**self._one_leg(Side.BUY, 1_000.0)),
            book,
            limits(max_net_exposure=self.LIMIT),
        ) == pytest.approx(0.0)

    def test_headroom_is_never_negative(self):
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=5_000.0))
        for side in (Side.BUY, Side.SELL):
            value = gates.net_exposure_headroom(
                intent(**self._one_leg(side, 1_000.0)),
                book,
                limits(max_net_exposure=self.LIMIT),
            )
            assert value >= 0.0

    def test_rune_never_enlarges_a_small_risk_reducing_request(self):
        """A tiny SELL against a breached long would need to be far bigger to
        re-enter the permitted band. RUNE reduces, never enlarges, so the
        request stands and the gate rejects it."""
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=500.0))
        decision = decide(
            {"max_net_exposure": self.LIMIT},
            self._one_leg(Side.SELL, 1_000.0),
            portfolio=book,
        )
        assert decision.requested_notional == pytest.approx(1_000.0)
        assert decision.approved_notional <= 1_000.0
        assert decision.verdict is RiskVerdict.REJECTED


# ======================================================================
# P5-7 — leverage headroom
# ======================================================================


class TestLeverageHeadroomBoundary:
    """``(gross + n * legs) / equity <= max_leverage``, solved exactly."""

    @staticmethod
    def _book():
        # Equity 100,000 (60,000 cash + a 40,000 long); gross 40,000.
        return portfolio_with(
            position(VENUE_A, quantity=400.0), cash=60_000.0, peak_equity=100_000.0
        )

    def test_the_solver_matches_the_gate_formula(self):
        book = self._book()
        allowed = gates.leverage_headroom(
            intent(notional=1.0), book, limits(max_leverage=1.0)
        )
        # (1.0 * 100,000 - 40,000) / 2 legs.
        assert allowed == pytest.approx(30_000.0)

    def test_a_request_at_the_bound_is_not_reduced(self):
        decision = decide(
            {"max_leverage": 1.0}, {"notional": 30_000.0}, portfolio=self._book()
        )
        assert decision.verdict is RiskVerdict.APPROVED
        assert gate_named(decision, "MAX_LEVERAGE").observed == pytest.approx(1.0)

    def test_a_request_above_the_bound_is_reduced_to_it(self):
        decision = decide(
            {"max_leverage": 1.0}, {"notional": 30_001.0}, portfolio=self._book()
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(30_000.0)
        assert gate_named(decision, "MAX_LEVERAGE").observed == pytest.approx(1.0)

    def test_leverage_headroom_scales_with_the_leg_count(self):
        book = self._book()
        one = gates.leverage_headroom(
            intent(legs=[leg(VENUE_A, Side.BUY)], notional=1.0),
            book,
            limits(max_leverage=1.0),
        )
        three = gates.leverage_headroom(
            intent(
                legs=[
                    leg(VENUE_A, Side.BUY),
                    leg(VENUE_B, Side.SELL),
                    leg(VENUE_C, Side.BUY),
                ],
                notional=1.0,
            ),
            book,
            limits(max_leverage=1.0),
        )
        assert one == pytest.approx(60_000.0)
        assert three == pytest.approx(20_000.0)

    @pytest.mark.parametrize("cash", [0.0, -1.0, -50_000.0])
    def test_non_positive_equity_yields_no_headroom_and_still_blocks(self, cash):
        book = portfolio(cash=cash)
        assert book.equity <= 0
        assert gates.leverage_headroom(
            intent(notional=1_000.0), book, limits(max_leverage=1.0)
        ) == pytest.approx(0.0)
        decision = decide({"max_leverage": 1.0}, {"notional": 1_000.0}, portfolio=book)
        assert decision.verdict is RiskVerdict.REJECTED

    def test_a_gross_position_already_past_the_leverage_limit_gives_zero(self):
        book = portfolio_with(
            position(VENUE_A, quantity=2_000.0), cash=0.0, peak_equity=200_000.0
        )
        assert gates.leverage_headroom(
            intent(notional=1_000.0), book, limits(max_leverage=0.5)
        ) == pytest.approx(0.0)


# ======================================================================
# P5-4 — open-order capacity
# ======================================================================


class TestOpenOrderBoundary:
    LIMIT = 20

    @pytest.mark.parametrize(
        ("current", "legs", "expected_pass"),
        [
            (18, 2, True),   # 18 + 2 == 20
            (19, 2, False),  # 19 + 2 == 21
            (19, 1, True),   # 19 + 1 == 20
            (20, 1, False),  # 20 + 1 == 21
            (17, 3, True),   # 17 + 3 == 20
            (18, 3, False),  # 18 + 3 == 21
        ],
    )
    def test_the_projected_count_decides(self, current, legs, expected_pass):
        venues = [VENUE_A, VENUE_B, VENUE_C][:legs]
        sides = [Side.BUY, Side.SELL, Side.BUY][:legs]
        proposed = intent(
            legs=[leg(v, s) for v, s in zip(venues, sides, strict=True)]
        )
        decision = decide(
            {"max_open_orders": self.LIMIT},
            {"legs": proposed.legs},
            open_orders=current,
        )
        check = gate_named(decision, "MAX_OPEN_ORDERS")
        assert check.observed == pytest.approx(float(current + legs))
        assert (check.result is GateResult.PASS) is expected_pass

    def test_reducing_the_notional_does_not_change_the_count(self):
        """The gate is not size-reducible, so a huge request that gets cut down
        still creates the same number of orders."""
        decision = decide(
            {"max_open_orders": self.LIMIT, "max_order_notional": 1_000.0,
             "max_position_notional": 1_000.0},
            {"notional": 500_000.0},
            open_orders=19,
        )
        assert decision.approved_notional <= 1_000.0
        assert gate_named(decision, "MAX_OPEN_ORDERS").observed == pytest.approx(21.0)
        assert decision.verdict is RiskVerdict.REJECTED

    def test_the_detail_explains_the_arithmetic(self):
        decision = decide(
            {"max_open_orders": self.LIMIT}, {}, open_orders=19
        )
        assert (
            gate_named(decision, "MAX_OPEN_ORDERS").detail
            == "19 live + 2 incoming = 21"
        )


# ======================================================================
# P5-11 — duplicate-leg aggregation
# ======================================================================


class TestDuplicateVenueBoundary:
    LIMIT = 20_000.0

    @staticmethod
    def _two_on_one_venue(notional: float):
        return {
            "legs": [
                leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                leg(VENUE_A, Side.BUY, symbol="ETH-USD"),
            ],
            "notional": notional,
        }

    def test_two_legs_on_one_venue_project_twice(self):
        decision = decide(
            {"max_venue_exposure": self.LIMIT}, self._two_on_one_venue(10_000.0)
        )
        check = gate_named(decision, "MAX_VENUE_EXPOSURE")
        assert check.observed == pytest.approx(20_000.0)
        assert check.result is GateResult.PASS

    def test_the_boundary_is_exact(self):
        assert (
            decide(
                {"max_venue_exposure": self.LIMIT}, self._two_on_one_venue(10_000.0)
            ).verdict
            is RiskVerdict.APPROVED
        )
        above = decide(
            {"max_venue_exposure": self.LIMIT}, self._two_on_one_venue(10_001.0)
        )
        assert above.verdict is RiskVerdict.APPROVED_REDUCED
        assert above.approved_notional == pytest.approx(10_000.0)

    def test_headroom_divides_by_the_venues_leg_count(self):
        """The sizing path and the gate must describe one model: half the
        remaining venue room per leg, not the whole of it twice."""
        decision = decide(
            {"max_venue_exposure": self.LIMIT}, self._two_on_one_venue(500_000.0)
        )
        assert decision.approved_notional == pytest.approx(10_000.0)
        assert gate_named(
            decision, "MAX_VENUE_EXPOSURE"
        ).observed == pytest.approx(self.LIMIT)

    def test_existing_exposure_is_shared_across_the_duplicate_legs(self):
        book = portfolio_with(position(VENUE_A, "SOL-USD", quantity=100.0))
        assert book.exposure_by_venue()[VENUE_A] == pytest.approx(10_000.0)
        decision = decide(
            {"max_venue_exposure": self.LIMIT},
            self._two_on_one_venue(500_000.0),
            portfolio=book,
        )
        # (20,000 - 10,000) / 2 legs.
        assert decision.approved_notional == pytest.approx(5_000.0)

    def test_distinct_venues_are_unaffected(self):
        """A control: the shipped two-leg strategy uses distinct venues, and
        its sizing must not change."""
        decision = decide({"max_venue_exposure": self.LIMIT}, {"notional": 20_000.0})
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.approved_notional == pytest.approx(20_000.0)


class TestDuplicatePositionBoundary:
    LIMIT = 20_000.0

    @staticmethod
    def _two_on_one_position(notional: float):
        return {
            "legs": [leg(VENUE_A, Side.BUY), leg(VENUE_A, Side.BUY)],
            "notional": notional,
        }

    def test_two_legs_on_one_position_project_twice(self):
        decision = decide(
            {"max_position_notional": self.LIMIT, "max_order_notional": self.LIMIT},
            self._two_on_one_position(10_000.0),
        )
        check = gate_named(decision, "MAX_POSITION_NOTIONAL")
        assert check.observed == pytest.approx(20_000.0)
        assert check.result is GateResult.PASS

    def test_the_boundary_is_exact(self):
        above = decide(
            {"max_position_notional": self.LIMIT, "max_order_notional": self.LIMIT},
            self._two_on_one_position(10_001.0),
        )
        assert above.verdict is RiskVerdict.APPROVED_REDUCED
        assert above.approved_notional == pytest.approx(10_000.0)

    def test_opposing_legs_on_one_position_are_added_not_netted(self):
        """Deliberately conservative. RUNE has no guaranteed fill sequence for
        a generic multi-leg intent, so it cannot assume the two offset."""
        decision = decide(
            {"max_position_notional": self.LIMIT, "max_order_notional": self.LIMIT},
            {
                "legs": [leg(VENUE_A, Side.BUY), leg(VENUE_A, Side.SELL)],
                "notional": 10_000.0,
            },
        )
        assert gate_named(
            decision, "MAX_POSITION_NOTIONAL"
        ).observed == pytest.approx(20_000.0)

    def test_different_symbols_on_one_venue_are_separate_positions(self):
        """Grouping is by ``venue:symbol``, so two symbols on one venue are two
        positions even though they are one venue."""
        decision = decide(
            {"max_position_notional": self.LIMIT, "max_order_notional": self.LIMIT},
            {
                "legs": [
                    leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                    leg(VENUE_A, Side.BUY, symbol="ETH-USD"),
                ],
                "notional": 15_000.0,
            },
        )
        assert gate_named(
            decision, "MAX_POSITION_NOTIONAL"
        ).observed == pytest.approx(15_000.0)


# ======================================================================
# the grouping helpers themselves
# ======================================================================


class TestLegGrouping:
    def test_venue_grouping_counts_every_leg(self):
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                leg(VENUE_A, Side.SELL, symbol="ETH-USD"),
                leg(VENUE_B, Side.BUY),
            ]
        )
        assert gates.legs_per_venue(proposed) == {VENUE_A: 2, VENUE_B: 1}

    def test_position_grouping_keys_on_venue_and_symbol(self):
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                leg(VENUE_A, Side.SELL, symbol="BTC-USD"),
                leg(VENUE_A, Side.BUY, symbol="ETH-USD"),
            ]
        )
        assert gates.legs_per_position(proposed) == {
            f"{VENUE_A}:BTC-USD": 2,
            f"{VENUE_A}:ETH-USD": 1,
        }

    def test_the_counts_sum_to_the_leg_count(self):
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY),
                leg(VENUE_A, Side.BUY),
                leg(VENUE_B, Side.SELL),
            ]
        )
        assert sum(gates.legs_per_venue(proposed).values()) == 3
        assert sum(gates.legs_per_position(proposed).values()) == 3

    @pytest.mark.parametrize(
        ("sides", "expected"),
        [
            ((Side.BUY,), 1.0),
            ((Side.SELL,), -1.0),
            ((Side.BUY, Side.SELL), 0.0),
            ((Side.BUY, Side.BUY), 2.0),
            ((Side.BUY, Side.SELL, Side.BUY), 1.0),
        ],
    )
    def test_the_net_coefficient_is_the_signed_leg_count(self, sides, expected):
        venues = [VENUE_A, VENUE_B, VENUE_C][: len(sides)]
        proposed = intent(
            legs=[leg(v, s) for v, s in zip(venues, sides, strict=True)]
        )
        assert gates.net_exposure_coefficient(proposed) == pytest.approx(expected)


class TestNothingElseMoved:
    """Guards against the remediation quietly changing the ordinary path."""

    def test_the_shipped_two_leg_shape_is_still_approved_unreduced(self):
        decision = core(RiskLimits()).evaluate(intent(), context(), START_MS)
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.approved_notional == pytest.approx(5_000.0)
        assert decision.reason_codes == ["ALL_GATES_PASSED"]

    def test_the_gate_list_is_unchanged(self):
        decision = core(RiskLimits()).evaluate(intent(), context(), START_MS)
        assert [g.name for g in decision.gates] == [
            "KILL_SWITCH_CLEAR",
            "SYSTEM_HEALTHY",
            "EXECUTION_HEALTHY",
            "CONSENSUS_THRESHOLD",
            "MARKET_DATA_FRESH",
            "INTENT_NOT_EXPIRED",
            "MIN_EXPECTED_EDGE",
            "LIQUIDITY_SUFFICIENT",
            "HEDGE_AVAILABLE",
            "MAX_ORDER_NOTIONAL",
            "MAX_POSITION_NOTIONAL",
            "MAX_GROSS_EXPOSURE",
            "MAX_NET_EXPOSURE",
            "MAX_LEVERAGE",
            "MAX_VENUE_EXPOSURE",
            "MAX_STRATEGY_EXPOSURE",
            "MAX_DAILY_LOSS",
            "MAX_DRAWDOWN",
            "MAX_UNHEDGED_EXPOSURE",
            "MAX_OPEN_ORDERS",
            "MAX_ERROR_RATE",
        ]

    def test_default_limits_are_untouched(self):
        """This pass is arithmetic correctness, not calibration."""
        defaults = RiskLimits()
        assert defaults.max_position_notional == pytest.approx(50_000.0)
        assert defaults.max_gross_exposure == pytest.approx(150_000.0)
        assert defaults.max_net_exposure == pytest.approx(25_000.0)
        assert defaults.max_leverage == pytest.approx(2.0)
        assert defaults.max_daily_loss == pytest.approx(2_500.0)
        assert defaults.max_drawdown == pytest.approx(5_000.0)
        assert defaults.max_venue_exposure == pytest.approx(75_000.0)
        assert defaults.max_strategy_exposure == pytest.approx(100_000.0)
        assert defaults.max_order_notional == pytest.approx(25_000.0)
        assert defaults.min_trade_notional == pytest.approx(250.0)
        assert defaults.max_unhedged_notional == pytest.approx(10_000.0)
        assert defaults.max_open_orders == 20

    def test_headroom_still_never_enlarges_a_request(self):
        for requested in (250.0, 1_000.0, 5_000.0, 25_000.0, 1e9):
            decision = core(RiskLimits()).evaluate(
                intent(notional=requested), context(), START_MS
            )
            assert decision.approved_notional <= requested + 1e-9

    def test_the_decision_is_deterministic(self):
        proposed = intent()
        ctx = context()
        engine = core(RiskLimits())
        first = engine.evaluate(proposed, ctx, START_MS)
        second = engine.evaluate(proposed, ctx, START_MS)
        assert first.verdict is second.verdict
        assert first.approved_notional == second.approved_notional
        assert [
            (g.name, g.result, g.observed, g.limit) for g in first.gates
        ] == [(g.name, g.result, g.observed, g.limit) for g in second.gates]
