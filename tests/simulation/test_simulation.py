"""The synthetic market itself, plus bus, clock and health primitives.

If the simulator is not deterministic and not economically sane, every test
built on it is measuring noise.
"""

from __future__ import annotations

import pytest

from agents.tidal.book import LocalOrderBook
from core.bus import InMemoryEventBus
from core.clock import ManualClock, SystemClock
from core.events import Event, EventType
from core.health import HealthPolicy, HealthRegistry
from core.models.market import OrderBookSnapshot, TradeEvent
from core.models.ops import HealthStatus
from monitoring import build_registry
from simulation.market import (
    DislocationSpec,
    SymbolSpec,
    SyntheticMarket,
    VenueSpec,
    default_market,
    recurring_dislocations,
)
from tests.conftest import START_MS
from venues.base.messages import BookDelta


def build_books(market: SyntheticMarket, steps: int) -> dict[tuple[str, str], LocalOrderBook]:
    books: dict[tuple[str, str], LocalOrderBook] = {}
    for _ in range(steps):
        for message in market.next_step():
            if isinstance(message, (OrderBookSnapshot, BookDelta)):
                key = (message.venue, message.symbol)
                book = books.setdefault(
                    key, LocalOrderBook(venue=message.venue, symbol=message.symbol, max_depth=30)
                )
                if isinstance(message, OrderBookSnapshot):
                    book.apply_snapshot(message)
                else:
                    book.apply_delta(message)
    return books


class TestSyntheticMarket:
    def test_the_same_seed_produces_the_same_market(self):
        a = default_market(seed=42, start_ms=START_MS)
        b = default_market(seed=42, start_ms=START_MS)
        for _ in range(100):
            assert [m.model_dump() for m in a.next_step()] == [
                m.model_dump() for m in b.next_step()
            ]

    def test_different_seeds_diverge(self):
        a = default_market(seed=1, start_ms=START_MS)
        b = default_market(seed=2, start_ms=START_MS)
        for _ in range(50):
            a.next_step()
            b.next_step()
        assert a.true_price("BTC-USD") != b.true_price("BTC-USD")

    def test_deltas_apply_cleanly_with_no_sequence_gaps(self):
        market = default_market(start_ms=START_MS)
        books = build_books(market, 300)
        assert books
        for book in books.values():
            assert book.sequence_gaps == 0
            assert book.usable

    def test_books_are_never_crossed(self):
        market = default_market(start_ms=START_MS)
        books = build_books(market, 300)
        for book in books.values():
            assert not book.crossed

    def test_timestamps_advance_monotonically(self):
        market = default_market(start_ms=START_MS)
        stamps = []
        for _ in range(50):
            for message in market.next_step():
                stamps.append(message.received_ts)
        assert stamps == sorted(stamps)

    def test_trades_print_at_the_touch(self):
        market = default_market(start_ms=START_MS)
        books = {}
        for _ in range(200):
            for message in market.next_step():
                key = (message.venue, message.symbol)
                if isinstance(message, OrderBookSnapshot):
                    books.setdefault(
                        key, LocalOrderBook(venue=message.venue, symbol=message.symbol)
                    ).apply_snapshot(message)
                elif isinstance(message, BookDelta) and key in books:
                    books[key].apply_delta(message)
                elif isinstance(message, TradeEvent) and key in books:
                    book = books[key]
                    if book.usable:
                        assert book.best_bid <= message.price <= book.best_ask

    def test_a_scheduled_dislocation_appears_and_ends(self):
        market = SyntheticMarket(
            symbols=[SymbolSpec(symbol="BTC-USD", initial_price=100_000.0, vol=0.0)],
            venues=[
                VenueSpec(venue="VENUE_A", basis_vol_bps=0.0, depth_factor=1.0),
                VenueSpec(venue="VENUE_B", basis_vol_bps=0.0, depth_factor=1.0),
            ],
            seed=5,
            start_ms=START_MS,
            dislocations=[
                DislocationSpec(
                    start_step=10,
                    duration_steps=10,
                    venue="VENUE_B",
                    symbol="BTC-USD",
                    magnitude_bps=50.0,
                )
            ],
        )
        gaps = []
        for _ in range(40):
            market.next_step()
            gaps.append(
                (market.venue_mid("VENUE_B", "BTC-USD") - market.venue_mid("VENUE_A", "BTC-USD"))
                / market.true_price("BTC-USD")
                * 10_000
            )
        # Quiet before, dislocated during, quiet after.
        assert abs(gaps[5]) < 1.0
        assert gaps[15] == pytest.approx(50.0, abs=1.0)
        assert abs(gaps[35]) < 1.0

    def test_recurring_schedule_alternates_direction(self):
        specs = recurring_dislocations(["BTC-USD"], count=4)
        magnitudes = [s.magnitude_bps for s in specs]
        assert magnitudes[0] > 0 and magnitudes[1] < 0

    def test_a_thinner_venue_really_is_thinner(self):
        market = SyntheticMarket(
            symbols=[SymbolSpec(symbol="BTC-USD", initial_price=100_000.0, vol=0.0)],
            venues=[
                VenueSpec(venue="DEEP", depth_factor=4.0, basis_vol_bps=0.0),
                VenueSpec(venue="THIN", depth_factor=0.25, basis_vol_bps=0.0),
            ],
            seed=3,
            start_ms=START_MS,
        )
        books = build_books(market, 60)
        deep = sum(s for s in books[("DEEP", "BTC-USD")].bids.values())
        thin = sum(s for s in books[("THIN", "BTC-USD")].bids.values())
        assert deep > thin * 3


