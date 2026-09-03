"""LocalOrderBook storage: TIDAL-M1.

The book used to trim itself to ``max_depth`` after every delta. A level
sitting just past the configured depth was discarded even though the venue
never told anyone it was gone — and when the levels ahead of it later
disappeared (an ordinary sequence of trades and cancels, not an error), that
level should have surfaced into the top-N with its real, already-known size.
Instead it was gone, sometimes permanently, until a fresh snapshot happened
to reintroduce it.

The fix separates two questions that trimming on write had conflated:
storage keeps the *full* book; only a read (``levels()``, and through it
``snapshot()`` and every metric) trims to ``max_depth``.
"""

from __future__ import annotations

from agents.tidal.book import LocalOrderBook
from core.models.common import Side
from core.models.market import PriceLevel
from tests.conftest import START_MS, make_book
from venues.base.messages import BookDelta


def range_delta(*, first, final, bids=(), asks=(), venue="VENUE_A", symbol="BTC-USDT"):
    return BookDelta(
        venue=venue, symbol=symbol, exchange_ts=START_MS, received_ts=START_MS,
        first_sequence=first, sequence=final,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


def cb_delta(*, exchange_ts, bids=(), asks=(), venue="VENUE_B", symbol="BTC-USD"):
    return BookDelta(
        venue=venue, symbol=symbol, exchange_ts=exchange_ts, received_ts=exchange_ts,
        sequence=None, first_sequence=None,
        bids=[PriceLevel(price=p, size=s) for p, s in bids],
        asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


class TestStorageIsUnbounded:
    def test_a_snapshot_deeper_than_max_depth_is_kept_in_full(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=3)
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=10, tick=1.0))
        assert len(book.bids) == 10
        assert len(book.asks) == 10

    def test_deltas_deeper_than_max_depth_are_still_stored(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=3)
        book.apply_snapshot(make_book("VENUE_A", "BTC-USDT", 100.0, levels=3, tick=1.0))
        # Insert ten levels well outside the configured top-3.
        book.apply_delta(
            range_delta(
                first=2, final=2,
                bids=[(100.0 - 10 - i, 1.0) for i in range(10)],
                asks=[(100.0 + 10 + i, 1.0) for i in range(10)],
            )
        )
        assert len(book.bids) == 13
        assert len(book.asks) == 13


class TestABuriedLevelResurfacesExactly:
    """The core defect: a level outside the top-N must reappear, with its
    correct original size, once the levels ahead of it are deleted — without
    needing a fresh update to the level itself.
    """

    def test_deleting_better_levels_resurfaces_a_buried_one(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=3)
        book.apply_snapshot(
            make_book("VENUE_A", "BTC-USDT", 100.0, levels=5, tick=1.0, size=1.0)
        )
        # Bury a level well past the top-3, with a distinctive size.
        book.apply_delta(range_delta(first=2, final=2, bids=[(90.0, 7.5)]))
        assert 90.0 not in [lvl.price for lvl in book.levels(Side.BUY)], (
            "sanity: it starts outside the default top-3 read"
        )
        assert book.bids[90.0] == 7.5, "but storage must already hold it"

        # Delete every level ahead of it, one at a time — no update ever
        # touches 90.0 itself.
        remaining = sorted(book.bids, reverse=True)
        for price in remaining:
            if price == 90.0:
                continue
            book.apply_delta(
                range_delta(first=book.sequence + 1, final=book.sequence + 1, bids=[(price, 0.0)])
            )

        assert book.bids == {90.0: 7.5}
        top = book.levels(Side.BUY)
        assert len(top) == 1
        assert top[0].price == 90.0
        assert top[0].size == 7.5, "the resurfaced level's size must be exactly what was sent"

    def test_repeated_boundary_crossing_stays_exact(self):
        """Insert/delete across the top-N line, back and forth, many times."""
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=3)
        book.apply_snapshot(
            make_book("VENUE_A", "BTC-USDT", 100.0, levels=1, tick=1.0, size=1.0)
        )
        seq = book.sequence
        # A level at 90.0 with a size that changes each time it crosses.
        for i in range(1, 21):
            seq += 1
            size = float(i)
            book.apply_delta(range_delta(first=seq, final=seq, bids=[(90.0, size)]))
            assert book.bids[90.0] == size, "storage must reflect the latest write immediately"
            if i % 2 == 0:
                # Bring it into the visible top-3 by clearing what's ahead of it.
                seq += 1
                ahead = [p for p in book.bids if p > 90.0]
                book.apply_delta(
                    range_delta(first=seq, final=seq, bids=[(p, 0.0) for p in ahead])
                )
                top = book.levels(Side.BUY)
                assert top[0].price == 90.0 and top[0].size == size
                # Restore a better level so 90.0 goes back outside the top-3.
                seq += 1
                book.apply_delta(range_delta(first=seq, final=seq, bids=[(99.0, 1.0), (98.0, 1.0)]))
        assert book.bids[90.0] == 20.0

    def test_asks_side_resurfaces_the_same_way(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=2)
        book.apply_snapshot(
            make_book("VENUE_A", "BTC-USDT", 100.0, levels=5, tick=1.0, size=1.0)
        )
        book.apply_delta(range_delta(first=2, final=2, asks=[(110.0, 3.25)]))
        assert 110.0 not in [lvl.price for lvl in book.levels(Side.SELL)]
        for price in [p for p in list(book.asks) if p != 110.0]:
            book.apply_delta(
                range_delta(first=book.sequence + 1, final=book.sequence + 1, asks=[(price, 0.0)])
            )
        top = book.levels(Side.SELL)
        assert top and top[0].price == 110.0 and top[0].size == 3.25


