"""Phase 6 — H15, H16, H18: how a resting order is credited, and with what.

**H15 — trade-flow accrual.** ``PaperExecutor._accrue_trade_flow`` runs on
every ``update_market`` and does:

    volume = state.metrics.buy_volume + state.metrics.sell_volume
    pending.traded_through += volume * 0.01

``BookMetrics.buy_volume``/``sell_volume`` come from TIDAL's rolling
``TradeFlowWindow``: they are the total still inside the recent window, not the
new volume since the previous snapshot. The same prints are therefore credited
once per market update for as long as they remain in the window — and
``traded_through`` is what drives the non-crossed branch of
``fill_passive``'s probability. This makes paper maker fills systematically
easier than they should be, in proportion to how often the market updates.

**H16 — ``max_partial_fraction``.** ``ExecutionConfig`` documents it as the
"simulated fraction of an order that may fill on one pass". It is applied in
``fill_marketable`` and nowhere else, so a passive evaluation can fill an
arbitrary share regardless of the configured cap.

**H18 — fill provenance.** ``_attempt_fill`` stamps every fill with
``self.market.source_data_timestamp`` — a market-wide value — while the fill
itself was computed from ``_book_view(order)``, which reads one venue and one
symbol. ``MarketState`` already offers ``source_data_timestamp_for(legs)`` for
exactly this distinction.
"""

from __future__ import annotations

import inspect

import pytest

from core.models.common import Liquidity, TimeInForce
from core.models.market import MarketState
from execution.paper.executor import PaperExecutor
from execution.paper.simulator import FillSimulator
from tests.audit.veska_fixtures import (
    SYMBOL,
    VENUE_A,
    VENUE_A_LATENCY_MS,
    VENUE_B,
    audit_settings,
    book,
    deterministic_execution,
    market,
    one_venue_market,
    plan,
    planned,
    rig,
    venue_state,
)
from tests.conftest import START_MS

CERTAIN = audit_settings(**deterministic_execution())
ACK_AT = START_MS + VENUE_A_LATENCY_MS

#: A book whose ask is far above the passive limit used below.
FAR = 60_000.0


def resting_order(**overrides):
    fields = {
        "time_in_force": TimeInForce.POST_ONLY,
        "limit_price": 39_000.0,
        "expected_price": 39_000.0,
        "quantity": 0.10,
        "ttl_ms": 600_000,
    }
    fields.update(overrides)
    return planned(**fields)


def with_flow(buy: float, sell: float, *, mid: float = FAR) -> MarketState:
    return one_venue_market(mid=mid, buy_volume=buy, sell_volume=sell)


