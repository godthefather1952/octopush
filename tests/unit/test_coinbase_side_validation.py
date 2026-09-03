"""Coinbase ``l2update`` side validation: TIDAL-M2.

The old code was, in effect, ``bids if side == "buy" else asks`` — anything
that wasn't literally ``"buy"`` silently became an ask. Coinbase's public
feed only ever sends ``"buy"`` or ``"sell"``; a different value means this
message is not what it claims to be, and defaulting it onto a side is the one
error a book can least afford — it can manufacture a crossed book or hide
real liquidity on the wrong side, rather than merely losing a level.
"""

from __future__ import annotations

import pytest

from venues.base.messages import MalformedVenueMessage
from venues.venue_b import parser as parser_b

START_MS = 1_788_000_000_000


def update(side, price="100.0", size="1.0"):
    return {
        "type": "l2update",
        "product_id": "BTC-USD",
        "time": "2024-01-01T00:00:00Z",
        "changes": [[side, price, size]],
    }


class TestValidSides:
    def test_buy_goes_to_bids(self):
        delta = parser_b.parse_l2update(update("buy"), START_MS)
        assert len(delta.bids) == 1 and not delta.asks
        assert delta.bids[0].price == 100.0

    def test_sell_goes_to_asks(self):
        delta = parser_b.parse_l2update(update("sell"), START_MS)
        assert len(delta.asks) == 1 and not delta.bids
        assert delta.asks[0].price == 100.0


class TestInvalidSidesFailClosed:
    @pytest.mark.parametrize(
        "bad_side",
        [
            "unknown",
            "",
            None,
            "Buy",       # wrong capitalization — the real protocol is lowercase-only
            "BUY",
            "Sell",
            "bid",       # a plausible-looking but wrong value
            "ask",
            0,
            1,
        ],
    )
    def test_an_invalid_side_raises_and_creates_no_level(self, bad_side):
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_l2update(update(bad_side), START_MS)

    def test_an_invalid_side_never_lands_on_either_book_side(self):
        """Direct proof it isn't quietly defaulting to ask (or bid)."""
        try:
            parser_b.parse_l2update(update("unknown"), START_MS)
        except MalformedVenueMessage:
            pass
        else:
            pytest.fail("expected MalformedVenueMessage")
        # There is no partial result to inspect — the function raised before
        # returning anything, so nothing could have been misfiled.

    def test_the_exception_identifies_the_bad_value(self):
        with pytest.raises(MalformedVenueMessage, match="unknown"):
            parser_b.parse_l2update(update("unknown"), START_MS)

    def test_the_exception_carries_symbol_and_message_type(self):
        with pytest.raises(MalformedVenueMessage) as excinfo:
            parser_b.parse_l2update(update("bogus"), START_MS)
        assert excinfo.value.symbol == "BTC-USD"
        assert excinfo.value.message_type == "l2update"
        assert excinfo.value.venue == "VENUE_B"

    def test_one_bad_side_invalidates_the_whole_batch(self):
        """A batch with a good change and a bad one must not partially apply —
        see the identical rule for numeric validation (TIDAL-M3/M4): a
        message is one atomic statement about the book.
        """
        payload = {
            "type": "l2update", "product_id": "BTC-USD", "time": "2024-01-01T00:00:00Z",
            "changes": [["buy", "100.0", "1.0"], ["bogus", "99.0", "1.0"]],
        }
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_l2update(payload, START_MS)


class TestParseMessageDispatchIsUnaffected:
    def test_a_valid_l2update_still_reaches_the_book(self):
        parsed = parser_b.parse_message(update("buy"), START_MS)
        assert parsed is not None and parsed.bids[0].price == 100.0

    def test_an_invalid_side_propagates_through_parse_message_too(self):
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_message(update("bogus"), START_MS)
