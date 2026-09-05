"""ZEPHR — liquidity and executability agent.

Prices the opportunity at several sizes against the real book, subtracts every
modelled cost, and reports the largest size that still carries edge.  Its
signal is how much net edge survives relative to what the strategy needs.

ZEPHR prices *taker* execution against a *touch-to-touch* gross edge, and both
halves of that sentence are contractual: the cross-venue detector quotes the
cheapest ask against the richest bid, which already prices both crossings, so
ZEPHR charges what happens beyond the touch and never the touch itself.  See
:class:`~execution.costs.EdgeFrame`.
"""

from __future__ import annotations

import math

from agents.zephr.liquidity import (
    INSUFFICIENT_DEPTH,
    NET_EDGE_BELOW_FLOOR,
    SizingCurve,
    build_sizing_curve,
)
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, Millis, Side
from core.models.market import MarketState, PriceLevel
from core.models.opportunity import Opportunity
from core.models.ops import HealthStatus
from execution.costs import EdgeFrame

SERVICE = "ZEPHR"
VERSION = "zephr-0.1"

#: The frame every opportunity reaching ZEPHR is quoted in. Cross-venue
#: dislocations are measured between the prices actually available to an
#: aggressor, so crossing the touch is already inside ``gross_edge_bps``.
GROSS_EDGE_FRAME = EdgeFrame.TOUCH_TO_TOUCH

#: Reason codes ZEPHR emits, so the set is enumerable from one place.
EDGE_SURVIVES_EXECUTION = "EDGE_SURVIVES_EXECUTION"
NO_ECONOMICAL_SIZE = "NO_ECONOMICAL_SIZE"
SIZE_CAPPED_BY_DEPTH = "SIZE_CAPPED_BY_DEPTH"
SIZE_CAPPED_BY_EDGE_DECAY = "SIZE_CAPPED_BY_EDGE_DECAY"
SIZE_CAPPED_BY_RISK_LIMIT = "SIZE_CAPPED_BY_RISK_LIMIT"


