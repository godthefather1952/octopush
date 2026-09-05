"""NORO — valuation agent.

Answers one question about a proposed cross-venue trade: *relative to
independent market evidence, is this direction actually mispriced?*

The word that carries the weight is **independent**. A benchmark built from the
same two venues the detector picked confirms the detector by construction: the
buy venue is the cheapest ask and the sell venue the richest bid, so a fair
value formed from those two prices necessarily sits between them, and both legs
necessarily look correct. NORO judged its own evidence and unsurprisingly
agreed with it — in the Phase 3 audit, 82 out of 82 production entry votes were
positive, none of them informative.

So the benchmark is now built from the venues *not* participating in the
opportunity. When such venues exist, NORO can genuinely confirm or contradict.
When they do not — an ordinary two-venue market — it says exactly that, and
neither confirms nor contradicts.
"""

from __future__ import annotations

from agents.noro.fair_value import (
    FairValue,
    VenueValuation,
    build_contributors,
    valuation_from,
)
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, Millis, Side
from core.models.market import MarketState
from core.models.opportunity import Opportunity, OpportunityLeg
from core.models.ops import HealthStatus

SERVICE = "NORO"
#: Bumped from ``noro-0.1``: the signal's *meaning* changed. It is now measured
#: against a benchmark that excludes the venues under judgement, the weakest
#: leg governs it outright, and a two-venue market yields a neutral vote rather
#: than a guaranteed positive one. An 0.1 opinion and an 0.2 opinion carrying
#: the same number do not say the same thing.
VERSION = "noro-0.2"

#: The smallest number of usable cross-venue contributors that constitutes a
#: valuation at all. Below this there is a price, but no second opinion, and
#: nothing to call a cross-venue valuation.
MIN_VALUATION_CONTRIBUTORS = 2

#: The smallest contributor count that can produce an *informative* verdict:
#: both opportunity legs, plus at least one venue outside the opportunity to
#: judge them against.
MIN_INFORMATIVE_CONTRIBUTORS = 3

