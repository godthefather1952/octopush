"""TIDAL — market intelligence infrastructure.

TIDAL is the foundation: it owns every local order book, normalises venue
state, measures freshness and latency, and publishes the one
:class:`MarketState` everything downstream reads.  Nothing here is
probabilistic and nothing here calls a model.
"""

from __future__ import annotations

import logging

from agents.tidal.book import BookDesyncError, BookOverflowError, LocalOrderBook
from agents.tidal.metrics import MidWindow, TradeFlowWindow, compute_metrics
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import MARKET_INPUT_TYPES, Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, DataQuality, Millis
from core.models.market import (
    BookMetrics,
    ConsolidatedView,
    MarketState,
    OrderBookSnapshot,
    TradeEvent,
    VenueMarketState,
    safe_bps,
)
from core.models.opportunity import Opportunity
from core.models.ops import HealthStatus, Severity, SystemEvent
from venues.base.messages import BookDelta, ResyncRequest, VenueStatus

log = logging.getLogger(__name__)

SERVICE = "TIDAL"
VERSION = "tidal-0.1"


class Tidal:
    """Maintains books and publishes consolidated market state."""

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        settings: Settings,
        health: HealthRegistry,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.health = health
        self.books: dict[tuple[str, str], LocalOrderBook] = {}
        self.flows: dict[tuple[str, str], TradeFlowWindow] = {}
        self.mids: dict[tuple[str, str], MidWindow] = {}
        self.connected: dict[str, bool] = {}
        self.reconnects: dict[str, int] = {}
        #: Smoothed non-negative economic latency: received_ts - exchange_ts,
        #: floored at zero. This is what ZEPHR's cost model consumes, and it
        #: must stay a usable "how slow is this feed" number — so a clock
        #: problem is never folded into it silently. See ``clock_skew_ms``.
        self.latency_ms: dict[tuple[str, str], float] = {}
        #: Most recently observed raw skew (received_ts - exchange_ts) per
        #: book, unfloored. Negative means the exchange timestamp is *ahead*
        #: of local receipt — the case ``latency_ms`` cannot represent, and
        #: the one that must stay visible rather than silently reading as
        #: "zero latency" (TIDAL-H3/H9: do not let zero mean "my clock is
        #: wrong").
        self.clock_skew_ms: dict[tuple[str, str], float] = {}
        #: Count of skew observations beyond ``risk.max_clock_skew_ms`` —
        #: severe enough that "clock drift" no longer explains them.
        self.clock_skew_violations = 0
        #: Rate-limits the CLOCK_SKEW system event the same way resync
        #: requests are rate-limited: the underlying condition persists across
        #: every message until it resolves, and one event per message would
        #: flood the bus with the same fact.
        self._clock_skew_reported_ms: dict[tuple[str, str], Millis] = {}
        self.desyncs = 0
        self.resync_requests = 0
        #: When a resync was last asked for, per book. A gap usually arrives as
        #: a burst — every subsequent delta hits the same unsynchronised book —
        #: and one request per message would be a request storm aimed at
        #: whoever owns the feed.
        self._resync_requested_ms: dict[tuple[str, str], Millis] = {}
        #: Consecutive snapshots that overflowed the storage bound for one
        #: book, reset the moment a snapshot succeeds. Recovery from a
        #: sequence gap is expected to succeed on the next checkpoint; an
        #: oversized checkpoint recurring is not a transient condition, it is
        #: evidence ``max_book_levels_per_side`` is misconfigured for this
        #: venue/symbol (FULL-BOOK STORAGE BOUND, Batch 5 pre-commit
        #: correction) -- see ``_persistent_overflow_threshold``.
        self._snapshot_overflow_streak: dict[tuple[str, str], int] = {}
        self.state: MarketState | None = None
        #: Highest ``Event.sequence`` among market-input events actually
        #: applied to book state so far. Publication order (when
        #: ``bus.publish()`` assigns a sequence number) and dispatch order
        #: (when a subscriber's handler actually runs) are NOT the same
        #: moment under an asynchronous bus -- an event can be published,
        #: recorded, and even sit ahead of a later tick boundary in the
        #: stored timeline, while still not yet applied here. This is the
        #: one place that knows the difference, updated only after a
        #: handler has actually mutated book state (see ``on_event``).
        self.processed_input_sequence: int = -1
        #: ``processed_input_sequence`` captured at the exact moment of the
        #: most recent ``build_state()`` call (see ``publish_state``) --
        #: frozen there deliberately, since ``processed_input_sequence``
        #: itself can keep advancing afterward (e.g. during the
        #: orchestrator's own post-snapshot ``bus.drain()``) without that
        #: advance being reflected in the ``MarketState`` already returned.
        self.last_snapshot_input_sequence: int = -1
        health.register(SERVICE, VERSION)

    #: Floor on the interval between resync requests for one book, in ms.
    resync_request_interval_ms: Millis = 5_000
    #: Consecutive snapshot overflows on one book before this is escalated
    #: from "recovering" to "likely misconfigured" (see
    #: ``_snapshot_overflow_streak``). Requests are already rate-limited by
    #: ``resync_request_interval_ms``, so this does not change retry
    #: frequency -- it only makes a persistent mismatch visible instead of
    #: looking identical to an ordinary, one-off recovery.
    _persistent_overflow_threshold: int = 3

    # -- wiring ------------------------------------------------------------

    def subscribe(self) -> None:
        self.bus.subscribe(
            self.on_event,
            types=[
                EventType.BOOK_SNAPSHOT,
                EventType.BOOK_DELTA,
                EventType.TRADE_PRINT,
                EventType.VENUE_CONNECTED,
                EventType.VENUE_DISCONNECTED,
                EventType.OPPORTUNITY_DETECTED,
            ],
            name="tidal",
        )

    def _book(self, venue: str, symbol: str) -> LocalOrderBook:
        key = (venue, symbol)
        if key not in self.books:
            known = self._known(venue)
            depth = self.settings.venue(venue).book_depth_levels if known else 25
            max_levels = (
                self.settings.venue(venue).max_book_levels_per_side
                if known
                else LocalOrderBook.max_levels_per_side
            )
            self.books[key] = LocalOrderBook(
                venue=venue, symbol=symbol, max_depth=depth, max_levels_per_side=max_levels
            )
            self.flows[key] = TradeFlowWindow()
            self.mids[key] = MidWindow()
        return self.books[key]

    def _known(self, venue: str) -> bool:
        return any(v.name == venue for v in self.settings.venues)

    # -- ingestion ---------------------------------------------------------

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.BOOK_SNAPSHOT:
            await self.on_snapshot(OrderBookSnapshot.model_validate(event.payload))
        elif event.type is EventType.BOOK_DELTA:
            await self.on_delta(BookDelta.model_validate(event.payload))
        elif event.type is EventType.TRADE_PRINT:
            await self.on_trade(TradeEvent.model_validate(event.payload))
        elif event.type is EventType.VENUE_CONNECTED:
            status = VenueStatus.model_validate(event.payload)
            was_connected = self.connected.get(status.venue, False)
            self.connected[status.venue] = True
            if not was_connected and status.venue in self.reconnects:
                self.reconnects[status.venue] += 1
            self.reconnects.setdefault(status.venue, 0)
        elif event.type is EventType.OPPORTUNITY_DETECTED:
            opinion = self.evaluate(
                Opportunity.model_validate(event.payload), event.ts_ms
            )
            if opinion is not None:
                await self.publish_opinion(opinion)
        elif event.type is EventType.VENUE_DISCONNECTED:
            status = VenueStatus.model_validate(event.payload)
            self.connected[status.venue] = False
            # A disconnected feed invalidates every book on that venue; the
            # data is not merely old, it is untrustworthy.
            for (venue, _symbol), book in self.books.items():
                if venue == status.venue:
                    book.invalidate("venue disconnected")

        # Recorded AFTER the branch above has actually mutated state, and
        # only for genuine market inputs -- OPPORTUNITY_DETECTED is handled
        # here too but is not a market input replay ever needs to cut on.
        if (
            event.type in MARKET_INPUT_TYPES
            and event.sequence is not None
            and event.sequence > self.processed_input_sequence
        ):
            self.processed_input_sequence = event.sequence

    async def request_resync(self, venue: str, symbol: str, reason: str = "") -> None:
        """Ask whoever owns this feed for a fresh checkpoint.

        Published as an event rather than called on an adapter. TIDAL holds no
        adapter reference and gains none here: it states that one book needs
        re-establishing, and whatever owns that feed decides what to do about
        it. That is the whole reason recovery is expressible at all — TIDAL
        cannot reach the venue, and should not be able to.

        Rate-limited per book, because a gap is reported by every delta that
        follows it until a snapshot lands.
        """
        key = (venue, symbol)
        now = self.clock.now_ms()
        last = self._resync_requested_ms.get(key)
        if last is not None and now - last < self.resync_request_interval_ms:
            return
        self._resync_requested_ms[key] = now
        self.resync_requests += 1
        await self.bus.publish(
            Event(
                type=EventType.BOOK_RESYNC_REQUESTED,
                ts_ms=now,
                source=SERVICE,
                schema_name="ResyncRequest",
                payload=ResyncRequest(
                    venue=venue, symbol=symbol, requested_at=now, reason=reason
                ).to_json_dict(),
            )
        )

    async def _record_latency(
        self, key: tuple[str, str], exchange_ts: Millis, received_ts: Millis
    ) -> None:
        """Update the smoothed latency estimate and surface any clock skew.

        ``raw`` is signed: positive is ordinary transport latency (the
        exchange observed the market before we received word of it, which is
        the only order events can happen in). Negative means the exchange
        timestamp is *after* local receipt time — either the two clocks are
        not perfectly synced (normal, and usually small), or the timestamp is
        wrong (not normal, and previously invisible: the old code clamped
        this to zero and folded it into "latency", so a broken clock and a
        perfectly fast feed were indistinguishable downstream).
        """
        raw = float(received_ts - exchange_ts)
        self.clock_skew_ms[key] = raw
        sample = raw if raw >= 0 else 0.0
        prior = self.latency_ms.get(key)
        self.latency_ms[key] = sample if prior is None else 0.8 * prior + 0.2 * sample
        if raw >= 0:
            return
        skew = -raw
        limit = self.settings.risk.max_clock_skew_ms
        if skew <= limit:
            return
        self.clock_skew_violations += 1
        self.health.record_error(
            SERVICE,
            f"{key[0]}:{key[1]} exchange timestamp {skew:.0f}ms ahead of receipt "
            f"(limit {limit}ms)",
        )
        now = self.clock.now_ms()
        last = self._clock_skew_reported_ms.get(key)
        if last is not None and now - last < self.resync_request_interval_ms:
            return
        self._clock_skew_reported_ms[key] = now
        await self._publish_system_event(
            "CLOCK_SKEW",
            Severity.WARNING,
            f"{key[0]}:{key[1]} exchange timestamp is {skew:.0f}ms ahead of local "
            f"receipt, past the {limit}ms tolerance",
            {"venue": key[0], "symbol": key[1], "skew_ms": skew, "limit_ms": limit},
        )

    async def on_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        """Apply a fresh checkpoint.

        A snapshot can fail exactly the same way a delta can (Batch 5
        pre-commit correction): :meth:`LocalOrderBook.apply_snapshot` raises
        :class:`BookOverflowError` if the snapshot itself already exceeds the
        storage bound. That is caught here the same way :meth:`on_delta`
        catches a sequence gap -- fail closed, report, and ask for recovery
        through the existing per-venue path -- so an oversized checkpoint
        never gets marked usable and never silently loses levels to fit.

        A run of consecutive overflowing snapshots on the same book is not
        the ordinary "gap, then one clean recovery" shape recovery is built
        for -- it means the venue's checkpoint depth and this book's
        ``max_book_levels_per_side`` are structurally incompatible, and no
        number of retries fixes that. That is escalated to a distinct,
        clearly-labelled health error once it persists, without changing how
        often a retry is actually attempted (still gated by the existing
        ``resync_request_interval_ms``, so this can never become a request
        storm).
        """
        key = (snapshot.venue, snapshot.symbol)
        book = self._book(*key)
        try:
            book.apply_snapshot(snapshot)
        except BookDesyncError as exc:
            self.desyncs += 1
            self.health.record_error(SERVICE, str(exc))
            if isinstance(exc, BookOverflowError):
                streak = self._snapshot_overflow_streak.get(key, 0) + 1
                self._snapshot_overflow_streak[key] = streak
                if streak >= self._persistent_overflow_threshold:
                    await self._publish_system_event(
                        "BOOK_OVERFLOW_PERSISTENT",
                        Severity.CRITICAL,
                        f"{key[0]}:{key[1]} snapshot has overflowed {streak} times in a "
                        "row -- max_book_levels_per_side is likely misconfigured for "
                        "this venue/symbol, not merely recovering from a transient gap",
                        {"venue": key[0], "symbol": key[1], "streak": streak},
                    )
            await self._publish_system_event(
                "BOOK_DESYNC",
                Severity.WARNING,
                str(exc),
                {"venue": snapshot.venue, "symbol": snapshot.symbol},
            )
            await self.request_resync(snapshot.venue, snapshot.symbol, str(exc))
            return
        self._snapshot_overflow_streak.pop(key, None)
        await self._record_latency(key, snapshot.exchange_ts, snapshot.received_ts)
        self.connected.setdefault(snapshot.venue, True)

    async def on_delta(self, delta: BookDelta) -> None:
        key = (delta.venue, delta.symbol)
        book = self._book(*key)
        try:
            book.apply_delta(delta)
        except BookDesyncError as exc:
            self.desyncs += 1
            self.health.record_error(SERVICE, str(exc))
            await self._publish_system_event(
                "BOOK_DESYNC",
                Severity.WARNING,
                str(exc),
                {"venue": delta.venue, "symbol": delta.symbol},
            )
            await self.request_resync(delta.venue, delta.symbol, str(exc))
            return
        await self._record_latency(key, delta.exchange_ts, delta.received_ts)

    async def on_trade(self, trade: TradeEvent) -> None:
        key = (trade.venue, trade.symbol)
        self._book(*key)
        self.flows[key].add(trade.received_ts, trade.aggressor, trade.notional)
        await self._record_latency(key, trade.exchange_ts, trade.received_ts)

    # -- state assembly ----------------------------------------------------

    def _quality(self, book: LocalOrderBook, now: Millis) -> DataQuality:
        """FRESH requires the data to be both recently *received* and recently
        *observed at the exchange* — either one alone is not enough.

        A feed delivering packets on schedule that describe a five-second-old
        market looked FRESH before this fix, because only local receipt time
        was checked (TIDAL-H3). A feed whose exchange timestamp cannot be
        trusted — because it implausibly leads local receipt time — is
        treated the same as one with no timestamp at all: UNAVAILABLE, not a
        best-effort guess (TIDAL-H3 clock skew).
        """
        if not book.synced or book.crossed or not book.bids or not book.asks:
            return DataQuality.UNAVAILABLE
        # Fail-closed by default (TIDAL-L1): a venue this dict has never heard
        # from is not "assumed connected until proven otherwise" -- it is
        # UNAVAILABLE until something actually established that lifecycle
        # (VENUE_CONNECTED, or any accepted snapshot -- see on_snapshot's
        # setdefault). The published ``VenueMarketState.connected`` field
        # already used this default; this only makes the internal quality
        # gate agree with it instead of silently trusting the unknown case.
        if not self.connected.get(book.venue, False):
            return DataQuality.UNAVAILABLE
        if book.last_update_ts is None or book.exchange_ts is None:
            return DataQuality.UNAVAILABLE

        # received_ts - exchange_ts for the last accepted message. Negative
        # beyond tolerance means exchange_ts is implausibly ahead of receipt:
        # the age figures below would be computed from a timestamp we cannot
        # trust, so this fails closed rather than reporting a number derived
        # from a broken clock.
        skew = book.last_update_ts - book.exchange_ts
        if -skew > self.settings.risk.max_clock_skew_ms:
            return DataQuality.UNAVAILABLE

        limit = self.settings.risk.max_data_age_ms
        local_silence_age = now - book.last_update_ts
        exchange_age = now - book.exchange_ts
        if local_silence_age > limit * 3 or exchange_age > limit * 3:
            return DataQuality.STALE
        if local_silence_age > limit or exchange_age > limit:
            return DataQuality.DEGRADED
        return DataQuality.FRESH

    def venue_state(
        self, venue: str, symbol: str, now_ms: Millis | None = None
    ) -> VenueMarketState | None:
        """Build one venue's state as of ``now_ms`` (default: the clock).

        ``now_ms`` exists so an entire :meth:`build_state` snapshot shares ONE
        instant (Phase 2 Batch 1.4 -- P2-15). ``as_of`` and the freshness
        classification are both derived from it, and a snapshot whose venues
        were aged against different instants is a snapshot replay cannot
        reproduce.
        """
        key = (venue, symbol)
        book = self.books.get(key)
        if book is None:
            return None
        now = self.clock.now_ms() if now_ms is None else now_ms
        metrics = compute_metrics(book, now, self.flows.get(key), self.mids.get(key))
        return VenueMarketState(
            venue=venue,
            symbol=symbol,
            metrics=metrics,
            book=book.snapshot(now) if book.bids or book.asks else None,
            exchange_ts=book.exchange_ts,
            last_update_ts=book.last_update_ts,
            as_of=now,
            quality=self._quality(book, now),
            latency_ms=self.latency_ms.get(key),
            clock_skew_ms=self.clock_skew_ms.get(key),
            connected=self.connected.get(venue, False),
            sequence_gaps=book.sequence_gaps,
            reconnects=self.reconnects.get(venue, 0),
        )

    def consolidate(
        self,
        symbol: str,
        states: list[VenueMarketState],
        now_ms: Millis | None = None,
    ) -> ConsolidatedView:
        """Cross-venue view.

        The reference here is a *depth-weighted mid*, deliberately simple: it
        exists so the dashboard and the detector have a stable anchor.  NORO
        owns the economically reasoned fair value.

        ``best_bid``/``best_ask``/``best_bid_venue``/``best_ask_venue``/
        ``cross_venue_spread_bps`` describe a comparison *between* venues, so
        they require at least two usable ones to mean anything. With exactly
        one, the old code still populated them from that single venue's own
        touch — reporting its bid and its own ask as a "cross-venue spread"
        against itself, always zero-or-crossed and always false (TIDAL-M6).
        ``reference_price`` is left populated even with one venue: it is
        documented as a blended reference, not a claim about two venues, so
        showing it for monitoring with a single contributor is truthful —
        the blend of one input is just that input.
        """
        now = self.clock.now_ms() if now_ms is None else now_ms
        usable = [s for s in states if s.quality.is_usable and s.metrics.mid is not None]
        view = ConsolidatedView(
            symbol=symbol,
            as_of=now,
            usable_venues=[s.venue for s in usable],
            quality=DataQuality.FRESH if len(usable) >= 2 else DataQuality.DEGRADED,
        )
        if not usable:
            view.quality = DataQuality.UNAVAILABLE
            return view

        weights = [
            max(1e-9, s.metrics.bid_depth_notional + s.metrics.ask_depth_notional)
            for s in usable
        ]
        view.reference_price = sum(
            s.metrics.mid * w for s, w in zip(usable, weights, strict=True)
        ) / sum(weights)

        if len(usable) >= 2:
            best_bid_state = max(usable, key=lambda s: s.metrics.best_bid or -float("inf"))
            best_ask_state = min(usable, key=lambda s: s.metrics.best_ask or float("inf"))
            view.best_bid = best_bid_state.metrics.best_bid
            view.best_bid_venue = best_bid_state.venue
            view.best_ask = best_ask_state.metrics.best_ask
            view.best_ask_venue = best_ask_state.venue
            if view.best_bid is not None and view.best_ask is not None:
                view.cross_venue_spread_bps = safe_bps(
                    view.best_bid - view.best_ask, view.reference_price
                )

        deviations = [
            (
                abs(
                    safe_bps(s.metrics.mid - view.reference_price, view.reference_price)
                    or 0.0
                ),
                s,
            )
            for s in usable
        ]
        worst, worst_state = max(deviations, key=lambda pair: pair[0])
        view.max_deviation_bps = worst
        view.max_deviation_venue = worst_state.venue
        return view

    def build_state(self, now_ms: Millis | None = None) -> MarketState:
        """Assemble the whole market snapshot at ONE instant.

        That instant becomes ``MarketState.created_at``, every
        ``VenueMarketState.as_of`` and every ``ConsolidatedView.as_of``, and
        -- because the orchestrator adopts it as the tick's canonical time
        (Phase 2 Batch 1.4 -- P2-15) -- the ``ORCHESTRATOR_TICK`` marker
        timestamp too. Replay pins its clock to that marker before re-running
        the tick, so this call then reads back exactly the same instant and
        reconstructs an identical snapshot. Reading the clock separately per
        venue, or letting the tick adopt a *later* time than the snapshot it
        is reasoning about, both break that: the age of the very same book
        would differ between the original run and its replay, and with it
        ``DataQuality`` -- FRESH in one, DEGRADED in the other.
        """
        now = self.clock.now_ms() if now_ms is None else now_ms
        venues: dict[str, VenueMarketState] = {}
        for (venue, symbol) in self.books:
            state = self.venue_state(venue, symbol, now)
            if state is not None:
                venues[f"{venue}:{symbol}"] = state
        # Consolidate every instrument actually being received, not only those
        # in the strategy universe. A venue book that exists but appears
        # nowhere in consolidated state is invisible to monitoring and to the
        # API, which is how a feed can look absent while running fine.
        #
        # Grouping is by exact canonical symbol, so BTC-USDT and BTC-USD form
        # two separate views. Nothing here computes a price across quote
        # assets, and a view with a single contributing venue is a normal,
        # honest result rather than something to fill in.
        consolidated: dict[str, ConsolidatedView] = {}
        for symbol in sorted({s.symbol for s in venues.values()}):
            states = [s for s in venues.values() if s.symbol == symbol]
            consolidated[symbol] = self.consolidate(symbol, states, now)
        newest = max(
            (s.last_update_ts for s in venues.values() if s.last_update_ts is not None),
            default=None,
        )
        state = MarketState(
            created_at=now,
            source_data_timestamp=newest,
            venues=venues,
            consolidated=consolidated,
        )
        self.state = state
        return state

    async def publish_state(self, now_ms: Millis | None = None) -> MarketState:
        state = self.build_state(now_ms)
        # Frozen here, at the exact moment book state was read for ``state``
        # -- not read later by the caller, since ``processed_input_sequence``
        # can keep advancing (e.g. during the orchestrator's own
        # post-snapshot ``bus.drain()``) without that later advance being
        # reflected in the ``state`` already computed above.
        self.last_snapshot_input_sequence = self.processed_input_sequence
        await self.bus.publish(
            Event(
                type=EventType.MARKET_STATE,
                ts_ms=state.created_at,
                source=SERVICE,
                schema_name="MarketState",
                payload=state.to_json_dict(),
            )
        )
        self._heartbeat(state)
        return state

    # -- opinion -----------------------------------------------------------

    def microstructure_metrics(self, venue: str, symbol: str) -> BookMetrics | None:
        state = self.venue_state(venue, symbol)
        return state.metrics if state is not None else None

    def evaluate(
        self, opportunity: Opportunity, now_ms: Millis
    ) -> AgentOpinion | None:
        """Score an opportunity on microstructure alone.

        TIDAL is not valuing the asset and not costing the trade — the other
        agents do that.  It answers a narrower question: does the *shape of
        the book* support each leg, or is the dislocation an artefact of a
        thin, wide, fast-moving market?

        ``now_ms`` is the request's logical time -- the tick that asked for
        this opinion, carried on the OPPORTUNITY_DETECTED event -- never a
        clock read taken when this subscriber happened to be scheduled
        (Phase 2 Batch 1.4). The opinion's ``created_at``/``expires_at``
        decide its freshness at every later tick, so a live-clock read here
        would let an opinion outlive its replayed twin purely because
        dispatch was slower in one run than in the other.
        """
        now = now_ms
        supports: list[float] = []
        reasons: list[str] = []
        detail: dict[str, float | int | str | bool | None] = {}
        worst_quality_age = 0

        for leg in opportunity.legs:
            state = self.venue_state(leg.venue, leg.symbol)
            if state is None or not state.quality.is_usable:
                # No usable book on a leg means no opinion at all.
                return None
            metrics = state.metrics
            # Buying is supported by bid-side pressure, selling by ask-side.
            pressure = metrics.imbalance * leg.side.sign
            flow = metrics.trade_flow_imbalance * leg.side.sign
            supports.append(0.7 * pressure + 0.3 * flow)
            detail[f"imbalance_{leg.venue}"] = round(metrics.imbalance, 4)
            detail[f"spread_bps_{leg.venue}"] = round(metrics.spread_bps or 0.0, 3)
            detail[f"vol_bps_{leg.venue}"] = round(metrics.short_vol_bps, 3)
            worst_quality_age = max(worst_quality_age, state.age_ms or 0)

        if not supports:
            return None

        # A market moving faster than the edge is a market that will have moved
        # by the time the orders land.
        vol = max(
            (
                self.venue_state(leg.venue, leg.symbol).metrics.short_vol_bps
                for leg in opportunity.legs
                if self.venue_state(leg.venue, leg.symbol) is not None
            ),
            default=0.0,
        )
        vol_penalty = min(0.8, vol / max(1e-9, opportunity.gross_edge_bps) * 0.25)
        signal = max(-1.0, min(1.0, sum(supports) / len(supports) - vol_penalty))
        detail["vol_penalty"] = round(vol_penalty, 4)
        detail["max_data_age_ms"] = worst_quality_age

        reasons.append(
            "MICROSTRUCTURE_SUPPORTS" if signal > 0 else "MICROSTRUCTURE_UNSUPPORTIVE"
        )
        if vol_penalty > 0.3:
            reasons.append("VOLATILITY_EXCEEDS_EDGE")

        freshness = 1.0 - min(
            1.0, worst_quality_age / max(1, self.settings.risk.max_data_age_ms)
        )
        confidence = max(0.0, min(1.0, 0.35 + 0.55 * freshness))

        return AgentOpinion(
            agent_id=AgentId.TIDAL,
            symbol=opportunity.symbol,
            created_at=now,
            source_data_timestamp=self.state.source_data_timestamp if self.state else None,
            correlation_id=opportunity.opportunity_id,
            signal=signal,
            confidence=confidence,
            expires_at=now + self.settings.risk.max_data_age_ms,
            reason_codes=reasons,
            model_version=VERSION,
            detail=detail,
        )

    async def publish_opinion(self, opinion: AgentOpinion) -> None:
        await self.bus.publish(
            Event(
                type=EventType.AGENT_OPINION,
                ts_ms=opinion.created_at,
                source=SERVICE,
                schema_name="AgentOpinion",
                correlation_id=opinion.correlation_id,
                payload=opinion.to_json_dict(),
            )
        )

    def _heartbeat(self, state: MarketState) -> None:
        ages = [s.age_ms for s in state.venues.values() if s.age_ms is not None]
        usable = [s for s in state.venues.values() if s.quality.is_usable]
        status = HealthStatus.HEALTHY
        detail = ""
        if not state.venues:
            status = HealthStatus.OFFLINE
            detail = "no books"
        elif not usable:
            status = HealthStatus.OFFLINE
            detail = "no usable venue"
        elif len(usable) < len(state.venues):
            status = HealthStatus.DEGRADED
            detail = f"{len(state.venues) - len(usable)} venue/symbol pairs unusable"
        self.health.heartbeat(
            SERVICE,
            status=status,
            last_event_age_ms=min(ages) if ages else None,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )

    async def _publish_system_event(
        self, kind: str, severity: Severity, message: str, detail: dict
    ) -> None:
        await self.bus.publish(
            Event(
                type=EventType.SYSTEM_EVENT,
                ts_ms=self.clock.now_ms(),
                source=SERVICE,
                schema_name="SystemEvent",
                payload=SystemEvent(
                    created_at=self.clock.now_ms(),
                    kind=kind,
                    severity=severity,
                    component=SERVICE,
                    message=message,
                    detail=detail,
                ).to_json_dict(),
            )
        )


__all__ = ["SERVICE", "VERSION", "AgentId", "Tidal"]