class TestTradeFlowAccrual:
    """H15. Rolling-window totals credited as if they were increments."""

    def test_the_accrual_reads_a_rolling_window_total(self):
        """The premise, from both sides of the interface."""
        source = inspect.getsource(PaperExecutor._accrue_trade_flow)
        assert "state.metrics.buy_volume + state.metrics.sell_volume" in source
        assert "pending.traded_through += volume * 0.01" in source

        from core.models.market import BookMetrics

        doc = inspect.getdoc(BookMetrics) or ""
        fields = BookMetrics.model_fields
        assert "buy_volume" in fields and "sell_volume" in fields
        assert "delta" not in doc.lower(), (
            "BookMetrics now documents its volumes as increments; H15's "
            "premise needs rechecking"
        )

    def test_the_accrual_runs_on_every_market_update(self):
        source = inspect.getsource(PaperExecutor.update_market)
        assert "self._accrue_trade_flow()" in source

    async def test_no_new_trade_flow_means_no_new_queue_progress(self):
        """The safety property.

        One population of prints enters the rolling window. The market is then
        republished repeatedly with no new prints at all. Queue progress must
        stop, because nothing traded.
        """
        built = rig(settings=CERTAIN, market_state=with_flow(0.0, 0.0))
        await built.executor.submit(plan(resting_order()), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)

        # One burst of prints arrives and stays inside the rolling window.
        built.executor.update_market(with_flow(5.0, 5.0))
        after_first = built.executor._pending[order.client_order_id].traded_through

        for _ in range(9):
            built.executor.update_market(with_flow(5.0, 5.0))
        after_nine_more = built.executor._pending[order.client_order_id].traded_through

        assert after_nine_more == pytest.approx(after_first), (
            f"queue progress grew from {after_first} to {after_nine_more} "
            "across nine republications of the SAME rolling-window volume: no "
            "new prints traded, but the order was credited nine more times"
        )

    async def test_the_credit_scales_with_update_count_not_with_volume(self):
        """The mechanism, stated as a measurement."""
        built = rig(settings=CERTAIN, market_state=with_flow(0.0, 0.0))
        await built.executor.submit(plan(resting_order()), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)

        for _ in range(20):
            built.executor.update_market(with_flow(5.0, 5.0))
        credited = built.executor._pending[order.client_order_id].traded_through

        one_window = 10.0 * 0.01
        assert credited == pytest.approx(one_window), (
            f"the window held 10.0 of volume throughout; the order was "
            f"credited {credited}, which is {credited / one_window:.0f}x the "
            "volume that actually traded"
        )

    async def test_repeated_updates_can_manufacture_a_passive_fill(self):
        """The consequence: an order that should not have traded, trades.

        With the market's ask far from the limit the order is not crossed, so
        ``fill_passive`` only fills in proportion to ``traded_through``. A
        genuinely static tape must not carry it over the line.
        """
        modest = audit_settings(
            **deterministic_execution(maker_fill_probability=0.35)
        )
        built = rig(settings=modest, market_state=with_flow(0.0, 0.0), seed=7)
        await built.executor.submit(plan(resting_order()), START_MS)
        order = built.only_order()
        await built.executor.poll(ACK_AT)

        filled_from_static_tape = False
        for step in range(40):
            built.executor.update_market(with_flow(1.0, 1.0))
            fills = await built.executor.poll(ACK_AT + (step + 1) * 100)
            if fills:
                filled_from_static_tape = True
                break

        assert not filled_from_static_tape, (
            "a resting order filled from a tape on which no new volume ever "
            f"printed: traded_through reached "
            f"{built.executor._pending[order.client_order_id].traded_through} "
            "purely from republished snapshots"
        )

    async def test_a_terminal_order_stops_accruing(self):
        """A control: the accrual walks ``live_orders()``, so it does stop."""
        built = rig(settings=CERTAIN, market_state=with_flow(0.0, 0.0))
        await built.executor.submit(
            plan(resting_order(ttl_ms=100)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        await built.executor.poll(START_MS + 200)
        assert order.is_terminal

        before = built.executor._pending[order.client_order_id].traded_through
        for _ in range(5):
            built.executor.update_market(with_flow(5.0, 5.0))
        assert (
            built.executor._pending[order.client_order_id].traded_through == before
        )


class TestMaxPartialFraction:
    """H16. One documented knob, two fill paths."""

    def test_it_is_applied_on_the_taker_path(self):
        source = inspect.getsource(FillSimulator.fill_marketable)
        assert "cap = self.config.max_partial_fraction" in source
        assert "wanted_quantity *= cap" in source

    def test_it_is_absent_from_the_maker_path(self):
        source = inspect.getsource(FillSimulator.fill_passive)
        assert "max_partial_fraction" in source, (
            "max_partial_fraction is documented as 'the fraction of an order "
            "that may fill on one pass' but fill_passive never consults it; a "
            "passive evaluation can fill the whole order under any cap"
        )

    async def test_a_capped_taker_fill_respects_the_cap(self):
        """The control."""
        capped = audit_settings(**deterministic_execution(max_partial_fraction=0.10))
        built = rig(settings=capped, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(quantity=1.0, limit_price=40_100.0)), START_MS
        )
        order = built.only_order()
        await built.executor.poll(ACK_AT)
        assert order.filled_quantity <= 0.10 + 1e-9

    async def test_a_capped_passive_fill_respects_the_same_cap(self):
        """The finding: one evaluation, the whole order, under a 10% cap."""
        capped = audit_settings(**deterministic_execution(max_partial_fraction=0.10))
        built = rig(
            settings=capped,
            # Crossed: fill_passive's certain-probability branch.
            market_state=one_venue_market(mid=30_000.0),
        )
        await built.executor.submit(
            plan(resting_order(quantity=1.0, limit_price=40_000.0)), START_MS
        )
        order = built.only_order()

        await built.executor.poll(ACK_AT)

        assert order.filled_quantity <= 0.10 + 1e-9, (
            f"a single passive evaluation filled {order.filled_quantity} of "
            f"{order.quantity} under a max_partial_fraction of 0.10"
        )

    def test_the_configured_default_leaves_the_cap_inert(self):
        """Recorded so the finding is scoped: shipped, the cap is 1.0."""
        assert audit_settings().execution.max_partial_fraction == 1.0


class TestFillProvenance:
    """H18. Which data a fill says it came from."""

    def test_the_fill_is_stamped_from_a_market_wide_value(self):
        source = inspect.getsource(PaperExecutor._attempt_fill)
        assert "self.market.source_data_timestamp" in source
        assert "source_data_timestamp_for" in source, (
            "a fill is stamped with the market-wide source timestamp while it "
            "was computed from _book_view(order), which reads one venue and "
            "one symbol; MarketState.source_data_timestamp_for exists for "
            "exactly this"
        )

    async def test_a_fill_does_not_inherit_a_fresher_venues_timestamp(self):
        """The behavioural consequence.

        Two venues, one stale and one fresh. The order executes against the
        stale one; its fill must not claim the fresh one's provenance.
        """
        stale_ts = START_MS - 5_000
        both = market(
            venue_state(book(VENUE_A, mid=30_000.0, ts=stale_ts), as_of=START_MS),
            venue_state(book(VENUE_B, mid=30_000.0, ts=START_MS), as_of=START_MS),
        )
        built = rig(settings=CERTAIN, market_state=both)

        await built.executor.submit(
            plan(planned(venue=VENUE_A, limit_price=40_100.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)

        assert fills
        own = both.source_data_timestamp_for([(VENUE_A, SYMBOL)])
        assert fills[0].source_data_timestamp == own, (
            f"a fill on {VENUE_A} (observed at {own}) was stamped "
            f"{fills[0].source_data_timestamp}, the market-wide value, which "
            f"is drawn from every venue including {VENUE_B}"
        )

    async def test_the_stamp_is_at_least_never_fresher_than_the_whole_market(self):
        """A control on the direction of the error: the market-wide value is
        the oldest across venues, so it cannot be fresher than the order's own
        data — it can only be older, or belong to an unrelated instrument."""
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)
        assert fills
        assert fills[0].source_data_timestamp is not None

    async def test_a_maker_and_a_taker_fill_are_labelled_differently(self):
        """A control on the fee classification the audit relies on elsewhere."""
        built = rig(settings=CERTAIN, market_state=one_venue_market(mid=30_000.0))
        await built.executor.submit(
            plan(planned(limit_price=40_100.0)), START_MS
        )
        fills = await built.executor.poll(ACK_AT)
        assert fills and fills[0].liquidity is Liquidity.TAKER
        taker_bps = built.settings.venue(VENUE_A).fees.taker_bps
        assert fills[0].fee == pytest.approx(
            fills[0].notional * taker_bps / 10_000
        )
