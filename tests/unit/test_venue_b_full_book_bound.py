"""VENUE_B's full order book fits its storage ceiling: P12-F3.

THE FIELD FAILURE
=================
With the P12-F1 transport ceiling raised to 8 MiB, the Coinbase snapshots
finally arrived intact. The production stack then logged, every few seconds:

    VENUE_B ... sent 1000 (OK); then received 1000 (OK)

A *clean* close, over and over. The transport was fine; the book was not. A
Coinbase level-2 subscription delivers the entire order book, and the live
Codespaces rehearsal measured:

    ETH-USD    8,363 bids / 11,278 asks   (~492 KB encoded)
    BTC-USD   21,109 bids / 21,203 asks   (~1.11 MB encoded)

Both exceed the generic ``max_book_levels_per_side`` of 10,000 on at least one
side. So every legitimate snapshot was rejected by ``apply_snapshot``, every
rejection raised the overflow/resync path, ``VenueBAdapter.request_resync``
closed the socket deliberately, Coinbase sent another perfectly good full
snapshot on reconnect, and the loop ran forever. TIDAL never saw a usable
VENUE_B book.

THE FIX, AND WHAT IT IS NOT
===========================
VENUE_B carries an explicit, evidence-based ceiling of 50,000 levels per side.

It is **not** a trim: an accepted snapshot stays authoritative in full, and one
that does not fit is still rejected whole rather than truncated. It is **not**
a global increase: the default stays 10,000 for simulated and Binance-style
feeds, which are depth-limited and have no evidence justifying more. It is
**not** unbounded: 50,000 is ~2.4x the largest side actually observed and a
quarter of the model's 200,000 maximum, and the overflow path below it is
unchanged and still armed.
"""

from __future__ import annotations

import pytest

from agents.tidal import Tidal
from agents.tidal.book import BookOverflowError, LocalOrderBook
from apps.orchestrator.wiring import ResyncBridge
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, VenueConfig, default_venues
from core.events import EventType
from core.health import HealthRegistry
from core.models.common import Side
from core.models.market import OrderBookSnapshot, PriceLevel
from tests.conftest import START_MS
from venues.base.adapter import VenueAdapter

#: Exactly what the live rehearsal measured, per side.
FIELD_BTC_BIDS = 21_109
FIELD_BTC_ASKS = 21_203
FIELD_ETH_BIDS = 8_363
FIELD_ETH_ASKS = 11_278

#: The ceiling this finding sets for VENUE_B.
VENUE_B_CEILING = 50_000


def live_venue(name: str) -> VenueConfig:
    return next(v for v in default_venues() if v.name == name)


def coinbase_snapshot(
    symbol: str, *, bids: int, asks: int, seq: int = 1
) -> OrderBookSnapshot:
    """A full-book snapshot shaped like the one Coinbase actually sends."""
    return OrderBookSnapshot(
        venue="VENUE_B",
        symbol=symbol,
        exchange_ts=START_MS,
        received_ts=START_MS,
        sequence=seq,
        bids=[PriceLevel(price=100_000.0 - i * 0.01, size=1.0) for i in range(bids)],
        asks=[PriceLevel(price=100_001.0 + i * 0.01, size=1.0) for i in range(asks)],
        is_checkpoint=True,
    )


def venue_b_book(symbol: str = "BTC-USD") -> LocalOrderBook:
    """A book sized exactly as the live VENUE_B configuration sizes it."""
    config = live_venue("VENUE_B")
    return LocalOrderBook(
        venue="VENUE_B",
        symbol=symbol,
        max_depth=config.book_depth_levels,
        max_levels_per_side=config.max_book_levels_per_side,
    )


class RecordingAdapter(VenueAdapter):
    """Records resync requests instead of touching a socket."""

    def __init__(self, config, clock, symbols) -> None:
        super().__init__(config, clock, symbols)
        self.requests: list[tuple[str, str]] = []

    async def run(self) -> None:  # pragma: no cover - never started
        return None

    async def request_resync(self, symbol: str, reason: str = "") -> None:
        self.requests.append((symbol, reason))


# ======================================================================
# A — the live configuration
# ======================================================================