class TestManualClock:
    def test_time_only_moves_forward(self):
        clock = ManualClock(START_MS)
        clock.advance(100)
        assert clock.now_ms() == START_MS + 100
        with pytest.raises(ValueError, match="backwards"):
            clock.set(START_MS)

    async def test_sleep_releases_when_time_reaches_the_deadline(self):
        import asyncio

        clock = ManualClock(START_MS)
        released = []

        async def sleeper():
            await clock.sleep(1.0)
            released.append(clock.now_ms())

        task = asyncio.create_task(sleeper())
        await asyncio.sleep(0)
        assert clock.pending_sleepers == 1
        assert not released

        clock.advance(999)
        await asyncio.sleep(0)
        assert not released

        clock.advance(1)
        await asyncio.sleep(0)
        await task
        assert released == [START_MS + 1_000]

    async def test_zero_sleep_yields_without_blocking(self):
        clock = ManualClock(START_MS)
        await clock.sleep(0)

    def test_system_clock_is_roughly_wall_time(self):
        import time

        assert abs(SystemClock().now_ms() - int(time.time() * 1000)) < 5_000


class TestEventBus:
    async def test_events_reach_subscribers_in_publication_order(self):
        bus = InMemoryEventBus()
        seen: list[str] = []

        async def handler(event):
            seen.append(event.source)

        bus.subscribe(handler)
        for source in ("a", "b", "c"):
            await bus.publish(
                Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source=source)
            )
        await bus.drain()
        assert seen == ["a", "b", "c"]

    async def test_type_filters_are_honoured(self):
        bus = InMemoryEventBus()
        seen: list[EventType] = []

        async def handler(event):
            seen.append(event.type)

        bus.subscribe(handler, types=[EventType.PAPER_FILL])
        await bus.publish(Event(type=EventType.PAPER_FILL, ts_ms=START_MS, source="x"))
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="x"))
        await bus.drain()
        assert seen == [EventType.PAPER_FILL]

    async def test_cascading_publications_are_processed_breadth_first(self):
        bus = InMemoryEventBus()
        order: list[str] = []

        async def first(event):
            order.append(event.source)
            if event.source == "root":
                await bus.publish(
                    Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="child")
                )

        bus.subscribe(first)
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="root"))
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="sibling"))
        await bus.drain()
        assert order == ["root", "sibling", "child"]

    async def test_a_failing_handler_is_isolated(self):
        bus = InMemoryEventBus()
        survived: list[int] = []

        async def broken(event):
            raise RuntimeError("nope")

        async def fine(event):
            survived.append(1)

        broken_sub = bus.subscribe(broken)
        bus.subscribe(fine)
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="x"))
        await bus.drain()
        assert survived == [1]
        assert broken_sub.errors == 1

    async def test_sequences_are_assigned_monotonically(self):
        bus = InMemoryEventBus()
        events = [
            Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="x") for _ in range(5)
        ]
        for event in events:
            await bus.publish(event)
        assert [e.sequence for e in events] == [1, 2, 3, 4, 5]

    async def test_middleware_sees_everything(self):
        bus = InMemoryEventBus()
        recorded: list[Event] = []

        async def middleware(event):
            recorded.append(event)

        bus.add_middleware(middleware)
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="x"))
        assert len(recorded) == 1

    async def test_unsubscribe_stops_delivery(self):
        bus = InMemoryEventBus()
        seen: list[int] = []
        sub = bus.subscribe(lambda e: seen.append(1) or _noop())
        bus.unsubscribe(sub)
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="x"))
        await bus.drain()
        assert seen == []

    async def test_runaway_cascade_is_caught(self):
        bus = InMemoryEventBus()

        async def forever(event):
            await bus.publish(
                Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="loop")
            )

        bus.subscribe(forever)
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=START_MS, source="loop"))
        with pytest.raises(RuntimeError, match="did not terminate"):
            await bus.drain(max_cycles=50)


