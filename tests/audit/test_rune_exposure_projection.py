"""Phase 5 — H5: can any exposure projection UNDERSTATE post-trade risk?

Conservatism is acceptable in a hard-risk layer; understatement never is. A
gate that overstates blocks a trade that would have been fine, which costs
opportunity. A gate that understates authorises a trade that breaches a limit,
which costs the limit its meaning.

Each projection is checked against an independently-computed post-trade
portfolio, built by actually applying the trade to positions rather than by
re-deriving production's formula.

Scope note: these tests treat ``intent.notional`` as per-leg, which is how
VESKA sizes and how ``gate_gross_exposure`` projects. Fill uncertainty is out
of scope — RUNE is not asked to predict partial fills — but the *modelled*
post-trade state must never come out below what the gate claimed.
"""

from __future__ import annotations

import pytest

from core.config import RiskLimits
from core.models.common import Side
from core.models.portfolio import PortfolioState, PositionState
from core.models.risk import RiskVerdict
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

#: Generous everywhere, so nothing is reduced and the gate reports the
#: projection for the size actually requested.
OPEN = RiskLimits(
    max_position_notional=10_000_000.0,
    max_gross_exposure=100_000_000.0,
    max_net_exposure=100_000_000.0,
    max_leverage=1_000_000.0,
    max_venue_exposure=10_000_000.0,
    max_strategy_exposure=100_000_000.0,
    max_order_notional=10_000_000.0,
)

PRICE = 100.0


def apply_trade(book: PortfolioState, proposed) -> PortfolioState:
    """The portfolio after every leg fills in full at its reference price.

    An audit-only reference: it uses ``PositionState.apply``, which is the
    platform's own fill arithmetic, so the comparison is against what the
    account would really record rather than against a second guess at it.
    """
    after = book.model_copy(deep=True)
    for one in proposed.legs:
        key = f"{one.venue}:{one.symbol}"
        held = after.positions.get(key)
        if held is None:
            held = PositionState(venue=one.venue, symbol=one.symbol)
            after.positions[key] = held
        quantity = proposed.notional / one.reference_price
        held.apply(one.side, quantity, one.reference_price, 0.0)
        held.mark_price = one.reference_price
    return after


def projection(proposed, book, name: str) -> float:
    decision = core(OPEN).evaluate(
        proposed,
        context(portfolio=book, max_economical_notional=10_000_000.0),
        START_MS,
    )
    check = gate_named(decision, name)
    assert check.observed is not None
    return check.observed


