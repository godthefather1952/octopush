"""Numeric validation across the market-data models: TIDAL-M3.

``float("NaN")``, ``float("Infinity")`` and ``float("-Infinity")`` all
succeed in Python — a venue that sends these as price/quantity strings (which
is exactly how Binance and Coinbase both send numbers, as JSON strings) would
previously have produced a ``PriceLevel``/``TradeEvent`` with a non-finite
value that every downstream sum, comparison and bps calculation was trusted
to handle correctly. It wasn't verified that they did; the rule now is to
never let one exist in the first place.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta, MalformedVenueMessage
from venues.venue_a import parser as parser_a
from venues.venue_b import parser as parser_b

START_MS = 1_788_000_000_000


class TestPriceLevelDirectConstruction:
    @pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
    def test_invalid_prices_are_rejected(self, bad_price):
        with pytest.raises(ValidationError):
            PriceLevel(price=bad_price, size=1.0)

    @pytest.mark.parametrize("bad_size", [float("nan"), float("inf"), float("-inf"), -1.0])
    def test_invalid_sizes_are_rejected(self, bad_size):
        with pytest.raises(ValidationError):
            PriceLevel(price=100.0, size=bad_size)

    def test_zero_size_remains_valid_as_a_deletion(self):
        level = PriceLevel(price=100.0, size=0.0)
        assert level.size == 0.0

    def test_a_finite_positive_price_and_size_are_valid(self):
        level = PriceLevel(price=100.5, size=2.25)
        assert level.price == 100.5 and level.size == 2.25


class TestTradeEventDirectConstruction:
    def _trade(self, **overrides):
        defaults = dict(
            venue="VENUE_A", symbol="BTC-USDT", exchange_ts=START_MS, received_ts=START_MS,
            price=100.0, size=1.0, aggressor="BUY",
        )
        defaults.update(overrides)
        return TradeEvent(**defaults)

    @pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), float("-inf"), 0.0])
    def test_invalid_price_is_rejected(self, bad_price):
        with pytest.raises(ValidationError):
            self._trade(price=bad_price)

    @pytest.mark.parametrize("bad_size", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
    def test_invalid_size_is_rejected(self, bad_size):
        # A trade of size 0 is not a deletion semantic like a book level —
        # it isn't a trade at all, so zero stays invalid here.
        with pytest.raises(ValidationError):
            self._trade(size=bad_size)


class TestBinanceParserRejectsNonFiniteValues:
    def _depth(self, bids=None, asks=None):
        return {
            "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT", "U": 1, "u": 2,
            "b": bids or [], "a": asks or [],
        }

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_bad_price_string_is_rejected(self, bad):
        with pytest.raises(MalformedVenueMessage):
            parser_a.parse_depth_update(self._depth(bids=[[bad, "1.0"]]), START_MS)

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_bad_quantity_string_is_rejected(self, bad):
        with pytest.raises(MalformedVenueMessage):
            parser_a.parse_depth_update(self._depth(bids=[["100.0", bad]]), START_MS)

    def test_a_bad_level_invalidates_the_whole_message_not_just_itself(self):
        """One malformed level must not let the rest of a good batch through
        looking complete — this message claimed several changes happened
        together, and applying only the parseable ones misrepresents the book
        the venue actually described (TIDAL-M3/M4).
        """
        with pytest.raises(MalformedVenueMessage):
            parser_a.parse_depth_update(
                self._depth(bids=[["100.0", "1.0"], ["NaN", "2.0"], ["99.0", "3.0"]]),
                START_MS,
            )

    def test_the_exception_carries_symbol_and_message_type(self):
        with pytest.raises(MalformedVenueMessage) as excinfo:
            parser_a.parse_depth_update(self._depth(bids=[["NaN", "1.0"]]), START_MS)
        assert excinfo.value.symbol == "BTC-USDT"
        assert excinfo.value.message_type == "depthUpdate"
        assert excinfo.value.venue == "VENUE_A"

    def test_infinite_json_tokens_are_also_rejected(self):
        """Python's json accepts bare NaN/Infinity tokens (not just strings);
        a payload using them must be rejected the same way a string is.
        """
        with pytest.raises(MalformedVenueMessage):
            parser_a.parse_depth_update(
                self._depth(bids=[[float("inf"), "1.0"]]), START_MS
            )

    def test_a_valid_message_is_unaffected(self):
        delta = parser_a.parse_depth_update(
            self._depth(bids=[["100.0", "1.0"]], asks=[["101.0", "2.0"]]), START_MS
        )
        assert delta.bids[0].price == 100.0


class TestCoinbaseParserRejectsNonFiniteValues:
    def _update(self, changes):
        return {
            "type": "l2update", "product_id": "BTC-USD",
            "time": "2024-01-01T00:00:00Z", "changes": changes,
        }

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_bad_price_is_rejected(self, bad):
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_l2update(self._update([["buy", bad, "1.0"]]), START_MS)

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_bad_quantity_is_rejected(self, bad):
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_l2update(self._update([["buy", "100.0", bad]]), START_MS)

    def test_snapshot_levels_are_validated_too(self):
        with pytest.raises(MalformedVenueMessage):
            parser_b.parse_snapshot(
                {
                    "type": "snapshot", "product_id": "BTC-USD",
                    "bids": [["NaN", "1.0"]], "asks": [],
                },
                START_MS,
            )

    def test_a_valid_message_is_unaffected(self):
        delta = parser_b.parse_l2update(self._update([["buy", "100.0", "1.0"]]), START_MS)
        assert delta.bids[0].price == 100.0


class TestZeroQuantityDeletionSemanticsSurvive:
    """The fix must not collateral-damage the one legitimate zero: a deletion."""

    def test_binance_zero_quantity_is_a_valid_deletion(self):
        delta = parser_a.parse_depth_update(
            {
                "e": "depthUpdate", "E": START_MS, "s": "BTCUSDT", "U": 1, "u": 2,
                "b": [["100.0", "0"]], "a": [],
            },
            START_MS,
        )
        assert delta.bids[0].size == 0.0

    def test_coinbase_zero_quantity_is_a_valid_deletion(self):
        delta = parser_b.parse_l2update(
            {
                "type": "l2update", "product_id": "BTC-USD", "time": "2024-01-01T00:00:00Z",
                "changes": [["buy", "100.0", "0"]],
            },
            START_MS,
        )
        assert delta.bids[0].size == 0.0


class TestBookDeltaCarriesTheSameGuarantee:
    def test_constructing_a_bookdelta_with_a_nan_level_fails(self):
        with pytest.raises(ValidationError):
            BookDelta(
                venue="VENUE_A", symbol="BTC-USDT", exchange_ts=START_MS,
                received_ts=START_MS, first_sequence=1, sequence=2,
                bids=[PriceLevel(price=float("nan"), size=1.0)],
            )


class TestOrderBookSnapshotCarriesTheSameGuarantee:
    def test_constructing_a_snapshot_with_an_infinite_level_fails(self):
        with pytest.raises(ValidationError):
            OrderBookSnapshot(
                venue="VENUE_A", symbol="BTC-USDT", exchange_ts=START_MS,
                received_ts=START_MS, sequence=1,
                bids=[PriceLevel(price=float("inf"), size=1.0)], asks=[],
            )


class TestMathAssumptions:
    def test_downstream_notional_math_never_sees_a_non_finite_level(self):
        """"Do not rely on downstream math to fail" as a direct proof: the
        normal constructor is the only way ``PriceLevel`` is ever built from
        parsed data, and it refuses every non-finite input before ``.notional``
        (or any other arithmetic) could ever run on one.
        """
        for bad_price in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError):
                PriceLevel(price=bad_price, size=1.0)
        # Bypassing validation entirely is the only way to get a level whose
        # arithmetic is undefined — confirming what the constructor exists
        # to prevent, not something the constructor itself would ever produce.
        unchecked = PriceLevel.model_construct(price=float("inf"), size=1.0)
        assert math.isinf(unchecked.notional)
