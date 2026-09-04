"""Phase 1 polish: portfolio P&L terminology and formulas.

``PositionState.apply()`` returns realised trading P&L *before* fees;
``FillEvent.cash_delta`` already subtracts the fee from cash. The two
`PortfolioState` properties disagreed with that: the old code defined

    gross_pnl = realized_pnl + unrealized_pnl + fees_paid   # added fees BACK
    net_pnl   = realized_pnl + unrealized_pnl               # never subtracted them

so "net" P&L was actually the pre-fee number, and "gross" overstated even
that by double-counting fees on top. For a flat account this made the
dashboard's net P&L larger than the account's actual equity gain by exactly
the fee total.

The corrected model:

    gross_pnl = realized_pnl + unrealized_pnl        (before fees)
    net_pnl   = gross_pnl - fees_paid                 (after fees -- reality)

For a flat account, ``equity - initial_balance`` must reconcile to
``net_pnl`` within numerical tolerance, because cash already has every fee
subtracted exactly once.
"""

from __future__ import annotations

import pytest

from core.models.common import Side
from core.models.execution import FillEvent
from core.models.portfolio import PortfolioState
from execution.paper import PaperAccount
from tests.conftest import START_MS

VENUE = "VENUE_A"
SYMBOL = "BTC-USD"


def fill(*, side, quantity, price, fee=0.0, ts=START_MS):
    return FillEvent(
        created_at=ts,
        client_order_id="order-1",
        venue=VENUE,
        symbol=SYMBOL,
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
    )


class TestFlatRoundTripWithProfit:
    def test_gross_is_pre_fee_and_net_is_gross_minus_fees(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=1.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=110.0, fee=1.0))
        snap = account.snapshot()

        assert snap.realized_pnl == pytest.approx(10.0)
        assert snap.fees_paid == pytest.approx(2.0)
        assert snap.gross_pnl == pytest.approx(10.0), "gross must be the pre-fee trading P&L"
        assert snap.net_pnl == pytest.approx(8.0), "net must be gross less total fees"
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl)


class TestFlatRoundTripWithLoss:
    def test_a_losing_round_trip_reconciles_too(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=1.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=95.0, fee=1.0))
        snap = account.snapshot()

        assert snap.realized_pnl == pytest.approx(-5.0)
        assert snap.gross_pnl == pytest.approx(-5.0)
        assert snap.net_pnl == pytest.approx(-7.0)
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl)


class TestOpenPositionIncludesUnrealized:
    def test_gross_includes_realized_and_unrealized_net_subtracts_fees_once(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=2.0, price=100.0, fee=1.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=110.0, fee=1.0))
        account.mark({SYMBOL: 120.0})
        snap = account.snapshot()

        assert snap.realized_pnl == pytest.approx(10.0)  # closed 1 unit at +10
        assert snap.unrealized_pnl == pytest.approx(20.0)  # 1 unit remaining, +20 mark
        assert snap.fees_paid == pytest.approx(2.0)
        assert snap.gross_pnl == pytest.approx(30.0)
        assert snap.net_pnl == pytest.approx(28.0)
        # Not flat: equity - initial_balance includes the mark-to-market, so it
        # equals net_pnl too (cash + signed_notional at the new mark).
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl)


class TestMultipleFillsAndPartialFills:
    def test_several_partial_fills_still_reconcile(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=0.4, price=100.0, fee=0.4))
        account.apply_fill(fill(side=Side.BUY, quantity=0.6, price=102.0, fee=0.6))
        account.apply_fill(fill(side=Side.SELL, quantity=0.5, price=108.0, fee=0.5))
        account.apply_fill(fill(side=Side.SELL, quantity=0.5, price=109.0, fee=0.5))
        snap = account.snapshot()

        assert snap.gross_pnl == pytest.approx(snap.realized_pnl + snap.unrealized_pnl)
        assert snap.net_pnl == pytest.approx(snap.gross_pnl - snap.fees_paid)
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl, abs=1e-6)


class TestMakerAndTakerFees:
    def test_different_fee_amounts_all_land_in_fees_paid_once(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=0.10))  # maker
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=105.0, fee=0.25))  # taker
        snap = account.snapshot()

        assert snap.fees_paid == pytest.approx(0.35)
        assert snap.net_pnl == pytest.approx(snap.gross_pnl - 0.35)
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl)


class TestZeroFees:
    def test_zero_fees_makes_gross_and_net_equal(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=0.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=103.0, fee=0.0))
        snap = account.snapshot()

        assert snap.fees_paid == 0.0
        assert snap.gross_pnl == pytest.approx(snap.net_pnl)
        assert snap.equity - snap.initial_balance == pytest.approx(snap.net_pnl)


class TestNoDoubleSubtraction:
    def test_fees_are_not_subtracted_twice_between_cash_and_net_pnl(self, clock):
        """Cash already has every fee subtracted via cash_delta. net_pnl must
        subtract fees_paid exactly once more to explain the SAME equity move
        -- not zero times (the old bug) and not twice.
        """
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=3.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=100.0, fee=3.0))
        snap = account.snapshot()

        # Flat, zero price move: the entire equity change is the fee drag.
        assert snap.equity - snap.initial_balance == pytest.approx(-6.0)
        assert snap.net_pnl == pytest.approx(-6.0)
        assert snap.gross_pnl == pytest.approx(0.0), "price did not move; gross is fee-blind"


class TestPrometheusMetricsUseCorrectedSemantics:
    def test_net_pnl_and_gross_pnl_gauges_match_the_corrected_properties(self):
        from monitoring.metrics import GROSS_PNL, NET_PNL, MetricsRegistry

        registry = MetricsRegistry()
        portfolio = PortfolioState(
            created_at=START_MS,
            initial_balance=100_000.0,
            cash=100_050.0,
            realized_pnl=60.0,
            fees_paid=10.0,
        )
        registry.set(NET_PNL, portfolio.net_pnl)
        registry.set(GROSS_PNL, portfolio.gross_pnl)
        text = registry.render()
        assert "tf_gross_pnl 60.0" in text
        assert "tf_net_pnl 50.0" in text


class TestMarinReconciliationStillPasses:
    def test_recompute_from_fills_agrees_with_the_corrected_properties(self, clock):
        account = PaperAccount(clock=clock, initial_balance=100_000.0)
        account.apply_fill(fill(side=Side.BUY, quantity=1.0, price=100.0, fee=1.0))
        account.apply_fill(fill(side=Side.SELL, quantity=1.0, price=104.0, fee=1.0))
        cash, _positions, realized = account.recompute_from_fills()
        assert cash == pytest.approx(account.cash)
        assert realized == pytest.approx(account.realized_pnl)
        snap = account.snapshot()
        assert snap.net_pnl == pytest.approx(realized - account.fees_paid)
