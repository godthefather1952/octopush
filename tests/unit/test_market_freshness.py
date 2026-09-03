"""Exchange-time freshness and clock skew: TIDAL-H3.

Before this fix, ``DataQuality`` was computed from a single number:
``now - received_ts`` — how recently *this process* got a packet. That
answers nothing about how old the observation inside the packet actually was.
A feed delivering packets on schedule that describe a five-second-old market
looked FRESH, because nothing ever looked at ``exchange_ts`` for freshness.

Three timestamps, one meaning each, all milliseconds:

* ``exchange_ts`` — when the exchange observed/produced the data.
* ``received_ts`` / ``last_update_ts`` — when this process received it.
* ``as_of`` — when TIDAL assembled the state being read.

FRESH now requires both the local-silence age (``as_of - last_update_ts``)
and the exchange-observation age (``as_of - exchange_ts``) to be within
``risk.max_data_age_ms``, and requires the two source timestamps to be in a
plausible relationship with each other at all (``risk.max_clock_skew_ms``).
"""

from __future__ import annotations

import pytest

from agents.tidal import Tidal
from core.health import HealthRegistry
from core.models.common import DataQuality
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS
from venues.base.messages import BookDelta
from venues.venue_a import parser as parser_a
from venues.venue_b import parser as parser_b

VENUE = "VENUE_A"


def snap(exchange_ts: int, received_ts: int, *, bid=100.0, ask=101.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        venue=VENUE, symbol="BTC-USDT", exchange_ts=exchange_ts, received_ts=received_ts,
        sequence=1000,
        bids=[PriceLevel(price=bid, size=1.0)], asks=[PriceLevel(price=ask, size=1.0)],
        is_checkpoint=True,
    )


def delta(exchange_ts: int, received_ts: int, *, first=1001, final=1002) -> BookDelta:
    return BookDelta(
        venue=VENUE, symbol="BTC-USDT", exchange_ts=exchange_ts, received_ts=received_ts,
        first_sequence=first, sequence=final,
    )


@pytest.fixture
def tidal(bus, clock, settings):
    return Tidal(bus, clock, settings, HealthRegistry(clock=clock))