class TestGrossExposureIsNeverUnderstated:
    @pytest.mark.parametrize(
        "book",
        [
            portfolio(),
            portfolio_with(position(VENUE_A, quantity=100.0)),
            portfolio_with(position(VENUE_A, quantity=-100.0)),
            portfolio_with(
                position(VENUE_A, quantity=100.0), position(VENUE_B, quantity=-100.0)
            ),
            portfolio_with(position(VENUE_C, "ETH-USD", quantity=250.0)),
        ],
    )
    @pytest.mark.parametrize("notional", [1_000.0, 25_000.0])
    def test_projection_is_at_least_the_realised_gross(self, book, notional):
        proposed = intent(notional=notional)
        claimed = projection(proposed, book, "MAX_GROSS_EXPOSURE")
        realised = apply_trade(book, proposed).gross_exposure
        assert claimed >= realised - 1e-6, (
            f"gross projection {claimed} is below the {realised} the trade "
            "would actually produce"
        )

    def test_an_exposure_reducing_trade_is_projected_conservatively(self):
        """Closing a long is projected as though it opened a new one.

        Deliberate: RUNE cannot know a leg will close rather than open, since
        an entry leg carries no quantity. Overstatement here is safe, and this
        test records it as a design decision rather than a defect.
        """
        book = portfolio_with(position(VENUE_A, quantity=250.0))  # +25,000
        proposed = intent(legs=[leg(VENUE_A, Side.SELL)], notional=25_000.0)
        claimed = projection(proposed, book, "MAX_GROSS_EXPOSURE")
        realised = apply_trade(book, proposed).gross_exposure
        assert realised == pytest.approx(0.0), "the trade actually flattens"
        assert claimed == pytest.approx(50_000.0)
        assert claimed >= realised

    @pytest.mark.parametrize("legs", [1, 2, 3])
    def test_every_leg_count_is_projected_at_full_notional(self, legs):
        venues = [VENUE_A, VENUE_B, VENUE_C][:legs]
        sides = [Side.BUY, Side.SELL, Side.BUY][:legs]
        proposed = intent(
            legs=[leg(v, s) for v, s in zip(venues, sides, strict=True)],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_GROSS_EXPOSURE")
        assert claimed == pytest.approx(10_000.0 * legs)

    def test_two_legs_on_one_venue_and_symbol_are_both_counted(self):
        """A degenerate but representable intent. Both legs must contribute."""
        proposed = intent(
            legs=[leg(VENUE_A, Side.BUY), leg(VENUE_A, Side.BUY)],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_GROSS_EXPOSURE")
        realised = apply_trade(portfolio(), proposed).gross_exposure
        assert realised == pytest.approx(20_000.0)
        assert claimed >= realised - 1e-6


class TestVenueExposureIsNeverUnderstated:
    def test_one_leg_per_venue_is_projected_correctly(self):
        book = portfolio_with(position(VENUE_A, quantity=100.0))
        proposed = intent(notional=10_000.0)
        claimed = projection(proposed, book, "MAX_VENUE_EXPOSURE")
        after = apply_trade(book, proposed).exposure_by_venue()
        assert claimed >= max(after.values()) - 1e-6

    def test_two_legs_on_the_same_venue_are_aggregated(self):
        """``gate_venue_exposure`` groups legs by venue before projecting.

        Taking ``max`` over ungrouped legs answered "what is the largest single
        leg's effect", which is not the question a venue limit asks: two legs
        routed to one venue put ``2 * notional`` on it (P5-11).
        """
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                leg(VENUE_A, Side.BUY, symbol="ETH-USD"),
            ],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_VENUE_EXPOSURE")
        after = apply_trade(portfolio(), proposed).exposure_by_venue()
        assert after[VENUE_A] == pytest.approx(20_000.0)
        assert claimed >= after[VENUE_A] - 1e-6, (
            f"venue projection {claimed} is below the {after[VENUE_A]} both "
            f"legs would put on {VENUE_A}"
        )

    def test_three_legs_on_the_same_venue_compound_the_same_way(self):
        proposed = intent(
            legs=[
                leg(VENUE_A, Side.BUY, symbol="BTC-USD"),
                leg(VENUE_A, Side.BUY, symbol="ETH-USD"),
                leg(VENUE_A, Side.BUY, symbol="SOL-USD"),
            ],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_VENUE_EXPOSURE")
        after = apply_trade(portfolio(), proposed).exposure_by_venue()
        assert claimed >= after[VENUE_A] - 1e-6


class TestPositionExposureIsNeverUnderstated:
    @pytest.mark.parametrize(
        ("held", "side"),
        [
            (0.0, Side.BUY),
            (0.0, Side.SELL),
            (100.0, Side.BUY),    # long, increasing
            (-100.0, Side.SELL),  # short, increasing
            (100.0, Side.SELL),   # long, reducing
            (-100.0, Side.BUY),   # short, reducing
            (50.0, Side.SELL),    # flips through zero
        ],
    )
    def test_projection_is_at_least_the_realised_position(self, held, side):
        book = (
            portfolio_with(position(VENUE_A, quantity=held))
            if held
            else portfolio()
        )
        proposed = intent(legs=[leg(VENUE_A, side)], notional=10_000.0)
        claimed = projection(proposed, book, "MAX_POSITION_NOTIONAL")
        after = apply_trade(book, proposed)
        realised = max(
            (p.notional for p in after.positions.values()), default=0.0
        )
        assert claimed >= realised - 1e-6, (
            f"position projection {claimed} is below the realised {realised} "
            f"(held {held}, {side.value} 10,000)"
        )

    def test_two_legs_on_one_position_are_aggregated(self):
        """``gate_position_notional`` groups by ``venue:symbol`` before
        projecting, so two legs landing on one position add twice (P5-11)."""
        proposed = intent(
            legs=[leg(VENUE_A, Side.BUY), leg(VENUE_A, Side.BUY)],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_POSITION_NOTIONAL")
        after = apply_trade(portfolio(), proposed)
        realised = after.positions[f"{VENUE_A}:BTC-USD"].notional
        assert realised == pytest.approx(20_000.0)
        assert claimed >= realised - 1e-6, (
            f"position projection {claimed} counts one leg where the trade "
            f"would build a {realised} position"
        )


class TestNetExposureIsNeverUnderstated:
    """H5 / §17. The projection is ``|net + sum(side.sign * notional)|``."""

    def test_a_balanced_two_leg_trade_projects_no_change(self):
        claimed = projection(intent(notional=10_000.0), portfolio(), "MAX_NET_EXPOSURE")
        realised = abs(apply_trade(portfolio(), intent(notional=10_000.0)).net_exposure)
        assert realised == pytest.approx(0.0)
        assert claimed >= realised - 1e-6

    @pytest.mark.parametrize(
        "sides",
        [
            (Side.BUY,),
            (Side.SELL,),
            (Side.BUY, Side.SELL),
            (Side.BUY, Side.BUY),
            (Side.SELL, Side.SELL),
            (Side.BUY, Side.SELL, Side.BUY),
            (Side.BUY, Side.BUY, Side.SELL),
        ],
    )
    def test_projection_is_at_least_the_realised_net(self, sides):
        venues = [VENUE_A, VENUE_B, VENUE_C][: len(sides)]
        proposed = intent(
            legs=[leg(v, s) for v, s in zip(venues, sides, strict=True)],
            notional=10_000.0,
        )
        claimed = projection(proposed, portfolio(), "MAX_NET_EXPOSURE")
        realised = abs(apply_trade(portfolio(), proposed).net_exposure)
        assert claimed >= realised - 1e-6, (
            f"net projection {claimed} is below the realised {realised} for "
            f"{[s.value for s in sides]}"
        )

    @pytest.mark.parametrize("held", [250.0, -250.0, 50.0, -50.0])
    def test_an_existing_position_is_included(self, held):
        book = portfolio_with(position(VENUE_C, "ETH-USD", quantity=held))
        proposed = intent(legs=[leg(VENUE_A, Side.BUY)], notional=10_000.0)
        claimed = projection(proposed, book, "MAX_NET_EXPOSURE")
        realised = abs(apply_trade(book, proposed).net_exposure)
        assert claimed >= realised - 1e-6

    def test_a_leg_offsetting_an_existing_position_is_projected_conservatively(self):
        """The offset case: the projection adds the leg's signed delta without
        knowing the leg will close rather than open. Conservative here means
        the projection can be LARGER than reality, which is safe."""
        book = portfolio_with(position(VENUE_A, quantity=100.0))  # +10,000
        proposed = intent(legs=[leg(VENUE_A, Side.SELL)], notional=10_000.0)
        claimed = projection(proposed, book, "MAX_NET_EXPOSURE")
        realised = abs(apply_trade(book, proposed).net_exposure)
        assert realised == pytest.approx(0.0)
        assert claimed >= realised - 1e-6
        assert claimed == pytest.approx(0.0), (
            "the signed projection happens to be exact here; recorded so a "
            "change in the formula is visible"
        )


class TestLeverageIsNeverUnderstated:
    @pytest.mark.parametrize(
        "book",
        [
            portfolio(),
            portfolio_with(position(VENUE_A, quantity=100.0)),
            portfolio_with(position(VENUE_A, quantity=-100.0), cash=110_000.0),
        ],
    )
    def test_projection_is_at_least_the_realised_leverage(self, book):
        proposed = intent(notional=10_000.0)
        claimed = projection(proposed, book, "MAX_LEVERAGE")
        after = apply_trade(book, proposed)
        realised = after.gross_exposure / after.equity if after.equity > 0 else 0.0
        assert claimed >= realised - 1e-9, (
            f"leverage projection {claimed} is below the realised {realised}"
        )

    def test_leverage_uses_pre_trade_equity(self):
        """Recorded, not judged: the denominator is equity before the trade.

        Fills move equity only by fees and by mark-to-market, so a pre-trade
        denominator is a close and slightly conservative stand-in.
        """
        book = portfolio()
        claimed = projection(intent(notional=10_000.0), book, "MAX_LEVERAGE")
        assert claimed == pytest.approx(20_000.0 / book.equity)

    @pytest.mark.parametrize("equity_cash", [0.0, -1.0, -50_000.0])
    def test_the_leverage_gate_itself_fails_on_non_positive_equity(self, equity_cash):
        """The gate's own behaviour, asked of the gate directly.

        Split out of what used to be a single ``evaluate`` test. Since the
        P5-7 remediation gave MAX_LEVERAGE a headroom solver, non-positive
        equity yields zero headroom, and ``evaluate`` short-circuits on
        MIN_TRADE_NOTIONAL before the twenty-one-gate list ever runs — so
        ``gate_named(decision, "MAX_LEVERAGE")`` no longer finds a gate to
        inspect. The gate is unchanged and still fail-closed; only the path
        that reaches it changed, so the assertion moves to the gate.
        """
        book = portfolio(cash=equity_cash)
        assert book.equity <= 0
        check = gates.gate_leverage(intent(notional=1_000.0), book, OPEN)
        assert check.blocking, "non-positive equity must never authorise a trade"
        assert "non-positive equity" in check.detail

    @pytest.mark.parametrize("equity_cash", [0.0, -1.0, -50_000.0])
    def test_non_positive_equity_always_blocks(self, equity_cash):
        """The safety property, unchanged: no trade is authorised.

        Which gate does the blocking is an implementation detail; that nothing
        is authorised is not. Rejection now arrives earlier and more cheaply,
        via zero headroom, and the decision must say so rather than approving
        anything at all.
        """
        book = portfolio(cash=equity_cash)
        assert book.equity <= 0
        decision = core(OPEN).evaluate(
            intent(notional=1_000.0),
            context(portfolio=book, max_economical_notional=10_000_000.0),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == pytest.approx(0.0)
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]

    def test_tiny_positive_equity_produces_an_enormous_projection(self):
        book = portfolio(cash=0.01)
        assert book.equity == pytest.approx(0.01)
        claimed = projection(intent(notional=1_000.0), book, "MAX_LEVERAGE")
        assert claimed == pytest.approx(2_000.0 / 0.01)
        assert claimed > OPEN.max_leverage or claimed > 1e5


class TestUnhedgedExposure:
    @pytest.mark.parametrize("unhedged", [0.0, -9_999.0, 9_999.0, -10_000.0, 10_000.0])
    def test_the_gate_uses_absolute_value(self, unhedged):
        decision = core(RiskLimits()).evaluate(
            intent(), context(unhedged_notional=unhedged), START_MS
        )
        check = gate_named(decision, "MAX_UNHEDGED_EXPOSURE")
        assert check.observed == pytest.approx(abs(unhedged))
        assert not check.blocking

    @pytest.mark.parametrize("unhedged", [-10_000.01, 10_000.01, 50_000.0])
    def test_beyond_the_limit_blocks_in_both_directions(self, unhedged):
        decision = core(RiskLimits()).evaluate(
            intent(), context(unhedged_notional=unhedged), START_MS
        )
        assert gate_named(decision, "MAX_UNHEDGED_EXPOSURE").blocking

    def test_the_gate_does_not_project_the_intents_own_residual(self):
        """Recorded scope: the unhedged gate reads OKAPI's current measurement
        and does not add what this trade might leave behind. A perfectly
        balanced two-leg trade leaves nothing, so for the current strategy the
        distinction does not bite -- but it is the shape of the guarantee."""
        import inspect

        from risk import limits as gates

        source = inspect.getsource(gates.gate_unhedged)
        assert "intent" not in source
