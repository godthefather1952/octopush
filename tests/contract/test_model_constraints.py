"""Domain models must refuse impossible states — P0-M3.

The audit found four states that validated cleanly and mean nothing:

    PositionState(average_entry_price=-100)   # paid a negative price
    PositionState(mark_price=-1)              # the market quoted below zero
    PortfolioState(initial_balance=-5)        # funded with a debt
    AgentOpinion(created_at=1000, expires_at=500)   # born expired, ttl -500ms

None had a live caller, which is exactly why they matter: a constraint that
is only enforced by every caller remembering to is not enforced. These are
the values a Phase 1 bug would produce, and the model is where they should
stop rather than three layers downstream where the symptom is a nonsensical
P&L.
"""

from __future__ import annotations

import pytest

from core.models.agent import AgentOpinion
from core.models.common import AgentId
from core.models.market import PriceLevel
from core.models.portfolio import PortfolioState, PositionState


def opinion(**overrides):
    fields = {
        "created_at": 1_000,
        "expires_at": 2_000,
        "agent_id": AgentId.NORO,
        "symbol": "BTC-USD",
        "signal": 0.0,
        "confidence": 0.5,
        "model_version": "v1",
    }
    fields.update(overrides)
    return AgentOpinion(**fields)


class TestPositionState:
    def test_a_negative_average_entry_price_is_refused(self):
        """You cannot have been paid to take on a long position."""
        with pytest.raises(ValueError):
            PositionState(venue="VENUE_A", symbol="BTC-USD", average_entry_price=-100.0)

    def test_a_negative_mark_price_is_refused(self):
        with pytest.raises(ValueError):
            PositionState(venue="VENUE_A", symbol="BTC-USD", mark_price=-1.0)

    def test_a_zero_mark_price_is_refused(self):
        """A mark of zero silently values every position at nothing."""
        with pytest.raises(ValueError):
            PositionState(venue="VENUE_A", symbol="BTC-USD", mark_price=0.0)

    def test_an_absent_mark_is_still_allowed(self):
        """Unmarked is a real state; zero is not how to say it."""
        assert PositionState(venue="VENUE_A", symbol="BTC-USD").mark_price is None

    def test_a_flat_position_may_have_a_zero_entry_price(self):
        """Zero is the correct entry price for a position that is flat."""
        position = PositionState(venue="VENUE_A", symbol="BTC-USD")
        assert position.average_entry_price == 0.0
        assert position.is_flat

    def test_a_short_position_is_still_allowed(self):
        """Negative *quantity* is normal; negative *price* is not."""
        short = PositionState(
            venue="VENUE_A", symbol="BTC-USD", quantity=-2.0, average_entry_price=50_000.0
        )
        assert short.quantity == -2.0

    def test_negative_realised_pnl_is_still_allowed(self):
        """Losses are legal."""
        assert (
            PositionState(venue="VENUE_A", symbol="BTC-USD", realized_pnl=-500.0).realized_pnl
            == -500.0
        )


class TestPortfolioState:
    def test_a_negative_initial_balance_is_refused(self):
        with pytest.raises(ValueError):
            PortfolioState(created_at=1, initial_balance=-5.0, cash=0.0)

    def test_a_zero_initial_balance_is_refused(self):
        """An account funded with nothing can never trade; say so at once."""
        with pytest.raises(ValueError):
            PortfolioState(created_at=1, initial_balance=0.0, cash=0.0)

    def test_negative_cash_is_still_allowed(self):
        """Cash goes negative on a short sale; that is real, not impossible."""
        state = PortfolioState(created_at=1, initial_balance=100_000.0, cash=-5_000.0)
        assert state.cash == -5_000.0


class TestAgentOpinion:
    def test_an_opinion_cannot_expire_before_it_was_created(self):
        """Born expired: ttl_ms was -500 and quality_at() called it STALE.

        Nothing distinguished that from a genuinely aged opinion, so a clock
        or wiring bug would present as an agent that had merely gone quiet.
        """
        with pytest.raises(ValueError):
            opinion(created_at=1_000, expires_at=500)

    def test_an_opinion_expiring_at_its_creation_instant_is_allowed(self):
        """A zero TTL is degenerate but meaningful: valid only right now."""
        assert opinion(created_at=1_000, expires_at=1_000).ttl_ms == 0

    def test_a_normal_ttl_still_works(self):
        assert opinion(created_at=1_000, expires_at=3_500).ttl_ms == 2_500

    def test_ttl_is_never_negative(self):
        """The property the constraint exists to guarantee."""
        assert opinion().ttl_ms >= 0


class TestAlreadyGuarded:
    """Confirming the audit's read that these were already correct."""

    def test_price_levels_refuse_non_positive_values(self):
        with pytest.raises(ValueError):
            PriceLevel(price=-1.0, size=1.0)
        with pytest.raises(ValueError):
            PriceLevel(price=1.0, size=-1.0)

    def test_signal_and_confidence_stay_in_range(self):
        with pytest.raises(ValueError):
            opinion(signal=1.5)
        with pytest.raises(ValueError):
            opinion(confidence=-0.1)


class TestSnapshotsAreDetached:
    """P0-L4: a shallow model_copy aliases nested positions.

    No live path was affected — `PaperAccount.snapshot()` already passes
    `deep=True` — but the footgun is latent, and a snapshot that keeps
    changing is worse than no snapshot: every consumer would believe it held
    the moment it asked for.
    """

    def test_an_account_snapshot_does_not_track_later_trades(self):
        from core.clock import ManualClock
        from core.models.common import Liquidity, Side
        from core.models.execution import FillEvent
        from execution.paper.account import PaperAccount

        clock = ManualClock(start_ms=1_700_000_000_000)
        account = PaperAccount(clock=clock, initial_balance=1_000_000.0)
        account.apply_fill(
            FillEvent(
                created_at=clock.now_ms(), client_order_id="ord-1", venue="VENUE_A",
                symbol="BTC-USD", side=Side.BUY, quantity=1.0, price=50_000.0,
                fee=1.0, liquidity=Liquidity.TAKER,
            )
        )
        before = account.snapshot()
        quantity_at_snapshot = before.positions["VENUE_A:BTC-USD"].quantity

        account.apply_fill(
            FillEvent(
                created_at=clock.now_ms(), client_order_id="ord-2", venue="VENUE_A",
                symbol="BTC-USD", side=Side.BUY, quantity=3.0, price=50_100.0,
                fee=1.0, liquidity=Liquidity.TAKER,
            )
        )

        assert before.positions["VENUE_A:BTC-USD"].quantity == quantity_at_snapshot
        assert account.positions["VENUE_A:BTC-USD"].quantity == 4.0

    def test_a_shallow_copy_would_have_aliased_it(self):
        """Pins why deep=True is load-bearing rather than decorative."""
        from core.models.portfolio import PortfolioState, PositionState

        position = PositionState(venue="VENUE_A", symbol="BTC-USD", quantity=1.0)
        state = PortfolioState(
            created_at=1, initial_balance=1_000.0, cash=1_000.0,
            positions={"VENUE_A:BTC-USD": position},
        )

        shallow = state.model_copy()
        deep = state.model_copy(deep=True)
        position.quantity = 99.0

        assert shallow.positions["VENUE_A:BTC-USD"].quantity == 99.0, "aliased, as expected"
        assert deep.positions["VENUE_A:BTC-USD"].quantity == 1.0
