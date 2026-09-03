"""Coinbase ``time`` field parsing: TIDAL-M8.

``parse_iso_ms`` used to assume its argument was a string strongly enough
that a non-string value (an int, a float, a dict) reached ``str.replace``
directly and raised an uncaught ``AttributeError`` — which, before TIDAL-M4,
would have taken the whole venue session down over one bad timestamp.

Coinbase documents ``time`` as an ISO-8601 string and nothing else. The fix
does not try to be clever about what a bare number *might* mean (Unix
seconds? milliseconds? something else?) — it parses ISO-8601 when the value
is a string that is one, and falls back to local receipt time for
everything else, exactly as it already did for a missing field. No guessing.
"""

from __future__ import annotations

import pytest

from venues.venue_b.parser import parse_iso_ms

FALLBACK = 1_788_000_000_000


class TestValidStrings:
    def test_a_z_suffixed_timestamp_parses(self):
        assert parse_iso_ms("2024-01-01T00:00:00.500Z", FALLBACK) == 1_704_067_200_500

    def test_an_explicit_offset_timestamp_parses(self):
        # +00:00 is the same instant as the Z-suffixed case above.
        assert parse_iso_ms("2024-01-01T00:00:00.500+00:00", FALLBACK) == 1_704_067_200_500

    def test_a_non_utc_offset_converts_correctly(self):
        # 01:00 at +01:00 is the same instant as 00:00Z.
        assert parse_iso_ms("2024-01-01T01:00:00+01:00", FALLBACK) == 1_704_067_200_000

    def test_no_fractional_seconds_still_parses(self):
        assert parse_iso_ms("2024-01-01T00:00:00Z", FALLBACK) == 1_704_067_200_000


class TestFallsBackWithoutGuessing:
    """Every one of these used to be a real risk of crashing the caller
    (the dict/list/int/float cases) or already fell back correctly (None,
    malformed string) — all must now uniformly fall back, never raise.
    """

    def test_none_falls_back(self):
        assert parse_iso_ms(None, FALLBACK) == FALLBACK

    def test_empty_string_falls_back(self):
        assert parse_iso_ms("", FALLBACK) == FALLBACK

    def test_malformed_string_falls_back(self):
        assert parse_iso_ms("not-a-timestamp", FALLBACK) == FALLBACK

    def test_a_bare_integer_falls_back_rather_than_being_guessed_as_seconds_or_ms(self):
        """1704067200 looks like Unix seconds and 1704067200000 looks like
        Unix milliseconds — but Coinbase documents neither for this field, so
        neither guess is made. It falls back exactly like any other
        unparseable value.
        """
        assert parse_iso_ms(1_704_067_200, FALLBACK) == FALLBACK
        assert parse_iso_ms(1_704_067_200_000, FALLBACK) == FALLBACK

    def test_a_float_falls_back(self):
        assert parse_iso_ms(1_704_067_200.5, FALLBACK) == FALLBACK

    def test_a_dict_falls_back_instead_of_raising(self):
        """This used to be the uncaught AttributeError: str.replace is not
        defined on a dict, and nothing caught that before it reached the
        caller.
        """
        assert parse_iso_ms({"seconds": 1704067200}, FALLBACK) == FALLBACK

    def test_a_list_falls_back_instead_of_raising(self):
        assert parse_iso_ms([2024, 1, 1], FALLBACK) == FALLBACK

    def test_a_bool_falls_back(self):
        # bool is a subclass of int in Python; must not be treated as a string.
        assert parse_iso_ms(True, FALLBACK) == FALLBACK


class TestNoExceptionEverEscapes:
    @pytest.mark.parametrize(
        "value",
        [None, "", "garbage", 123, 123.456, {}, [], True, object()],
    )
    def test_nothing_raises(self, value):
        # The whole point: every one of these is a value real, buggy, or
        # malicious input could plausibly send, and none may propagate.
        parse_iso_ms(value, FALLBACK)


class TestIntegrationWithTheParsers:
    """The parsers that call parse_iso_ms must inherit the same safety."""

    def test_l2update_with_a_numeric_time_falls_back_rather_than_crashing(self):
        from venues.venue_b.parser import parse_l2update

        delta = parse_l2update(
            {
                "type": "l2update", "product_id": "BTC-USD", "time": 1_704_067_200,
                "changes": [["buy", "100.0", "1.0"]],
            },
            FALLBACK,
        )
        assert delta.exchange_ts == FALLBACK

    def test_match_with_a_dict_time_falls_back_rather_than_crashing(self):
        from venues.venue_b.parser import parse_match

        trade = parse_match(
            {
                "type": "match", "product_id": "BTC-USD", "time": {"bad": "shape"},
                "price": "100.0", "size": "1.0", "side": "buy",
            },
            FALLBACK,
        )
        assert trade.exchange_ts == FALLBACK

    def test_snapshot_with_no_time_field_falls_back(self):
        """Real Coinbase snapshots have no time field at all — this is the
        normal case, not an edge case.
        """
        from venues.venue_b.parser import parse_snapshot

        snapshot = parse_snapshot(
            {"type": "snapshot", "product_id": "BTC-USD", "bids": [], "asks": []},
            FALLBACK,
        )
        assert snapshot.exchange_ts == FALLBACK
