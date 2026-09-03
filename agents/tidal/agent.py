"""TIDAL — market intelligence infrastructure.

TIDAL is the foundation: it owns every local order book, normalises venue
state, measures freshness and latency, and publishes the one
:class:`MarketState` everything downstream reads.  Nothing here is
probabilistic and nothing here calls a model.
"""

from __future__ import annotations

import logging

from agents.tidal.book import BookDesyncError, LocalOrderBook
from agents.tidal.metrics import MidWindow, TradeFlowWindow, compute_metrics
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
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
        #: Latency samples: received_ts - exchange_ts, exponentially smoothed.
        self.latency_ms: dict[tuple[str, str], float] = {}
        self.updates_since_checkpoint: dict[tuple[str, str], int] = {}
        self.desyncs = 0
        self.resync_requests = 0
        #: When a resync was last asked for, per book. A gap usually arrives as
        #: a burst — every subsequent delta hits the same unsynchronised book —
        #: and one request per message would be a request storm aimed at
        #: whoever owns the feed.
        self._resync_requested_ms: dict[tuple[str, str], Millis] = {}
        self.state: MarketState | None = None
        health.register(SERVICE, VERSION)

    #: Floor on the interval between resync requests for one book, in ms.
    resync_request_interval_ms: Millis = 5_000

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
            depth = self.settings.venue(venue).book_depth_levels if self._known(venue) else 25
            self.books[key] = LocalOrderBook(venue=venue, symbol=symbol, max_depth=depth)
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
            opinion = self.evaluate(Opportunity.model_validate(event.payload))
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

    def _record_latency(
        self, key: tuple[str, str], exchange_ts: Millis, received_ts: Millis
    ) -> None:
        sample = max(0.0, float(received_ts - exchange_ts))
        prior = self.latency_ms.get(key)
        self.latency_ms[key] = sample if prior is None else 0.8 * prior + 0.2 * sample

    async def on_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        key = (snapshot.venue, snapshot.symbol)
        book = self._book(*key)
        book.apply_snapshot(snapshot)
        self.updates_since_checkpoint[key] = 0
        self._record_latency(key, snapshot.exchange_ts, snapshot.received_ts)
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
        self.updates_since_checkpoint[key] = self.updates_since_checkpoint.get(key, 0) + 1
        self._record_latency(key, delta.exchange_ts, delta.received_ts)

    async def on_trade(self, trade: TradeEvent) -> None:
        key = (trade.venue, trade.symbol)
        self._book(*key)
        self.flows[key].add(trade.received_ts, trade.aggressor, trade.notional)
        self._record_latency(key, trade.exchange_ts, trade.received_ts)

    # -- state assembly ----------------------------------------------------

    def _quality(self, book: LocalOrderBook, now: Millis) -> DataQuality:
        if not book.synced or book.crossed or not book.bids or not book.asks:
            return DataQuality.UNAVAILABLE
        if not self.connected.get(book.venue, True):
            return DataQuality.UNAVAILABLE
        if book.last_update_ts is None:
            return DataQuality.UNAVAILABLE
        age = now - book.last_update_ts
        limit = self.settings.risk.max_data_age_ms
        if age > limit * 3:
            return DataQuality.STALE
        if age > limit:
            return DataQuality.DEGRADED
        return DataQuality.FRESH

    def venue_state(self, venue: str, symbol: str) -> VenueMarketState | None:
        key = (venue, symbol)
        book = self.books.get(key)
        if book is None:
            return None
        now = self.clock.now_ms()
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
            connected=self.connected.get(venue, False),
            sequence_gaps=book.sequence_gaps,
            reconnects=self.reconnects.get(venue, 0),
        )

    def consolidate(self, symbol: str, states: list[VenueMarketState]) -> ConsolidatedView:
        """Cross-venue view.

        The reference here is a *depth-weighted mid*, deliberately simple: it
        exists so the dashboard and the detector have a stable anchor.  NORO
        owns the economically reasoned fair value.
        """
        now = self.clock.now_ms()
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

    def build_state(self) -> MarketState:
        now = self.clock.now_ms()
        venues: dict[str, VenueMarketState] = {}
        for (venue, symbol) in self.books:
            state = self.venue_state(venue, symbol)
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
            consolidated[symbol] = self.consolidate(symbol, states)
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

    async def publish_state(self) -> MarketState:
        state = self.build_state()
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

    def evaluate(self, opportunity: Opportunity) -> AgentOpinion | None:
        """Score an opportunity on microstructure alone.

        TIDAL is not valuing the asset and not costing the trade — the other
        agents do that.  It answers a narrower question: does the *shape of
        the book* support each leg, or is the dislocation an artefact of a
        thin, wide, fast-moving market?
        """
        now = self.clock.now_ms()
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
