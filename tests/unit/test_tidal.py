"""TIDAL: book maintenance, sequence handling and microstructure metrics."""

from __future__ import annotations

import pytest

from agents.tidal.book import BookDesyncError, LocalOrderBook
from agents.tidal.metrics import (
    MidWindow,
    TradeFlowWindow,
    compute_metrics,
    depth_within_bps,
    microprice,
)
from core.models.common import Side
from core.models.market import PriceLevel
from tests.conftest import START_MS, make_book
from venues.base.messages import BookDelta


def delta(venue="VENUE_A", symbol="BTC-USD", *, seq, prev, bids=(), asks=(), ts=START_MS):
    return BookDelta(
        venue=venue,
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        sequence=seq,
        prev_sequence=prev,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


@pytest.fixture
def book() -> LocalOrderBook:
    book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USD", max_depth=10)
    book.apply_snapshot(make_book("VENUE_A", "BTC-USD", 100.0, levels=5, tick=1.0, size=1.0))
    return book


class TestBookMaintenance:
    def test_snapshot_makes_the_book_usable(self, book):
        assert book.synced and book.usable
        assert book.best_bid == pytest.approx(99.99)
        assert book.best_ask == pytest.approx(100.01)

    def test_delta_updates_a_level(self, book):
        book.sequence = 1
        book.apply_delta(delta(seq=2, prev=1, bids=[(99.99, 5.0)]))
        assert book.bids[99.99] == pytest.approx(5.0)
        assert book.sequence == 2

    def test_zero_size_removes_a_level(self, book):
        book.apply_delta(delta(seq=2, prev=1, bids=[(99.99, 0.0)]))
        assert 99.99 not in book.bids

    def test_sequence_gap_desyncs_the_book(self, book):
        # Named for what it checks. It used to be called
        # "..._and_requests_resync" while asserting only that a flag was set —
        # and no resync was ever requested, which is how TIDAL-C2 survived. The
        # recovery this name promised is now asserted end to end in
        # tests/unit/test_resync_flow.py.
        #
        # The update expects to sit on sequence 5, but we hold 1: applying it
        # would silently corrupt the book.
        with pytest.raises(BookDesyncError):
            book.apply_delta(delta(seq=6, prev=5, bids=[(99.99, 5.0)]))
        assert not book.synced
        assert book.needs_resync
        assert book.sequence_gaps == 1
        assert not book.usable

    def test_stale_update_is_dropped_not_applied(self, book):
        book.apply_delta(delta(seq=2, prev=1, bids=[(99.99, 5.0)]))
        book.apply_delta(delta(seq=2, prev=1, bids=[(99.99, 9.0)]))
        assert book.bids[99.99] == pytest.approx(5.0)

    def test_out_of_order_update_is_dropped(self, book):
        book.apply_delta(delta(seq=3, prev=1, bids=[(99.99, 5.0)]))
        book.apply_delta(delta(seq=2, prev=1, bids=[(99.99, 9.0)]))
        assert book.bids[99.99] == pytest.approx(5.0)
        assert book.sequence == 3

    def test_delta_before_snapshot_is_rejected(self):
        fresh = LocalOrderBook(venue="V", symbol="BTC-USD")
        with pytest.raises(BookDesyncError):
            fresh.apply_delta(delta(seq=1, prev=0, bids=[(100.0, 1.0)]))

    def test_resync_via_snapshot_clears_the_gap(self, book):
        with pytest.raises(BookDesyncError):
            book.apply_delta(delta(seq=6, prev=5))
        book.apply_snapshot(make_book("VENUE_A", "BTC-USD", 100.0, levels=5, tick=1.0))
        assert book.synced and not book.needs_resync and book.usable

    def test_depth_is_trimmed(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=3)
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=10, tick=1.0))
        assert len(book.bids) == 3
        assert len(book.asks) == 3
        # The levels kept are the ones nearest the touch.
        assert max(book.bids) == pytest.approx(99.99)

    def test_crossed_book_is_not_usable(self, book):
        book.asks = {99.0: 1.0}
        assert book.crossed
        assert not book.usable

    def test_invalidate_marks_unusable(self, book):
        book.invalidate("disconnected")
        assert not book.usable and book.needs_resync


class TestMetrics:
    def test_microprice_leans_towards_the_thin_side(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(
            make_book("V", "BTC-USD", 100.0, levels=1, tick=1.0, size=1.0)
        )
        # Equal sizes: microprice sits at the mid.
        assert microprice(book) == pytest.approx(100.0)
        # A thin ask means the ask is likelier to be consumed, so the
        # microprice leans up towards it.
        book.asks = {100.01: 0.1}
        book.bids = {99.99: 10.0}
        assert microprice(book) > 100.0

    def test_depth_within_bps_stops_at_the_limit(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=8, tick=1.0, size=1.0))
        near = depth_within_bps(book, Side.BUY, 100.0, 20.0)
        far = depth_within_bps(book, Side.BUY, 100.0, 1_000.0)
        assert 0 < near < far

    def test_imbalance_sign_follows_the_heavier_side(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=3, tick=1.0, size=1.0))
        book.bids = {99.99: 10.0}
        book.asks = {100.01: 1.0}
        metrics = compute_metrics(book, START_MS)
        assert metrics.imbalance > 0

    def test_metrics_are_empty_when_a_side_is_missing(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=3, tick=1.0))
        book.asks = {}
        metrics = compute_metrics(book, START_MS)
        assert metrics.mid is None and metrics.spread_bps is None

    def test_trade_flow_window_evicts_old_prints(self):
        window = TradeFlowWindow(window_ms=1_000)
        window.add(START_MS, Side.BUY, 100.0)
        window.add(START_MS + 500, Side.SELL, 50.0)
        assert window.volumes(START_MS + 600) == (100.0, 50.0)
        # At +1100 the cutoff is +100, so the buy print falls out of the
        # window while the sell at +500 is still inside it.
        assert window.volumes(START_MS + 1_100) == (0.0, 50.0)
        assert window.volumes(START_MS + 1_600) == (0.0, 0.0)

    def test_volatility_needs_several_points(self):
        window = MidWindow(window_ms=10_000)
        assert window.volatility_bps(START_MS) == 0.0
        for i in range(5):
            window.add(START_MS + i * 100, 100.0 + i * 0.1)
        assert window.volatility_bps(START_MS + 500) > 0

    def test_flat_prices_have_zero_volatility(self):
        window = MidWindow()
        for i in range(6):
            window.add(START_MS + i * 100, 100.0)
        assert window.volatility_bps(START_MS + 600) == pytest.approx(0.0)