class TestHealthRegistry:
    def test_unregistered_components_are_offline(self, clock):
        registry = HealthRegistry(clock=clock)
        assert registry.status_of("GHOST") is HealthStatus.OFFLINE

    def test_a_heartbeat_makes_a_component_healthy(self, clock):
        registry = HealthRegistry(clock=clock)
        registry.heartbeat("NORO")
        assert registry.status_of("NORO") is HealthStatus.HEALTHY

    def test_a_missing_heartbeat_degrades_then_goes_offline(self, clock):
        registry = HealthRegistry(
            clock=clock, policy=HealthPolicy(degraded_after_ms=1_000, offline_after_ms=5_000)
        )
        registry.heartbeat("NORO")
        clock.advance(1_500)
        assert registry.status_of("NORO") is HealthStatus.DEGRADED
        clock.advance(5_000)
        assert registry.status_of("NORO") is HealthStatus.OFFLINE

    def test_error_budget_degrades_a_component(self, clock):
        registry = HealthRegistry(clock=clock, policy=HealthPolicy(error_budget=3))
        registry.heartbeat("TIDAL")
        for _ in range(3):
            registry.record_error("TIDAL", "boom")
        assert registry.snapshot().components["TIDAL"].status is HealthStatus.DEGRADED

    def test_required_components_are_reported_individually(self, clock):
        registry = HealthRegistry(clock=clock)
        registry.heartbeat("TIDAL")
        ok, bad = registry.all_healthy(["TIDAL", "NORO"])
        assert not ok and bad == ["NORO"]

    def test_overall_status_is_the_worst_component(self, clock):
        registry = HealthRegistry(clock=clock)
        registry.heartbeat("TIDAL")
        registry.heartbeat("NORO", status=HealthStatus.OFFLINE)
        assert registry.snapshot().status is HealthStatus.OFFLINE


class TestMetrics:
    def test_counters_gauges_and_histograms_render(self):
        registry = build_registry()
        registry.inc("tf_events_processed_total", 2, source="TIDAL")
        registry.set("tf_net_pnl", -12.5)
        registry.observe("tf_slippage_bps", 3.0)
        text = registry.render()
        assert 'tf_events_processed_total{source="TIDAL"} 2.0' in text
        assert "tf_net_pnl -12.5" in text
        assert "tf_slippage_bps_bucket" in text
        assert "# TYPE tf_slippage_bps histogram" in text

    def test_histogram_buckets_are_cumulative(self):
        registry = build_registry()
        for value in (1, 4, 40, 4_000):
            registry.observe("tf_market_data_latency_ms", value)
        histogram = registry.histogram("tf_market_data_latency_ms")
        counts = [count for _, count in histogram.cumulative()]
        assert counts == sorted(counts)
        assert histogram.count == 4

    def test_non_finite_observations_are_ignored(self):
        registry = build_registry()
        registry.observe("tf_slippage_bps", float("inf"))
        assert registry.histogram("tf_slippage_bps").count == 0

    def test_snapshot_flattens_labels(self):
        registry = build_registry()
        registry.inc("tf_paper_orders_total", 1, venue="VENUE_A")
        assert 'tf_paper_orders_total{venue="VENUE_A"}' in registry.snapshot()


async def _noop() -> None:
    return None