class TestFreshRequiresBothAges:
    async def test_current_receive_and_exchange_time_is_fresh(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.FRESH

    async def test_current_receipt_but_a_five_second_old_observation_is_not_fresh(
        self, tidal, clock
    ):
        """The exact defect: packets arriving on time, describing stale data."""
        old_observation = clock.now_ms() - 8_000
        await tidal.on_snapshot(snap(old_observation, clock.now_ms()))
        state = tidal.venue_state(VENUE, "BTC-USDT")
        assert state.quality is not DataQuality.FRESH
        assert state.quality is DataQuality.STALE  # 8000ms > 3x the 2000ms default limit

    async def test_a_moderately_old_observation_degrades_before_it_goes_stale(
        self, tidal, clock
    ):
        moderately_old = clock.now_ms() - 3_000  # > limit(2000), <= 3x limit(6000)
        await tidal.on_snapshot(snap(moderately_old, clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.DEGRADED

    async def test_no_recent_packet_is_not_fresh_even_with_a_fresh_observation(
        self, tidal, clock
    ):
        """Local silence must gate too — receiving nothing is its own problem."""
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        clock.advance(8_000)
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.STALE

    async def test_disconnected_is_unavailable_regardless_of_timestamps(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        tidal.connected[VENUE] = False
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE

    async def test_desynchronized_is_unavailable_regardless_of_timestamps(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        tidal.books[(VENUE, "BTC-USDT")].invalidate("test")
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE

    async def test_crossed_book_is_unavailable_regardless_of_timestamps(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms(), bid=100.0, ask=101.0))
        await tidal.on_delta(
            BookDelta(
                venue=VENUE, symbol="BTC-USDT", exchange_ts=clock.now_ms(),
                received_ts=clock.now_ms(), first_sequence=1001, sequence=1002,
                bids=[PriceLevel(price=105.0, size=1.0)],  # crosses the 101.0 ask
            )
        )
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE

    async def test_empty_side_is_unavailable_regardless_of_timestamps(self, tidal, clock):
        await tidal.on_snapshot(
            OrderBookSnapshot(
                venue=VENUE, symbol="BTC-USDT", exchange_ts=clock.now_ms(),
                received_ts=clock.now_ms(), sequence=1000,
                bids=[PriceLevel(price=100.0, size=1.0)], asks=[], is_checkpoint=True,
            )
        )
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE


class TestClockSkew:
    async def test_a_small_future_exchange_timestamp_is_tolerated(self, tidal, clock):
        """Two independently-synced clocks are never in perfect lockstep."""
        await tidal.on_snapshot(snap(clock.now_ms() + 50, clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.FRESH

    async def test_an_unreasonable_future_exchange_timestamp_is_not_fresh(self, tidal, clock):
        far_future = clock.now_ms() + 10_000  # default tolerance is 2000ms
        await tidal.on_snapshot(snap(far_future, clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is not DataQuality.FRESH
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE

    async def test_the_violation_is_visible_on_health_and_as_a_system_event(
        self, tidal, clock, bus
    ):
        from core.events import EventType

        events = []

        async def spy(event):
            events.append(event)

        bus.subscribe(spy, types=[EventType.SYSTEM_EVENT], name="spy")
        await tidal.on_snapshot(snap(clock.now_ms() + 10_000, clock.now_ms()))
        await bus.drain()

        assert tidal.clock_skew_violations == 1
        assert any(e.payload.get("kind") == "CLOCK_SKEW" for e in events), (
            "a broken clock must be visible on the bus, not just internally counted"
        )
        component = tidal.health.snapshot().components.get("TIDAL")
        assert component is not None and component.error_count >= 1

    async def test_repeated_violations_on_one_book_are_rate_limited(self, tidal, clock, bus):
        from core.events import EventType

        count = 0

        async def spy(_event):
            nonlocal count
            count += 1

        bus.subscribe(spy, types=[EventType.SYSTEM_EVENT], name="spy")
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        for i in range(20):
            await tidal.on_delta(
                delta(clock.now_ms() + 10_000, clock.now_ms(), first=1001 + i, final=1002 + i)
            )
        await bus.drain()
        assert count == 1, "the same broken clock reported 20 times is one fact, not 20"
        assert tidal.clock_skew_violations == 20, "each occurrence is still counted"

    async def test_local_clock_behind_exchange_is_the_same_skew_direction(self, tidal, clock):
        """"Local clock behind exchange" and "exchange ahead of local" are the
        same physical situation from TIDAL's point of view — it only ever
        sees the two timestamps it was given, never which machine is "right".
        """
        await tidal.on_snapshot(snap(clock.now_ms() + 10_000, clock.now_ms()))
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.UNAVAILABLE

    async def test_a_normally_delayed_exchange_event_is_not_treated_as_skew(
        self, tidal, clock
    ):
        """Exchange behind receipt is ordinary transport latency, not skew."""
        await tidal.on_snapshot(snap(clock.now_ms() - 100, clock.now_ms()))
        assert tidal.clock_skew_violations == 0
        assert tidal.venue_state(VENUE, "BTC-USDT").quality is DataQuality.FRESH


class TestLatencyDoesNotHideSkew:
    async def test_negative_raw_latency_is_recorded_not_silently_zeroed(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms() + 30, clock.now_ms()))
        key = (VENUE, "BTC-USDT")
        assert tidal.clock_skew_ms[key] == pytest.approx(-30.0)
        # The economic latency ZEPHR consumes still cannot be negative.
        assert tidal.latency_ms[key] == 0.0

    async def test_zero_latency_from_real_zero_latency_is_distinguishable_from_skew(
        self, tidal, clock
    ):
        await tidal.on_snapshot(snap(clock.now_ms(), clock.now_ms()))
        key = (VENUE, "BTC-USDT")
        assert tidal.latency_ms[key] == 0.0
        assert tidal.clock_skew_ms[key] == 0.0  # genuinely zero, not masking anything

        await tidal.on_delta(delta(clock.now_ms() + 40, clock.now_ms()))
        # latency_ms still floored at 0 (smoothed toward it) ...
        assert tidal.latency_ms[key] < 40.0
        # ... but clock_skew_ms says exactly what happened.
        assert tidal.clock_skew_ms[key] == pytest.approx(-40.0)

    async def test_positive_latency_is_unaffected(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms() - 20, clock.now_ms()))
        key = (VENUE, "BTC-USDT")
        assert tidal.latency_ms[key] == pytest.approx(20.0)
        assert tidal.clock_skew_ms[key] == pytest.approx(20.0)

    async def test_clock_skew_is_exposed_on_venue_market_state(self, tidal, clock):
        await tidal.on_snapshot(snap(clock.now_ms() + 15, clock.now_ms()))
        state = tidal.venue_state(VENUE, "BTC-USDT")
        assert state.clock_skew_ms == pytest.approx(-15.0)


# ======================================================================
# Timestamp units and per-venue parsers stay milliseconds throughout
# ======================================================================


class TestTimestampParsing:
    def test_binance_event_time_is_milliseconds(self):
        delta = parser_a.parse_depth_update(
            {"e": "depthUpdate", "E": START_MS, "s": "BTCUSDT", "U": 1, "u": 2},
            START_MS + 5,
        )
        assert delta.exchange_ts == START_MS
        assert delta.received_ts == START_MS + 5

    def test_binance_trade_time_prefers_t_over_e(self):
        trade = parser_a.parse_trade(
            {"s": "BTCUSDT", "T": START_MS, "E": START_MS + 1000, "p": "1", "q": "1", "t": 1},
            START_MS,
        )
        assert trade.exchange_ts == START_MS

    def test_coinbase_iso_timestamp_becomes_epoch_milliseconds(self):
        # A known instant: 2024-01-01T00:00:00.500Z.
        ms = parser_b.parse_iso_ms("2024-01-01T00:00:00.500Z", fallback=0)
        assert ms == 1_704_067_200_500

    def test_coinbase_falls_back_to_receipt_time_on_missing_time(self):
        assert parser_b.parse_iso_ms(None, fallback=START_MS) == START_MS

    def test_coinbase_falls_back_to_receipt_time_on_unparseable_time(self):
        assert parser_b.parse_iso_ms("not-a-timestamp", fallback=START_MS) == START_MS