class Zephr:
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
        self.market: MarketState | None = None
        #: Latest curve per opportunity, consumed by the orchestrator when it
        #: sizes the trade intent.
        self.curves: dict[str, SizingCurve] = {}
        self.evaluations = 0
        health.register(SERVICE, VERSION)

    def subscribe(self) -> None:
        self.bus.subscribe(
            self.on_event,
            types=[EventType.MARKET_STATE, EventType.OPPORTUNITY_DETECTED],
            name="zephr",
        )

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.MARKET_STATE:
            self.market = MarketState.model_validate(event.payload)
            self._heartbeat()
        elif event.type is EventType.OPPORTUNITY_DETECTED:
            opinion = self.evaluate(
                Opportunity.model_validate(event.payload), event.ts_ms
            )
            if opinion is not None:
                await self.publish(opinion)

    # -- evaluation --------------------------------------------------------

    def _levels(self, venue: str, symbol: str, side: Side) -> list[PriceLevel]:
        """Book levels the given side of a trade would consume.

        A buyer consumes asks, a seller consumes bids.
        """
        if self.market is None:
            return []
        state = self.market.venue_state(venue, symbol)
        if state is None or state.book is None:
            return []
        return state.book.asks if side is Side.BUY else state.book.bids

    def build_curve(self, opportunity: Opportunity) -> SizingCurve | None:
        if self.market is None:
            return None
        leg_inputs = []
        for leg in opportunity.legs:
            state = self.market.venue_state(leg.venue, leg.symbol)
            if state is None or not state.quality.is_usable:
                return None
            levels = self._levels(leg.venue, leg.symbol, leg.side)
            if not levels:
                return None
            fees = self.settings.venue(leg.venue).fees
            leg_inputs.append((state, leg.side, levels, fees))
        if not leg_inputs:
            return None
        return build_sizing_curve(
            opportunity.symbol,
            opportunity.gross_edge_bps,
            leg_inputs,
            self.settings.zephr,
            frame=GROSS_EDGE_FRAME,
            # The stricter of ZEPHR's own floor and the risk limit that will
            # judge the resulting intent: a size RUNE will refuse is not an
            # executable size, and reporting it as one is how ZEPHR came to
            # advertise edge the platform could never act on.
            min_net_edge_bps=self.settings.zephr_min_net_edge_bps,
            min_notional=self.settings.risk.min_trade_notional,
            max_notional=self.settings.risk.max_order_notional,
        )

    def evaluate(
        self, opportunity: Opportunity, now_ms: Millis
    ) -> AgentOpinion | None:
        """Score the opportunity on its executable cost curve.

        ``now_ms`` is the request's logical time -- the tick that asked for
        this opinion, carried on the OPPORTUNITY_DETECTED event -- never a
        clock read taken when this subscriber happened to be scheduled
        (Phase 2 Batch 1.4). The opinion's ``created_at``/``expires_at``
        decide its freshness at every later tick, so a live-clock read here
        would let an opinion outlive its replayed twin purely because
        dispatch was slower in one run than in the other.
        """
        curve = self.build_curve(opportunity)
        if curve is None:
            # ZEPHR could not price the opportunity. The orchestrator sees a
            # missing required agent and suspends, rather than trading blind.
            return None

        # Freshness of *this opportunity's own legs*, not the market-wide
        # newest observation (TIDAL-H4). A fresh venue somewhere else in the
        # market says nothing about the two books this trade would actually
        # hit, and quoting the market's newest timestamp here would launder a
        # stale leg into a fresh-looking opinion. Unknown means unknown: if
        # any leg has no exchange observation behind it, ZEPHR reports no
        # opinion at all and the orchestrator suspends on a missing required
        # agent, rather than publishing an opinion whose age cannot be
        # checked.
        market = self.market
        if market is None:
            return None
        source_ts = market.source_data_timestamp_for(
            (leg.venue, leg.symbol) for leg in opportunity.legs
        )
        if source_ts is None:
            return None

        self.curves[opportunity.opportunity_id] = curve
        self.evaluations += 1
        now = now_ms
        config = self.settings.zephr

        reasons: list[str] = []
        detail: dict[str, float | int | str | bool | None] = {
            "max_economical_notional": curve.max_economical_notional,
            "ladder_points": len(curve.points),
            "gross_edge_bps": round(opportunity.gross_edge_bps, 3),
            "gross_edge_frame": curve.frame.value,
            "min_net_edge_bps": curve.min_net_edge_bps,
            "min_trade_notional": curve.min_notional,
            "max_order_notional": self.settings.risk.max_order_notional,
            "hedge_bps": curve.hedge_bps,
        }
        for point in curve.points:
            rung = f"{point.notional:.0f}"
            detail[f"net_edge_bps@{rung}"] = round(point.net_edge_bps, 3)
            detail[f"status@{rung}"] = point.infeasible_reason or "FEASIBLE"

        if curve.best is None:
            signal = -1.0
            confidence = config.refusal_confidence
            reasons.append(NO_ECONOMICAL_SIZE)
            # The cheapest rung -- the one that came closest to working. Named
            # for what it is: it is the *best* case for this opportunity, and
            # calling it the worst inverted the reading of the number beside
            # it on the dashboard.
            cheapest = min(curve.points, key=lambda p: p.total_cost_bps, default=None)
            if cheapest is not None:
                detail["cheapest_cost_bps"] = round(cheapest.total_cost_bps, 3)
                detail["cheapest_notional"] = cheapest.notional
                detail["net_edge_at_min_size_bps"] = round(
                    curve.points[0].net_edge_bps, 3
                )
            # Every distinct reason a rung failed, in ladder order, so the
            # refusal says which constraint actually bound rather than
            # asserting a liquidity problem whatever the cause.
            for point in curve.points:
                if point.infeasible_reason and point.infeasible_reason not in reasons:
                    reasons.append(point.infeasible_reason)
        else:
            best = curve.best
            detail["chosen_notional"] = best.notional
            detail["expected_net_edge_bps"] = round(best.net_edge_bps, 3)
            detail["expected_cost_bps"] = round(best.total_cost_bps, 3)
            detail["expected_profit"] = round(best.expected_profit, 4)
            for leg in best.legs:
                detail[f"expected_price_{leg.venue}"] = leg.expected_price
                detail[f"touch_price_{leg.venue}"] = leg.touch_price
                detail[f"slippage_bps_{leg.venue}"] = round(leg.slippage_bps, 3)
                detail[f"fee_bps_{leg.venue}"] = round(leg.fee_bps, 3)
                detail[f"latency_bps_{leg.venue}"] = round(leg.latency_bps, 3)
                detail[f"liquidity_{leg.venue}"] = leg.liquidity.value
                # Reported, never charged in this frame: the gross edge was
                # measured touch to touch and already paid for the crossing.
                detail[f"quoted_half_spread_bps_{leg.venue}"] = round(
                    leg.quoted_half_spread_bps, 3
                )
                detail[f"spread_cost_bps_{leg.venue}"] = round(leg.spread_cost_bps, 3)
                detail[f"usable_liquidity_{leg.venue}"] = leg.usable_liquidity
                # Impact is unbounded when a book side is exhausted. The value
                # serialises as null, so state *why* rather than leaving a
                # bare null to be guessed at.
                detail[f"impact_status_{leg.venue}"] = (
                    "OK" if math.isfinite(leg.impact_bps) else "INSUFFICIENT_LIQUIDITY"
                )

            # The trade is only as executable as its thinnest leg: a deep buy
            # side cannot supply the sell side's missing depth. Taking leg
            # zero's liquidity read the first leg's book and reported it as
            # the whole trade's, so an opportunity with one deep venue and one
            # empty one came back nearly fully confident.
            bottleneck = min(leg.usable_liquidity for leg in best.legs)
            detail["bottleneck_liquidity"] = bottleneck

            floor = max(curve.min_net_edge_bps, 1e-9)
            saturation = floor * config.signal_saturation_multiple
            signal = min(1.0, best.net_edge_bps / saturation)
            credit = min(1.0, bottleneck / config.confidence_liquidity_saturation)
            confidence = min(
                1.0, config.confidence_floor + config.confidence_liquidity_weight * credit
            )
            reasons.append(EDGE_SURVIVES_EXECUTION)
            reasons.extend(self._size_cap_reasons(curve))

        return AgentOpinion(
            agent_id=AgentId.ZEPHR,
            symbol=opportunity.symbol,
            created_at=now,
            source_data_timestamp=source_ts,
            correlation_id=opportunity.opportunity_id,
            signal=signal,
            confidence=confidence,
            # ZEPHR's own TTL. It used to borrow NORO's, which tied how long
            # an execution quote stayed usable to a valuation setting:
            # retuning fair value silently retuned executability freshness,
            # and the two answer different questions over different horizons.
            expires_at=now + config.ttl_ms,
            reason_codes=reasons,
            model_version=VERSION,
            detail=detail,
        )

    def _size_cap_reasons(self, curve: SizingCurve) -> list[str]:
        """Why the chosen size is not the largest rung on the ladder.

        The old code reported ``SIZE_CAPPED_BY_LIQUIDITY`` whenever the chosen
        size was not the largest priced rung -- including the common case
        where every rung was perfectly executable and the size was chosen
        purely because net edge decays faster than notional grows. Those are
        different facts about the market and now carry different codes.
        """
        best = curve.best
        if best is None:
            return []
        larger = [p for p in curve.points if p.notional > best.notional]
        if not larger:
            # Nothing above the chosen size was priced at all. That is a cap
            # only if the ladder itself was truncated by the order limit.
            if best.notional >= self.settings.risk.max_order_notional:
                return [SIZE_CAPPED_BY_RISK_LIMIT]
            return []
        reasons: list[str] = []
        blocking = {p.infeasible_reason for p in larger}
        if INSUFFICIENT_DEPTH in blocking:
            reasons.append(SIZE_CAPPED_BY_DEPTH)
        if NET_EDGE_BELOW_FLOOR in blocking:
            reasons.append(SIZE_CAPPED_BY_EDGE_DECAY)
        # BELOW_MIN_TRADE_NOTIONAL cannot appear here: every rung in ``larger``
        # is bigger than the chosen one, which already cleared that floor.
        if None in blocking:
            # A larger rung was feasible but less profitable: net edge decays
            # with size, which is edge decay, not a liquidity problem.
            reasons.append(SIZE_CAPPED_BY_EDGE_DECAY)
        return sorted(set(reasons))

    def curve_for(self, opportunity_id: str) -> SizingCurve | None:
        return self.curves.get(opportunity_id)

    def forget(self, opportunity_id: str) -> None:
        self.curves.pop(opportunity_id, None)

    async def publish(self, opinion: AgentOpinion) -> None:
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

    def _heartbeat(self) -> None:
        """Report whether ZEPHR can actually price anything it is asked about.

        The question is per symbol, not per venue/symbol pair. A cross-venue
        opportunity needs two usable venues *for the same symbol*; counting
        usable pairs across the whole market let one usable venue on BTC-USD
        and one on ETH-USD add up to HEALTHY while ZEPHR could price neither
        symbol -- and ZEPHR is a required component, so a false HEALTHY there
        keeps the strategy running on an agent that will return no opinion.
        """
        usable = 0
        priceable: list[str] = []
        if self.market is not None:
            usable = sum(1 for s in self.market.venues.values() if s.quality.is_usable)
            for symbol in self.settings.symbols:
                venues = sum(
                    1 for s in self.market.states_for(symbol) if s.quality.is_usable
                )
                if venues >= 2:
                    priceable.append(symbol)

        expected = len(self.settings.symbols)
        if usable == 0:
            status = HealthStatus.OFFLINE
            detail = "no usable venue state"
        elif not priceable:
            status = HealthStatus.DEGRADED
            detail = (
                f"{usable} usable venue/symbol pairs, but no symbol has the two "
                "usable venues a cross-venue quote needs"
            )
        else:
            status = HealthStatus.HEALTHY
            detail = f"{len(priceable)}/{expected} symbols priceable on two venues"

        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
