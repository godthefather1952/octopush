"""TIDAL-L2: the reason a book was invalidated is observable, not discarded.

``LocalOrderBook.invalidate(reason)`` accepted a ``reason`` argument and threw
it away -- every invalidation looked identical from the outside regardless of
whether it was a disconnect, a sequence gap, or a storage overflow. This adds
a narrow, bounded diagnostic field (``invalid_reason``) set by every
invalidation path and cleared the moment a fresh snapshot re-establishes
trust. It is diagnostic only: nothing in the trading path reads it.
"""

from __future__ import annotations

import contextlib

from agents.tidal.book import BookDesyncError, BookOverflowError, LocalOrderBook
from core.models.market import PriceLevel
from tests.conftest import START_MS, make_book
from venues.base.messages import BookDelta


def range_delta(*, first, final, bids=(), asks=(), venue="V", symbol="BTC-USD", ts=START_MS):
    return BookDelta(
        venue=venue, symbol=symbol, exchange_ts=ts, received_ts=ts,
        first_sequence=first, sequence=final,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


class TestDisconnectedReasonRetained:
    def test_invalidate_stores_the_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        assert book.invalid_reason is None
        book.invalidate("venue disconnected")
        assert book.invalid_reason == "venue disconnected"
        assert not book.synced

    def test_a_missing_reason_stores_none_not_an_empty_string(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        book.invalidate()
        assert book.invalid_reason is None


class TestDesyncReasonRetained:
    def test_a_sequence_gap_leaves_a_visible_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2)
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        with contextlib.suppress(BookDesyncError):
            book.apply_delta(range_delta(first=book.sequence + 5, final=book.sequence + 6))
        assert book.invalid_reason is not None
        assert "sequence gap" in book.invalid_reason


class TestOverflowReasonRetained:
    def test_a_delta_overflow_leaves_a_visible_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2, max_levels_per_side=3)
        book.apply_snapshot(
            make_book("V", "BTC-USD", 100.0, levels=3, tick=1.0)
        )
        with contextlib.suppress(BookOverflowError):
            book.apply_delta(range_delta(first=book.sequence + 1, final=book.sequence + 1, bids=[(50.0, 1.0)]))
        assert book.invalid_reason is not None
        assert "max_levels_per_side" in book.invalid_reason

    def test_a_snapshot_overflow_leaves_a_visible_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_levels_per_side=3)
        big = make_book("V", "BTC-USD", 100.0, levels=10, tick=1.0)
        with contextlib.suppress(BookOverflowError):
            book.apply_snapshot(big)
        assert book.invalid_reason is not None
        assert "max_levels_per_side" in book.invalid_reason


class TestSuccessfulCheckpointClearsStaleReason:
    def test_a_fresh_snapshot_after_a_disconnect_clears_the_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        book.invalidate("venue disconnected")
        assert book.invalid_reason == "venue disconnected"

        book.apply_snapshot(make_book("V", "BTC-USD", 101.0))
        assert book.invalid_reason is None
        assert book.synced

    def test_a_fresh_snapshot_after_a_sequence_gap_clears_the_reason(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=2)
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        with contextlib.suppress(BookDesyncError):
            book.apply_delta(range_delta(first=book.sequence + 5, final=book.sequence + 6))
        assert book.invalid_reason is not None

        book.apply_snapshot(make_book("V", "BTC-USD", 102.0, tick=1.0))
        assert book.invalid_reason is None


class TestReasonStringIsBounded:
    def test_an_extremely_long_reason_is_truncated_not_stored_raw(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD")
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0))
        huge = "x" * 100_000
        book.invalidate(huge)
        assert book.invalid_reason is not None
        assert len(book.invalid_reason) <= 200