class TestLiveVenueConfiguration:
    def test_venue_b_carries_an_explicit_full_book_ceiling(self):
        assert live_venue("VENUE_B").max_book_levels_per_side == VENUE_B_CEILING

    def test_the_ceiling_clears_the_largest_side_actually_observed(self):
        assert max(FIELD_BTC_ASKS, FIELD_BTC_BIDS) * 2 < VENUE_B_CEILING, (
            "the bound must leave real headroom above the field measurement, "
            "not merely scrape past it"
        )

    def test_it_is_finite_and_well_inside_the_model_maximum(self):
        config = live_venue("VENUE_B")
        assert config.max_book_levels_per_side < 200_000
        field = VenueConfig.model_fields["max_book_levels_per_side"]
        assert any(getattr(m, "le", None) == 200_000 for m in field.metadata)

    def test_venue_a_is_unchanged(self):
        """Binance-style feeds are depth-limited; nothing justifies more."""
        assert live_venue("VENUE_A").max_book_levels_per_side == 10_000

    def test_the_generic_default_is_unchanged(self):
        assert VenueConfig.model_fields["max_book_levels_per_side"].default == 10_000

    def test_simulated_venues_are_unchanged(self):
        from core.config import simulated_venues

        for config in simulated_venues():
            assert config.max_book_levels_per_side == 10_000

    def test_the_transport_bound_is_untouched_by_this_finding(self):
        """P12-F1 stays as it was: finite, 8 MiB, never None."""
        for config in default_venues():
            assert config.ws_max_message_bytes == 8_388_608

    def test_venue_b_symbols_are_unchanged(self):
        assert live_venue("VENUE_B").symbols == ["BTC-USD", "ETH-USD"]

    def test_venue_a_symbols_are_unchanged(self):
        """USD and USDT are different instruments and stay that way."""
        assert live_venue("VENUE_A").symbols == ["BTC-USDT", "ETH-USDT"]


# ======================================================================
# B — the real field-shaped snapshot is accepted
# ======================================================================


class TestFieldShapedSnapshotsAreAccepted:
    def test_the_measured_btc_snapshot_is_accepted(self):
        book = venue_b_book("BTC-USD")
        book.apply_snapshot(
            coinbase_snapshot("BTC-USD", bids=FIELD_BTC_BIDS, asks=FIELD_BTC_ASKS)
        )
        assert book.synced
        assert not book.needs_resync
        assert book.overflow_count == 0
        assert len(book.bids) == FIELD_BTC_BIDS
        assert len(book.asks) == FIELD_BTC_ASKS

    def test_the_measured_eth_snapshot_is_accepted(self):
        book = venue_b_book("ETH-USD")
        book.apply_snapshot(
            coinbase_snapshot("ETH-USD", bids=FIELD_ETH_BIDS, asks=FIELD_ETH_ASKS)
        )
        assert book.synced
        assert book.overflow_count == 0

    def test_the_old_ceiling_would_have_rejected_both(self):
        """The regression this finding exists for, stated directly."""
        for symbol, bids, asks in (
            ("BTC-USD", FIELD_BTC_BIDS, FIELD_BTC_ASKS),
            ("ETH-USD", FIELD_ETH_BIDS, FIELD_ETH_ASKS),
        ):
            old = LocalOrderBook(
                venue="VENUE_B", symbol=symbol, max_depth=25, max_levels_per_side=10_000
            )
            with pytest.raises(BookOverflowError):
                old.apply_snapshot(coinbase_snapshot(symbol, bids=bids, asks=asks))

    def test_a_snapshot_exactly_at_the_ceiling_is_valid(self):
        book = venue_b_book()
        book.apply_snapshot(
            coinbase_snapshot("BTC-USD", bids=VENUE_B_CEILING, asks=VENUE_B_CEILING)
        )
        assert book.synced
        assert book.overflow_count == 0


# ======================================================================
# C — the ceiling is still enforced
# ======================================================================


class TestTheCeilingIsStillEnforced:
    def test_one_level_past_the_ceiling_is_rejected(self):
        book = venue_b_book()
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                coinbase_snapshot("BTC-USD", bids=VENUE_B_CEILING + 1, asks=10)
            )
        assert not book.synced
        assert book.needs_resync
        assert book.overflow_count == 1

    def test_the_ask_side_alone_can_trigger_it(self):
        book = venue_b_book()
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                coinbase_snapshot("BTC-USD", bids=10, asks=VENUE_B_CEILING + 1)
            )
        assert not book.synced

    def test_no_part_of_an_oversized_snapshot_is_installed(self):
        """Rejected whole. Not trimmed, not partially applied."""
        book = venue_b_book()
        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                coinbase_snapshot("BTC-USD", bids=VENUE_B_CEILING + 1, asks=10)
            )
        assert book.bids == {}
        assert book.asks == {}

    def test_previous_authoritative_state_survives_a_rejected_snapshot(self):
        book = venue_b_book()
        book.apply_snapshot(
            coinbase_snapshot("BTC-USD", bids=FIELD_BTC_BIDS, asks=FIELD_BTC_ASKS)
        )
        good_bids = dict(book.bids)

        with pytest.raises(BookOverflowError):
            book.apply_snapshot(
                coinbase_snapshot(
                    "BTC-USD", bids=VENUE_B_CEILING + 1, asks=10, seq=2
                )
            )

        assert book.bids == good_bids, (
            "a rejected snapshot must leave the previous book exactly as it was"
        )
        assert book.needs_resync, "but the book must know it can no longer be trusted"


