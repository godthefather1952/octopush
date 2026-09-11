"""LUMEN — the Claude intelligence agent.

LUMEN reads unstructured information and returns structure.  It runs on the
slow intelligence loop, measured in seconds and events, never per market tick:
the fast loop must never wait on a model.

Its published opinion answers one question with a well-defined sign:

    How favourable is it, right now, to take short-horizon relative-value risk
    in this symbol?

Calm markets score near zero; an information shock, a spike in attention or a
sharp narrative change scores negative, because a dislocation during a
repricing event is more likely to be real information than noise.  The raw
sentiment, direction and attention readings ride along in ``detail`` for
attribution.

LUMEN is optional by construction.  It is not in ``required_agents``, so if
Claude is unavailable the platform loses one weighted input and nothing else.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from agents.lumen.provider import (
    IntelligenceProvider,
    IntelligenceRequest,
    IntelligenceResponse,
)
from agents.lumen.providers import IntelligenceProviderDirectory, describe_provider
from agents.lumen.registry import IntelligenceRegistry
from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.health import HealthRegistry
from core.models.agent import AgentOpinion
from core.models.common import AgentId, Millis
from core.models.intelligence import (
    EvidenceFreshness,
    IntelligenceAnalysisRecord,
    IntelligenceEvidence,
    IntelligenceEvidenceBundle,
    IntelligenceLoopSnapshot,
    IntelligenceLoopStatus,
    IntelligenceMarketContext,
    IntelligenceProviderDescriptor,
    IntelligenceReplayProvenance,
    IntelligenceSourceKind,
    LumenContextSnapshot,
    LumenReadiness,
    LumenSnapshot,
    PublishedOpinionRef,
)
from core.models.market import MarketState
from core.models.ops import HealthStatus

log = logging.getLogger(__name__)

SERVICE = "LUMEN"
VERSION = "lumen-0.1"

SYSTEM_PROMPT = (
    "You are the information-analysis agent of an automated paper-trading "
    "platform. You receive structured market context and any recent headlines "
    "or announcements. Return a structured read of the information environment: "
    "sentiment, how much attention the asset is receiving, whether an "
    "information shock has occurred, and the direction the news implies. "
    "You never see orders, balances or risk limits, and your output is one "
    "weighted input among several. Be calibrated: if nothing notable is "
    "happening, say so with low confidence rather than inventing a narrative."
)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "sentiment",
        "attention",
        "information_shock",
        "direction",
        "confidence",
        "ttl_seconds",
        "reason_codes",
    ],
    "properties": {
        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
        "attention": {"type": "number", "minimum": 0, "maximum": 1},
        "information_shock": {"type": "boolean"},
        "direction": {"type": "number", "minimum": -1, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ttl_seconds": {"type": "integer", "minimum": 5, "maximum": 900},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass
class NewsItem:
    """One piece of unstructured input."""

    headline: str
    source: str
    published_ms: int
    body: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "source": self.source,
            "published_ms": self.published_ms,
            "body": self.body[:1000],
        }


@dataclass
class Lumen:
    bus: EventBus
    clock: Clock
    settings: Settings
    health: HealthRegistry
    provider: IntelligenceProvider
    #: Injected by a news adapter. Empty is a valid state — LUMEN then reads
    #: only the market context it is given.
    headlines: list[NewsItem] = field(default_factory=list)
    market: MarketState | None = None
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_call_ms: int | None = None
    latencies_ms: list[float] = field(default_factory=list)
    #: Phase 10. Provenance for each pass of the slow loop: what LUMEN saw,
    #: what it asked, what came back, and which opinion that produced. Written
    #: to beside the code that already decides and read by nothing — whether an
    #: opinion is produced still depends only on the provider response and
    #: :meth:`_to_opinion`.
    intel_registry: IntelligenceRegistry = field(default_factory=IntelligenceRegistry)
    #: Phase 10. Provider metadata for display. ``build_provider`` still selects
    #: by configured name, and nothing here switches or fails over.
    provider_directory: IntelligenceProviderDirectory = field(
        default_factory=IntelligenceProviderDirectory
    )

    def __post_init__(self) -> None:
        self.health.register(SERVICE, VERSION)
        # Describe the provider wiring already chose. Metadata only: this
        # neither selects a provider nor touches a credential.
        self.provider_directory.register_provider(self.provider, active=True)

    # -- input -------------------------------------------------------------

    def on_market_state(self, state: MarketState) -> None:
        self.market = state

    def add_headline(self, item: NewsItem) -> None:
        self.headlines.append(item)
        if len(self.headlines) > 50:
            del self.headlines[:-50]

    def _context(
        self, symbol: str, *, now_ms: Millis | None = None
    ) -> dict[str, Any]:
        """The structured facts LUMEN is given.

        Deliberately *not* the same context the pricing agents receive: feeding
        every agent identical input defeats the point of independent analysis.
        LUMEN sees the information environment and only a coarse market summary.

        One logical instant governs both as_of_ms and the one-hour headline
        boundary. evaluate() supplies the instant it already sampled; direct
        callers get one clock read here.
        """
        now = self.clock.now_ms() if now_ms is None else now_ms
        payload: dict[str, Any] = {
            "symbol": symbol,
            "as_of_ms": now,
            "recent_headlines": [
                item.to_payload()
                for item in self.headlines[-10:]
                if item.published_ms >= now - 3_600_000
            ],
        }
        if self.market is not None:
            view = self.market.consolidated.get(symbol)
            states = self.market.states_for(symbol)
            payload["market_summary"] = {
                "reference_price": view.reference_price if view else None,
                "venues_quoting": len(states),
                "max_cross_venue_deviation_bps": view.max_deviation_bps if view else None,
                "short_vol_bps": max(
                    (s.metrics.short_vol_bps for s in states), default=0.0
                ),
            }
        return payload

    # -- the slow loop -----------------------------------------------------

    async def evaluate(self, symbol: str) -> AgentOpinion | None:
        """Ask the provider for a read. Returns ``None`` when unavailable."""
        self.calls += 1
        self.last_call_ms = self.clock.now_ms()
        # The request is built exactly as before and handed to the provider
        # unchanged. It is bound to a local first only so the provenance record
        # can be built from the very payload that was sent -- calling
        # ``_context`` a second time would read the clock again and could
        # describe a different ``as_of_ms`` than the provider actually saw.
        request = IntelligenceRequest(
            task="information_environment",
            system=SYSTEM_PROMPT,
            payload=self._context(symbol, now_ms=self.last_call_ms),
            response_schema=RESPONSE_SCHEMA,
            max_tokens=self.settings.lumen.max_tokens,
            timeout_s=self.settings.lumen.timeout_s,
        )
        analysis = self._begin_analysis(symbol, request)
        # ONE call. Provenance never costs a second request: against a
        # non-deterministic model a second call would record a different
        # answer than the one the platform used.
        response = await self.provider.analyze(request)
        self.latencies_ms.append(response.latency_ms)
        if len(self.latencies_ms) > 200:
            del self.latencies_ms[:100]

        self._record_response(analysis, response)

        if not response.ok:
            self.failures += 1
            self.consecutive_failures += 1
            self._heartbeat()
            # No opinion is published. The orchestrator sees LUMEN missing,
            # which is honest; it does not see a neutral opinion, which would
            # be a lie.
            return None

        self.consecutive_failures = 0
        schema_error = self._response_schema_error(response.data)
        if schema_error is not None:
            log.warning(
                "LUMEN response violated schema",
                extra={"error": schema_error},
            )
            self.failures += 1
            self._heartbeat()
            self.intel_registry.mark_malformed(
                analysis.analysis_id,
                self._logical_now(),
                error=schema_error,
            )
            return None

        opinion = self._to_opinion(symbol, response.data)
        self._heartbeat()
        self._record_outcome(analysis, opinion)
        return opinion

    @staticmethod
    def _response_schema_error(data: dict[str, Any]) -> str | None:
        """Validate provider data against the authoritative RESPONSE_SCHEMA."""
        required = (
            "sentiment",
            "attention",
            "information_shock",
            "direction",
            "confidence",
            "ttl_seconds",
            "reason_codes",
        )
        missing = [name for name in required if name not in data]
        if missing:
            return f"missing required fields: {', '.join(missing)}"

        def finite_number(name: str, lower: float, upper: float) -> str | None:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{name} must be a number"
            numeric = float(value)
            if not math.isfinite(numeric) or not lower <= numeric <= upper:
                return f"{name} must be between {lower:g} and {upper:g}"
            return None

        for name, lower, upper in (
            ("sentiment", -1.0, 1.0),
            ("attention", 0.0, 1.0),
            ("direction", -1.0, 1.0),
            ("confidence", 0.0, 1.0),
        ):
            error = finite_number(name, lower, upper)
            if error is not None:
                return error

        if not isinstance(data.get("information_shock"), bool):
            return "information_shock must be a boolean"

        ttl = data.get("ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 5 <= ttl <= 900:
            return "ttl_seconds must be an integer between 5 and 900"

        reason_codes = data.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(code, str) for code in reason_codes
        ):
            return "reason_codes must be an array of strings"

        return None

    def _to_opinion(self, symbol: str, data: dict[str, Any]) -> AgentOpinion | None:
        try:
            sentiment = float(data["sentiment"])
            attention = float(data["attention"])
            shock = bool(data["information_shock"])
            direction = float(data["direction"])
            confidence = float(data["confidence"])
            ttl_seconds = int(data.get("ttl_seconds", self.settings.lumen.ttl_s))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("LUMEN response malformed", extra={"error": str(exc)})
            self.failures += 1
            return None

        reason_codes = [str(code) for code in data.get("reason_codes", [])][:8]
        # Relative-value risk is unfavourable when the information environment
        # is moving: a shock, high attention, or a strong directional narrative
        # all argue that a cross-venue gap is repricing rather than noise.
        turbulence = min(
            1.0,
            0.6 * max(0.0, attention) + 0.4 * abs(direction) + (0.4 if shock else 0.0),
        )
        signal = max(-1.0, min(1.0, -turbulence))
        if shock and "NEGATIVE_INFORMATION_SHOCK" not in reason_codes and direction < 0:
            reason_codes.append("NEGATIVE_INFORMATION_SHOCK")
        if not shock and attention < 0.3:
            reason_codes.append("QUIET_INFORMATION_ENVIRONMENT")

        now = self.clock.now_ms()
        ttl_ms = max(5_000, min(900_000, ttl_seconds * 1000))
        return AgentOpinion(
            agent_id=AgentId.LUMEN,
            symbol=symbol,
            created_at=now,
            source_data_timestamp=(
                self.market.source_data_timestamp if self.market is not None else None
            ),
            signal=signal,
            confidence=max(0.0, min(1.0, confidence)),
            expires_at=now + ttl_ms,
            reason_codes=reason_codes,
            model_version=VERSION,
            detail={
                "sentiment": sentiment,
                "attention": attention,
                "direction": direction,
                "information_shock": shock,
                "turbulence": turbulence,
            },
        )

    async def run_once(self) -> list[AgentOpinion]:
        """One pass of the intelligence loop, over every configured symbol."""
        published: list[AgentOpinion] = []
        for symbol in self.settings.symbols:
            opinion = await self.evaluate(symbol)
            if opinion is None:
                continue
            await self.bus.publish(
                Event(
                    type=EventType.AGENT_OPINION,
                    ts_ms=opinion.created_at,
                    source=SERVICE,
                    schema_name="AgentOpinion",
                    payload=opinion.to_json_dict(),
                )
            )
            self._record_publication(opinion)
            published.append(opinion)
        return published

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("LUMEN loop failed")
                self.failures += 1
            await self.clock.sleep(self.settings.lumen.poll_interval_s)

    # -- provenance (Phase 10) --------------------------------------------
    #
    # Everything below records or reports. The three ``_record``/``_begin``
    # helpers are writes whose results nothing branches on; the rest are reads
    # that nothing in the slow loop calls. Removing all of it would leave
    # LUMEN's behaviour identical.

    def _logical_now(self) -> Millis:
        """The instant the provenance records use.

        Reuses ``last_call_ms``, the clock read :meth:`evaluate` already made
        for this pass, rather than taking another. Phase 10 adds no clock read
        to LUMEN; the existing slow-loop clock behaviour is untouched and its
        logical-time correctness is validation work, not this phase's.
        """
        return self.last_call_ms if self.last_call_ms is not None else 0

    def _evidence_from_payload(
        self, symbol: str, payload: dict[str, Any], now_ms: Millis
    ) -> list[IntelligenceEvidence]:
        """Adapt the headlines that were actually sent into evidence.

        Built from ``request.payload`` — the object the provider received — so
        the record cannot describe a different input set than the one that was
        analysed. No filtering happens here: ``_context`` already applied
        LUMEN's own window (last ten, published within the hour) and a second
        filter that disagreed would misdescribe what the provider saw.

        ``freshness`` is left UNKNOWN rather than derived, for the same reason.
        """
        return [
            IntelligenceEvidence(
                kind=IntelligenceSourceKind.HEADLINE,
                source=str(item.get("source", "")),
                title=str(item.get("headline", "")),
                published_at=item.get("published_ms"),
                captured_at=now_ms,
                symbol=symbol,
                body_excerpt=str(item.get("body", ""))[:1000],
                freshness=EvidenceFreshness.UNKNOWN,
            )
            for item in payload.get("recent_headlines", [])
            if isinstance(item, dict)
        ]

    def _market_context_from_payload(
        self, symbol: str, payload: dict[str, Any], now_ms: Millis
    ) -> IntelligenceMarketContext | None:
        """Type the coarse market summary that was sent, if there was one."""
        summary = payload.get("market_summary")
        if not isinstance(summary, dict):
            return None
        return IntelligenceMarketContext(
            created_at=now_ms,
            symbol=symbol,
            reference_price=summary.get("reference_price"),
            venues_quoting=int(summary.get("venues_quoting", 0) or 0),
            max_cross_venue_deviation_bps=summary.get(
                "max_cross_venue_deviation_bps"
            ),
            short_vol_bps=float(summary.get("short_vol_bps", 0.0) or 0.0),
            source_data_timestamp=(
                self.market.source_data_timestamp if self.market is not None else None
            ),
        )

    def _begin_analysis(
        self, symbol: str, request: IntelligenceRequest
    ) -> IntelligenceAnalysisRecord:
        """Open the provenance record for one pass, from the request just built.

        Records the evidence bundle and a *summary* of the request. The system
        prompt and the response schema are module constants; storing either
        per call would make the record's size a function of how often the slow
        loop ran.
        """
        now = self._logical_now()
        bundle = self.intel_registry.register_evidence_bundle(
            symbol,
            now,
            evidence=self._evidence_from_payload(symbol, request.payload, now),
            market_context=self._market_context_from_payload(
                symbol, request.payload, now
            ),
            complete=True,
        )
        analysis = self.intel_registry.begin_analysis(
            symbol,
            now,
            task=request.task,
            provider=self.provider.name,
            model=getattr(self.provider, "model", None),
            evidence_bundle_id=bundle.bundle_id,
        )
        self.intel_registry.attach_request(
            analysis.analysis_id,
            now,
            task=request.task,
            provider=self.provider.name,
            model=getattr(self.provider, "model", None),
            max_tokens=request.max_tokens,
            timeout_s=request.timeout_s,
            payload_summary={
                "symbol": symbol,
                "headlines": str(len(bundle.evidence)),
                "market_summary": str("market_summary" in request.payload),
            },
            schema_name="lumen_information_environment",
        )
        return analysis

    def _record_response(
        self, analysis: IntelligenceAnalysisRecord, response: IntelligenceResponse
    ) -> None:
        """Mirror the outcome of the one call that was made.

        Runs before the failure branch in :meth:`evaluate` and changes none of
        its counters. ``unavailable`` and a plain failure are kept apart
        because the provider already keeps them apart: an outage is not a
        defect.
        """
        now = self._logical_now()
        self.intel_registry.attach_response(
            analysis.analysis_id,
            now,
            ok=response.ok,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            unavailable=response.unavailable,
            error=response.error or "",
            data=response.data,
        )
        if not response.ok:
            if response.unavailable:
                self.intel_registry.mark_unavailable(
                    analysis.analysis_id, now, error=response.error or ""
                )
            else:
                self.intel_registry.mark_failed(
                    analysis.analysis_id, now, error=response.error or ""
                )

    def _record_outcome(
        self, analysis: IntelligenceAnalysisRecord, opinion: AgentOpinion | None
    ) -> None:
        """Record what a successful call became.

        ``opinion is None`` here means ``_to_opinion`` found the payload
        unreadable — it has already logged, already counted a failure, and
        already published nothing. The registry marks the analysis malformed
        *after* that outcome. It never invents a neutral opinion to fill the
        gap: a fabricated neutral vote is a lie, and publishing nothing is the
        honest answer the platform already gives.
        """
        now = self._logical_now()
        if opinion is None:
            self.intel_registry.mark_malformed(
                analysis.analysis_id, now, error="response payload unreadable"
            )
            return
        self.intel_registry.complete_analysis(analysis.analysis_id, now)

    def _record_publication(self, opinion: AgentOpinion) -> None:
        """Mark publication only after the bus accepted the opinion event."""
        analysis = self.intel_registry.latest_for_symbol(opinion.symbol)
        if analysis is None:
            return
        self.intel_registry.link_opinion(
            analysis.analysis_id,
            PublishedOpinionRef(
                agent_id=opinion.agent_id,
                symbol=opinion.symbol,
                created_at=opinion.created_at,
                expires_at=opinion.expires_at,
                model_version=opinion.model_version,
                correlation_id=opinion.correlation_id,
                signal=opinion.signal,
                confidence=opinion.confidence,
            ),
            self._logical_now(),
        )

    # -- query surface (Phase 10) -----------------------------------------

    def analyses(self, limit: int = 20) -> list[IntelligenceAnalysisRecord]:
        """The most recent analyses, newest last."""
        return self.intel_registry.recent_analyses(limit)

    def analysis_for_id(self, analysis_id: str) -> IntelligenceAnalysisRecord | None:
        return self.intel_registry.get_analysis(analysis_id)

    def latest_analysis(self, symbol: str) -> IntelligenceAnalysisRecord | None:
        return self.intel_registry.latest_for_symbol(symbol)

    def evidence_bundles(self, limit: int = 20) -> list[IntelligenceEvidenceBundle]:
        """The most recently captured evidence bundles."""
        return self.intel_registry.evidence_bundle_list(limit)

    def provider_descriptor(self) -> IntelligenceProviderDescriptor:
        """What provider is wired, and what it is like.

        Carries no API key, no header and no client object, and must never
        learn to.
        """
        return self.provider_directory.active() or describe_provider(self.provider)

    def context_snapshot(
        self, analysis_id: str
    ) -> LumenContextSnapshot | None:
        """What LUMEN saw for one analysis.

        References the stored bundle rather than re-assembling the payload, so
        it cannot describe a different input set than the provider was sent.
        """
        analysis = self.intel_registry.get_analysis(analysis_id)
        if analysis is None:
            return None
        bundle = (
            self.intel_registry.get_bundle(analysis.evidence_bundle_id)
            if analysis.evidence_bundle_id
            else None
        )
        return LumenContextSnapshot(
            created_at=analysis.created_at,
            symbol=analysis.symbol,
            analysis_id=analysis.analysis_id,
            evidence_bundle_id=analysis.evidence_bundle_id,
            market_context=bundle.market_context if bundle else None,
            headline_ids=[item.evidence_id for item in bundle.evidence]
            if bundle
            else [],
            headlines_in_scope=len(bundle.evidence) if bundle else 0,
        )

    def replay_provenance(
        self, analysis_id: str
    ) -> IntelligenceReplayProvenance | None:
        """How a recorded analysis behaves under replay.

        Always the same three answers, for every analysis this build produces:
        recorded, replayed as an external input, and **the provider is not
        reinvoked**. Replay republishes the recorded ``AGENT_OPINION`` events;
        it could not re-ask a non-deterministic model deterministically, and a
        replay that did would not be a replay of anything.

        This describes the replay engine. It does not configure it, and the
        replay engine is untouched by this phase.
        """
        analysis = self.intel_registry.get_analysis(analysis_id)
        if analysis is None:
            return None
        return IntelligenceReplayProvenance(
            analysis_id=analysis.analysis_id,
            opinion_reference=analysis.published_opinion_ref,
            provider=analysis.provider,
            model=analysis.model,
            recorded=analysis.published,
            replayed_as_external_input=True,
            provider_reinvoked=False,
        )

    def loop_snapshot(self, now_ms: Millis) -> IntelligenceLoopSnapshot:
        """Where the slow loop stands, derived from state that already exists.

        ``run_forever`` is untouched, so RUNNING and SLEEPING are not
        distinguishable from outside it — the status reported is DEGRADED when
        the provider has failed consecutively, IDLE before the first call, and
        SLEEPING between passes. Adding a status assignment inside the loop
        would be rewriting the loop, which this phase may not do.
        """
        interval_ms = int(self.settings.lumen.poll_interval_s * 1000)
        if self.last_call_ms is None:
            status = IntelligenceLoopStatus.IDLE
        elif self.consecutive_failures:
            status = IntelligenceLoopStatus.DEGRADED
        else:
            status = IntelligenceLoopStatus.SLEEPING
        return IntelligenceLoopSnapshot(
            created_at=now_ms,
            status=status,
            last_run_at=self.last_call_ms,
            next_run_due_at=(
                None if self.last_call_ms is None else self.last_call_ms + interval_ms
            ),
            poll_interval_s=self.settings.lumen.poll_interval_s,
            symbols_total=len(self.settings.symbols),
            symbols_processed=len(self.intel_registry.latest_analysis_ids()),
            analyses_started=self.intel_registry.analyses_started,
            analyses_completed=self.intel_registry.analyses_completed,
        )

    def readiness(self, now_ms: Millis) -> LumenReadiness:
        """Whether the intelligence layer is in a fit state. **Reporting only.**

        **LUMEN IS OPTIONAL**, and ``ready=False`` here must never stop
        trading. LUMEN is not in ``required_agents``, a missing LUMEN opinion
        does not make a consensus incomplete, and with the shipped
        ``NullProvider`` configuration this reports unready permanently while
        the platform trades normally — the designed state, not a fault.

        Provider health is separate and unchanged: :meth:`_heartbeat` still
        decides OFFLINE, DEGRADED and HEALTHY exactly as before.
        """
        descriptor = self.provider_descriptor()
        recent = self.intel_registry.recent_analyses(1)
        reasons: list[str] = []
        if not descriptor.configured:
            reasons.append("NO_PROVIDER_CONFIGURED")
        if self.consecutive_failures:
            reasons.append(f"CONSECUTIVE_FAILURES:{self.consecutive_failures}")
        if not self.headlines:
            reasons.append("NO_EVIDENCE")
        if self.market is None:
            reasons.append("NO_MARKET_CONTEXT")
        if not recent:
            reasons.append("NO_RECENT_ANALYSIS")

        return LumenReadiness(
            ready=not reasons,
            created_at=now_ms,
            provider_configured=descriptor.configured,
            provider_available=self.consecutive_failures == 0 and self.calls > 0,
            evidence_available=bool(self.headlines),
            market_context_available=self.market is not None,
            recent_analysis_available=bool(recent),
            consecutive_failures=self.consecutive_failures,
            optional=True,
            reason_codes=reasons,
            detail=f"provider={descriptor.name}",
        )

    def lumen_snapshot(self, now_ms: Millis) -> LumenSnapshot:
        """One serializable view of the intelligence layer.

        Compact metadata: counters, the provider descriptor, and *ids* for
        recent analyses. A snapshot embedding every analysis with its evidence
        would be sized by how much news the platform had ingested rather than
        by what is currently happening.
        """
        return LumenSnapshot(
            created_at=now_ms,
            provider=self.provider_descriptor(),
            last_call_ms=self.last_call_ms,
            calls=self.calls,
            failures=self.failures,
            consecutive_failures=self.consecutive_failures,
            mean_latency_ms=self.mean_latency_ms,
            headline_count=len(self.headlines),
            pending_analyses=len(self.intel_registry.pending()),
            recent_analysis_ids=[
                record.analysis_id for record in self.intel_registry.recent_analyses(10)
            ],
            latest_analysis_by_symbol=self.intel_registry.latest_analysis_ids(),
            latest_opinion_by_symbol=self.intel_registry.latest_opinions(),
            loop=self.loop_snapshot(now_ms),
            metrics=self.intel_registry.metrics(),
            readiness=self.readiness(now_ms),
        )

    @property
    def mean_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    def _heartbeat(self) -> None:
        threshold = self.settings.lumen.failure_threshold
        if self.consecutive_failures >= threshold:
            status = HealthStatus.OFFLINE
            detail = f"{self.consecutive_failures} consecutive provider failures"
        elif self.consecutive_failures:
            status = HealthStatus.DEGRADED
            detail = f"{self.consecutive_failures} recent provider failures"
        else:
            status = HealthStatus.HEALTHY
            detail = f"provider={self.provider.name}"
        self.health.heartbeat(
            SERVICE,
            status=status,
            queue_depth=self.bus.queue_depth,
            version=VERSION,
            detail=detail,
        )