class TestSnapshotResetStillReplacesEverything:
    def test_a_fresh_snapshot_discards_the_full_previous_book(self):
        book = LocalOrderBook(venue="V", symbol="BTC-USD", max_depth=5)
        book.apply_snapshot(make_book("V", "BTC-USD", 100.0, levels=20, tick=0.5))
        assert len(book.bids) == 20
        book.apply_snapshot(make_book("V", "BTC-USD", 200.0, levels=3, tick=0.5))
        assert len(book.bids) == 3, "a snapshot is a full replace, not a merge"
        assert all(price > 150 for price in book.bids)


class TestBothVenueShapesPreserveTheBehavior:
    """M1 fixed LocalOrderBook itself, so both the range-sequenced (Binance)
    and unordered (Coinbase) delta paths inherit it identically.
    """

    def test_binance_shaped_deltas_preserve_buried_levels(self):
        book = LocalOrderBook(venue="VENUE_A", symbol="BTC-USDT", max_depth=2)
        book.apply_snapshot(
            make_book("VENUE_A", "BTC-USDT", 100.0, levels=5, tick=1.0, size=1.0)
        )
        book.apply_delta(range_delta(first=2, final=2, bids=[(80.0, 4.0)]))
        assert 80.0 not in [lvl.price for lvl in book.levels(Side.BUY)], "starts buried"
        assert book.bids[80.0] == 4.0
        for price in [p for p in list(book.bids) if p != 80.0]:
            book.apply_delta(
                range_delta(first=book.sequence + 1, final=book.sequence + 1, bids=[(price, 0.0)])
            )
        assert book.levels(Side.BUY)[0].price == 80.0

    def test_coinbase_shaped_deltas_preserve_buried_levels(self):
        book = LocalOrderBook(venue="VENUE_B", symbol="BTC-USD", max_depth=2)
        book.apply_snapshot(
            make_book("VENUE_B", "BTC-USD", 100.0, levels=5, tick=1.0, size=1.0)
        )
        book.apply_delta(cb_delta(exchange_ts=START_MS + 10, bids=[(80.0, 4.0)]))
        assert 80.0 not in [lvl.price for lvl in book.levels(Side.BUY)], "starts buried"
        assert book.bids[80.0] == 4.0
        for i, price in enumerate([p for p in list(book.bids) if p != 80.0]):
            book.apply_delta(
                cb_delta(exchange_ts=START_MS + 20 + i, bids=[(price, 0.0)])
            )
        assert book.levels(Side.BUY)[0].price == 80.0
        assert book.levels(Side.BUY)[0].size == 4.0