# ======================================================================
# D — no destructive trimming above the read depth
# ======================================================================


class TestNoDestructiveTrimming:
    def test_levels_far_beyond_the_read_depth_stay_in_storage(self):
        """TIDAL-M1's invariant, at full-book scale.

        ``book_depth_levels`` is a read-time view. Storage keeps everything
        the venue said was there.
        """
        config = live_venue("VENUE_B")
        book = venue_b_book()
        book.apply_snapshot(
            coinbase_snapshot("BTC-USD", bids=FIELD_BTC_BIDS, asks=FIELD_BTC_ASKS)
        )

        assert len(book.bids) == FIELD_BTC_BIDS
        assert len(book.levels(Side.BUY)) == config.book_depth_levels, (
            "the read view is still trimmed to the configured depth"
        )

        deep_price = 100_000.0 - (FIELD_BTC_BIDS - 1) * 0.01
        assert deep_price in book.bids, "a deeply buried level must still be held"
        assert deep_price not in [lvl.price for lvl in book.levels(Side.BUY)]

    def test_a_buried_level_can_still_resurface(self):
        book = venue_b_book()
        book.apply_snapshot(coinbase_snapshot("BTC-USD", bids=5_000, asks=5_000))
        buried = 100_000.0 - 4_999 * 0.01
        assert book.bids[buried] == 1.0


# ======================================================================
# E — the reconnect loop, through the real TIDAL recovery wiring
# ======================================================================


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    """Settings carrying the real live venue configuration."""
    return settings.model_copy(update={"venues": default_venues()})


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_MS)


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus(raise_on_handler_error=True)


@pytest.fixture
def tidal(bus: InMemoryEventBus, clock: ManualClock, live_settings: Settings) -> Tidal:
    agent = Tidal(bus, clock, live_settings, HealthRegistry(clock=clock))
    agent.subscribe()
    return agent


class TestTheReconnectLoopIsGone:
    """The whole point of P12-F3: a legitimate live snapshot must not ask for
    a resync, because that request is what closed the socket and restarted the
    cycle."""

    async def test_a_field_sized_snapshot_requests_no_resync(
        self, tidal, bus, clock, live_settings
    ):
        adapter = RecordingAdapter(
            live_settings.venue("VENUE_B"), clock, ["BTC-USD", "ETH-USD"]
        )
        bus.subscribe(
            ResyncBridge({"VENUE_B": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )

        await tidal.on_snapshot(
            coinbase_snapshot("BTC-USD", bids=FIELD_BTC_BIDS, asks=FIELD_BTC_ASKS)
        )
        await tidal.on_snapshot(
            coinbase_snapshot("ETH-USD", bids=FIELD_ETH_BIDS, asks=FIELD_ETH_ASKS)
        )
        await bus.drain()

        assert adapter.requests == [], (
            "a legitimate full Coinbase snapshot must not trigger the resync "
            "that closes the socket -- that is the reconnect loop"
        )
        for symbol in ("BTC-USD", "ETH-USD"):
            book = tidal.books[("VENUE_B", symbol)]
            assert book.synced, f"{symbol} must be usable"
            assert book.overflow_count == 0

    async def test_tidal_publishes_usable_state_for_both_symbols(
        self, tidal, bus, clock
    ):
        await tidal.on_snapshot(
            coinbase_snapshot("BTC-USD", bids=FIELD_BTC_BIDS, asks=FIELD_BTC_ASKS)
        )
        await tidal.on_snapshot(
            coinbase_snapshot("ETH-USD", bids=FIELD_ETH_BIDS, asks=FIELD_ETH_ASKS)
        )
        await bus.drain()

        for symbol in ("BTC-USD", "ETH-USD"):
            book = tidal.books[("VENUE_B", symbol)]
            assert book.levels(Side.BUY), f"{symbol} has no usable bid view"
            assert book.levels(Side.SELL), f"{symbol} has no usable ask view"

    async def test_a_genuinely_oversized_snapshot_still_requests_resync(
        self, tidal, bus, clock, live_settings
    ):
        """The safety path must still fire -- this fix widened the bound, it
        did not remove it."""
        adapter = RecordingAdapter(
            live_settings.venue("VENUE_B"), clock, ["BTC-USD", "ETH-USD"]
        )
        bus.subscribe(
            ResyncBridge({"VENUE_B": adapter}),
            types=[EventType.BOOK_RESYNC_REQUESTED],
            name="venue-resync",
        )

        await tidal.on_snapshot(
            coinbase_snapshot("BTC-USD", bids=VENUE_B_CEILING + 1, asks=10)
        )
        await bus.drain()

        assert [s for s, _ in adapter.requests] == ["BTC-USD"]
        assert tidal.books[("VENUE_B", "BTC-USD")].overflow_count == 1