FAIR_VALUE_CONFIRMS_DISLOCATION = "FAIR_VALUE_CONFIRMS_DISLOCATION"
FAIR_VALUE_CONTRADICTS_DISLOCATION = "FAIR_VALUE_CONTRADICTS_DISLOCATION"
FAIR_VALUE_NEUTRAL_ON_DISLOCATION = "FAIR_VALUE_NEUTRAL_ON_DISLOCATION"
LEG_AGAINST_FAIR_VALUE = "LEG_AGAINST_FAIR_VALUE"
INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH = (
    "INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH"
)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class Noro:
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
        #: Symbol-wide diagnostic valuations, refreshed on every market state.
        #: Observability and health only — an opportunity is never judged
        #: against these, because they include the venues under judgement.
        self.fair_values: dict[str, FairValue] = {}
        self.evaluations = 0
        health.register(SERVICE, VERSION)

    def subscribe(self) -> None:
        self.bus.subscribe(
            self.on_event,
            types=[EventType.MARKET_STATE, EventType.OPPORTUNITY_DETECTED],
            name="noro",
        )

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.MARKET_STATE:
            self.on_market_state(MarketState.model_validate(event.payload))
        elif event.type is EventType.OPPORTUNITY_DETECTED:
            opinion = self.evaluate(
                Opportunity.model_validate(event.payload), event.ts_ms
            )
            if opinion is not None:
                await self.publish(opinion)

    def on_market_state(self, state: MarketState) -> None:
        """Refresh the diagnostic valuation for every symbol TIDAL can price.

        Recomputed from scratch each time: a contributor that has gone stale or
        disappeared must leave the valuation immediately, and a cached value
        from a previous tick is a stale price wearing a fresh timestamp.
        """
        self.market = state
        self.fair_values = {}
        for symbol in self.settings.symbols:
            contributors = build_contributors(
                symbol, state.states_for(symbol), self.settings.noro
            )
            fair = valuation_from(symbol, contributors)
            if fair is not None:
                self.fair_values[symbol] = fair
        self._heartbeat()

    def fair_value(self, symbol: str) -> FairValue | None:
        return self.fair_values.get(symbol)

    # -- evaluation --------------------------------------------------------

    def independent_valuation(
        self, opportunity: Opportunity, contributors: list[VenueValuation]
    ) -> FairValue | None:
        """Benchmark built from venues outside the opportunity.

        Both legs are then judged against the *same* benchmark, and neither
        venue helped build the one that judges it. This is strictly stronger
        than leaving out only the venue under judgement: with two venues,
        leave-one-out judges A against B and B against A, which is the same
        pair of prices the detector used and confirms it just as reliably.

        ``None`` when nothing independent remains. That is not an error and not
        a neutral benchmark — it is the absence of evidence, and the caller
        must say so rather than substitute a number.
        """
        participating = {leg.venue for leg in opportunity.legs}
        independent = [c for c in contributors if c.venue not in participating]
        if not independent:
            return None
        return valuation_from(opportunity.symbol, independent)

    def evaluate(
        self, opportunity: Opportunity, now_ms: Millis
    ) -> AgentOpinion | None:
        """Score the opportunity against independent valuation evidence.

        Returns ``None`` only when a valuation genuinely cannot be formed — the
        orchestrator then sees a *missing* required agent and suspends. Missing
        is not neutral, so the common case of "no independent evidence exists"
        is an explicit neutral opinion, not silence: silence would stop the
        whole strategy on every ordinary two-venue market.

        ``now_ms`` is the request's logical time -- the tick that asked for
        this opinion, carried on the OPPORTUNITY_DETECTED event -- never a
        clock read taken when this subscriber happened to be scheduled
        (Phase 2 Batch 1.4). The opinion's ``created_at``/``expires_at``
        decide its freshness at every later tick, so a live-clock read here
        would let an opinion outlive its replayed twin purely because
        dispatch was slower in one run than in the other.
        """
        market = self.market
        if market is None:
            return None
        config = self.settings.noro
        symbol = opportunity.symbol

        contributors = build_contributors(symbol, market.states_for(symbol), config)
        if not contributors:
            return None
        by_venue = {c.venue: c for c in contributors}

        # Every leg's own venue must be priceable, or NORO has no view of that
        # leg at all. That is a genuine inability to value, not an absence of
        # independent evidence, so it stays a missing opinion.
        legs = list(opportunity.legs)
        if not legs or any(leg.venue not in by_venue for leg in legs):
            return None

        # Freshness of exactly the data this conclusion rests on (TIDAL-H4):
        # every contributor whose price was read, which is the union of the
        # opportunity's own legs and the independent venues judging them. The
        # market-wide newest timestamp launders a stale contributor behind an
        # unrelated fresh venue. Unknown stays unknown: an opinion whose age
        # cannot be checked is not publishable.
        source_ts = market.source_data_timestamp_for(
            (c.venue, c.symbol) for c in contributors
        )
        if source_ts is None:
            return None

        benchmark = self.independent_valuation(opportunity, contributors)

        detail: dict[str, float | int | str | bool | None] = {
            "contributors": len(contributors),
            "contributor_venues": ",".join(c.venue for c in contributors),
            "near_touch_notional": round(
                sum(c.near_touch_notional for c in contributors), 2
            ),
            "saturation_bps": config.saturation_bps,
            "liquidity_window_bps": config.liquidity_window_bps,
        }
        for contributor in contributors:
            detail[f"price_{contributor.venue}"] = contributor.price
            detail[f"reliability_{contributor.venue}"] = round(
                contributor.reliability, 4
            )

        if benchmark is None:
            signal, confidence, reasons = self._no_independent_evidence(detail)
        else:
            scored = self._score_against(benchmark, legs, by_venue, detail)
            if scored is None:
                return None
            signal, confidence, reasons = scored

        self.evaluations += 1
        return AgentOpinion(
            agent_id=AgentId.NORO,
            symbol=symbol,
            created_at=now_ms,
            source_data_timestamp=source_ts,
            correlation_id=opportunity.opportunity_id,
            signal=signal,
            confidence=confidence,
            expires_at=now_ms + config.ttl_ms,
            reason_codes=reasons,
            model_version=VERSION,
            detail=detail,
        )

    def _no_independent_evidence(
        self, detail: dict[str, float | int | str | bool | None]
    ) -> tuple[float, float, list[str]]:
        """Neither confirm nor contradict: there is nothing to judge against.

        Every usable venue for this symbol is one of the opportunity's own two,
        so any benchmark would be built from the very prices under judgement.
        NORO reports a neutral signal at low confidence, which lets TIDAL and
        ZEPHR carry the decision without NORO contributing a vote it has not
        earned. Confidence is low rather than zero because "0 signal, full
        confidence" would assert strong belief in neutrality, which is the
        opposite of what is being said.
        """
        config = self.settings.noro
        detail["independent_contributors"] = 0
        detail["independent_venues"] = ""
        detail["valuation_benchmark"] = None
        detail["weakest_confirmation_bps"] = None
        detail["valuation_dispersion_bps"] = None
        detail["confidence_breadth"] = 0.0
        detail["confidence_agreement"] = None
        detail["confidence_quality"] = None
        reasons = [INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH]
        return 0.0, config.insufficient_breadth_confidence, reasons

    def _score_against(
        self,
        benchmark: FairValue,
        legs: list[OpportunityLeg],
        by_venue: dict[str, VenueValuation],
        detail: dict[str, float | int | str | bool | None],
    ) -> tuple[float, float, list[str]] | None:
        """Confirm the opportunity against an independent benchmark.

        The weakest leg governs, outright:

            confirmed_edge_bps = min(per-leg confirmation)

        The previous formula was ``min(confirmations) + mean(confirmations)``,
        which let a strongly confirming leg buy off a contradicting one --
        ``[-1, +10]`` came out positive, so "both legs must agree" was not what
        the code did. Under ``min``, one negative leg makes the aggregate
        negative and one neutral leg caps it at neutral, with no compensation
        available.

        That also lets ``saturation_bps`` finally mean what it says: the
        aggregate is now a single leg's confirmation, so 15 bps of weakest-leg
        confirmation maps to a signal of +1, and -15 bps to -1.
        """
        config = self.settings.noro
        confirmations: list[float] = []
        for leg in legs:
            contributor = by_venue[leg.venue]
            deviation = benchmark.deviation_of(contributor.price)
            if deviation is None:
                return None
            detail[f"deviation_bps_{leg.venue}"] = round(deviation, 4)
            # Buying is confirmed where the venue trades below the independent
            # benchmark (negative deviation); selling where it trades above.
            confirmation = -deviation if leg.side is Side.BUY else deviation
            detail[f"confirmation_bps_{leg.venue}"] = round(confirmation, 4)
            confirmations.append(confirmation)

        weakest = min(confirmations)
        signal = _clamp(weakest / config.saturation_bps)

        independent = benchmark.venues
        detail["independent_contributors"] = len(independent)
        detail["independent_venues"] = ",".join(c.venue for c in independent)
        detail["valuation_benchmark"] = benchmark.fair_value
        detail["weakest_confirmation_bps"] = round(weakest, 4)
        detail["valuation_dispersion_bps"] = round(benchmark.dispersion_bps, 4)

        confidence = self._confidence(benchmark, detail)

        reasons: list[str] = []
        if weakest > 0:
            reasons.append(FAIR_VALUE_CONFIRMS_DISLOCATION)
        elif weakest < 0:
            reasons.append(FAIR_VALUE_CONTRADICTS_DISLOCATION)
            reasons.append(LEG_AGAINST_FAIR_VALUE)
        else:
            reasons.append(FAIR_VALUE_NEUTRAL_ON_DISLOCATION)
        return signal, confidence, reasons

    def _confidence(
        self,
        benchmark: FairValue,
        detail: dict[str, float | int | str | bool | None],
    ) -> float:
        """How much NORO stands behind its own valuation.

        Three components, deterministically combined at configured weights that
        must sum to 1:

        * **breadth** ``1 - 1/(1 + n)`` over independent contributors. Zero at
          none, 0.5 at one, 0.667 at two, 0.75 at three -- diminishing, but it
          never stops rising. The old formula saturated at exactly two venues,
          so a fourth independent opinion was worth nothing.
        * **agreement** falls linearly from 1 to 0 as the widest contributor
          deviation approaches ``dispersion_tolerance_bps``. Venues that
          disagree that much are not describing one price, and the old formula
          did not look at disagreement at all.
        * **quality** is the mean reliability weight of the contributors.

        None of these measures how much could be traded. That is ZEPHR's
        question, and answering it here is what pinned confidence near 1.0 in
        production: a fixed $250k liquidity scale that every venue cleared by a
        wide margin, so the term carried no information.
        """
        config = self.settings.noro
        count = len(benchmark.venues)
        breadth = 1.0 - 1.0 / (1.0 + count)
        agreement = _clamp(
            1.0 - benchmark.dispersion_bps / config.dispersion_tolerance_bps, 0.0, 1.0
        )
        quality = _clamp(benchmark.mean_reliability, 0.0, 1.0)
        detail["confidence_breadth"] = round(breadth, 4)
        detail["confidence_agreement"] = round(agreement, 4)
        detail["confidence_quality"] = round(quality, 4)
        return _clamp(
            config.breadth_confidence_weight * breadth
            + config.agreement_confidence_weight * agreement
            + config.quality_confidence_weight * quality,
            0.0,
            1.0,
        )

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
        """Report *valuation readiness*, not merely "a price exists".

        A single contributor gives a price but no second opinion, and calling
        that "priced" reported NORO healthy on a market where no cross-venue
        valuation was possible at all. Readiness now means a symbol has at
        least :data:`MIN_VALUATION_CONTRIBUTORS` usable cross-venue
        contributors.

        The detail also names how many symbols reach
        :data:`MIN_INFORMATIVE_CONTRIBUTORS`, because a symbol sitting at
        exactly two contributors is one where NORO will only ever return the
        neutral verdict -- healthy, answering honestly, and carrying no
        information. An operator should be able to see that without reading
        opinions one by one.
        """
        expected = len(self.settings.symbols)
        ready = [
            fair
            for fair in self.fair_values.values()
            if fair.contributor_count >= MIN_VALUATION_CONTRIBUTORS
        ]
        informative = sum(
            1
            for fair in self.fair_values.values()
            if fair.contributor_count >= MIN_INFORMATIVE_CONTRIBUTORS
        )
        widest = max(
            (fair.contributor_count for fair in self.fair_values.values()), default=0
        )

        if not self.fair_values:
            status = HealthStatus.OFFLINE
            detail = "no usable market data"
        elif not ready:
            status = HealthStatus.DEGRADED
            detail = (
                f"0/{expected} symbols valuation-ready: no symbol has the "
                f"{MIN_VALUATION_CONTRIBUTORS} cross-venue contributors a "
                f"valuation needs (widest: {widest})"
            )
        else:
            status = HealthStatus.HEALTHY
            detail = (
                f"{len(ready)}/{expected} symbols valuation-ready, "
                f"{informative} with the {MIN_INFORMATIVE_CONTRIBUTORS} "
                "contributors needed to confirm or contradict an opportunity"
            )

        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
