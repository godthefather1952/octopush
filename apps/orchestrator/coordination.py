"""The coordination registry — the platform's memory of what it did.

WHAT THIS IS
============
Phase 6 gave execution a registry. Phase 7 gave reconciliation one. Neither
answered the question that sits above both: *what did the orchestrator do this
tick, who did it ask, what came back, and which of those answers turned into
which order?*

:class:`CoordinationRegistry` is that memory. It holds tick records, consensus
request records, consensus evaluation records and decision traces, and it links
them to identifiers owned elsewhere.

WHAT THIS IS NOT
================
It is not a decision authority, and every design choice here defends that:

* **Nothing reads it.** The orchestrator writes to the registry beside the code
  that already decides; no branch anywhere consults what the registry holds.
  A registry a decision reads is a registry a decision can be broken by.
* **It recomputes nothing.** A consensus evaluation is built by
  ``consensus_evaluation_from_result`` from a ``ConsensusResult`` the engine
  already produced. ``allowed`` is passed in by whoever called
  ``entry_allowed``/``continuation_allowed``, never derived here from an
  agreement and a threshold.
* **It waits for nothing.** The ``ResponseBarrier`` decides who answered;
  :meth:`record_barrier_result` copies the answer down.
* **It raises nothing of its own.** :meth:`fail_tick` records that a tick
  raised; the exception itself still propagates untouched. A registry that
  swallowed an error to keep its own bookkeeping tidy would be deciding that
  the failure did not matter.
* **Its backend cannot stop it either.** The optional
  :class:`CoordinationStore` is written through best-effort: a failure there
  is logged and dropped, never raised at the caller. A backend nobody is
  required to configure must not become something the tick path is required
  to succeed at.

Every mutation takes ``now_ms`` from the caller. Nothing in this module reads a
clock, so a replayed session records the instants the original recorded.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.models.common import AgentId, Millis
from core.models.opportunity import StrategyState
from core.models.orchestration import (
    BarrierSnapshot,
    ConsensusEvaluationRecord,
    ConsensusPurpose,
    ConsensusRequestRecord,
    ConsensusRequestStatus,
    CoordinationMetrics,
    DecisionTrace,
    OpinionReference,
    OrchestrationPhase,
    OrchestrationPhaseRecord,
    OrchestrationTickRecord,
    OrchestrationTickStatus,
    consensus_evaluation_from_result,
)

log = logging.getLogger(__name__)


class CoordinationStore(ABC):
    """The persistence seam for coordination history.

    **No implementation exists, and none is built here.** In-memory is right
    for a paper session, and choosing a schema now would fix the shape of
    queries nobody has written yet.

    The seam is real rather than decorative: :class:`CoordinationRegistry`
    accepts one and writes through when present, so a later pass adds a class
    instead of restructuring the registry. Reads still come from memory —
    turning the registry into a cache needs an invalidation story, and that
    belongs with whoever implements the backend.
    """

    @abstractmethod
    def put_tick(self, record: OrchestrationTickRecord) -> None: ...

    @abstractmethod
    def put_consensus_request(self, record: ConsensusRequestRecord) -> None: ...

    @abstractmethod
    def put_consensus_evaluation(
        self, record: ConsensusEvaluationRecord
    ) -> None: ...

    @abstractmethod
    def put_trace(self, trace: DecisionTrace) -> None: ...


@dataclass
class CoordinationRegistry:
    """Ticks, consensus requests, evaluations and traces, in memory."""

    ticks: dict[str, OrchestrationTickRecord] = field(default_factory=dict)
    consensus_requests: dict[str, ConsensusRequestRecord] = field(
        default_factory=dict
    )
    consensus_evaluations: dict[str, ConsensusEvaluationRecord] = field(
        default_factory=dict
    )
    traces: dict[str, DecisionTrace] = field(default_factory=dict)

    #: Optional write-through persistence. See :class:`CoordinationStore`.
    store: CoordinationStore | None = None

    #: Ticks in the order they were opened, so "the last N ticks" is a slice
    #: rather than a sort over a dict whose keys carry no ordering.
    _tick_order: list[str] = field(default_factory=list)
    #: correlation_id -> request_id, for the barrier and consensus callbacks
    #: that only know the correlation id.
    _request_by_correlation: dict[str, str] = field(default_factory=dict)
    #: opportunity_id -> trace_id.
    _trace_by_opportunity: dict[str, str] = field(default_factory=dict)

    #: The tick currently open, if any.
    current_tick_id: str | None = None

    #: Lifetime counters, unaffected by any future compaction.
    ticks_started: int = 0
    ticks_completed: int = 0
    ticks_failed: int = 0
    requests_registered: int = 0
    requests_completed: int = 0
    requests_timed_out: int = 0
    agent_responses: int = 0
    evaluations_recorded: int = 0
    entry_consensus: int = 0
    continuation_consensus: int = 0
    entry_allowed: int = 0
    entry_blocked: int = 0
    continuation_allowed: int = 0
    continuation_blocked: int = 0
    traces_created: int = 0
    traces_closed: int = 0

    # ------------------------------------------------------------------
    # tick lifecycle
    # ------------------------------------------------------------------

    def begin_tick(
        self,
        now_ms: Millis,
        *,
        tick_number: int = 0,
        market_timestamp: Millis | None = None,
        source_data_timestamp: Millis | None = None,
        warming_up: bool = False,
    ) -> OrchestrationTickRecord:
        """Open a tick record.

        A previous tick still marked RUNNING is left exactly as it is. That
        state is evidence — it means a tick neither completed nor was recorded
        as failed — and quietly closing it here would erase the one trace of
        whatever went wrong.
        """
        record = OrchestrationTickRecord(
            tick_number=tick_number,
            created_at=now_ms,
            updated_at=now_ms,
            status=OrchestrationTickStatus.RUNNING,
            current_phase=OrchestrationPhase.IDLE,
            market_timestamp=market_timestamp,
            source_data_timestamp=source_data_timestamp,
            warming_up=warming_up,
        )
        self.ticks[record.tick_id] = record
        self._tick_order.append(record.tick_id)
        self.current_tick_id = record.tick_id
        self.ticks_started += 1
        self._persist_tick(record)
        return record

    def enter_phase(
        self, phase: OrchestrationPhase, now_ms: Millis, *, tick_id: str | None = None
    ) -> OrchestrationPhaseRecord | None:
        """Note that the tick has reached ``phase``.

        Returns ``None`` when there is no open tick. That is not an error: the
        orchestrator's control flow does not depend on the registry, and a
        metadata call that raised because bookkeeping was missing would let the
        record break the thing it records.

        Re-entering a phase already recorded for this tick returns the existing
        record rather than appending a second one.
        """
        record = self._tick(tick_id)
        if record is None:
            return None
        existing = record.phase_record(phase)
        if existing is not None:
            record.current_phase = phase
            record.updated_at = now_ms
            # Re-entry moves the current-phase pointer, so it is a change to a
            # persisted record even though no phase was appended. Returning
            # without writing through would leave a configured backend showing
            # a phase the tick has already left.
            self._persist_tick(record)
            return existing
        phase_record = OrchestrationPhaseRecord(phase=phase, started_at=now_ms)
        record.phases = [*record.phases, phase_record]
        record.current_phase = phase
        record.updated_at = now_ms
        self._persist_tick(record)
        return phase_record

    def complete_phase(
        self,
        phase: OrchestrationPhase,
        now_ms: Millis,
        *,
        tick_id: str | None = None,
        ok: bool = True,
        detail: str = "",
    ) -> OrchestrationPhaseRecord | None:
        """Close one phase. Returns ``None`` when it was never opened.

        The first close wins. A phase that already carries a completion
        instant is finished history: closing it again returns the record it
        already has without touching the instant, the verdict or the detail.
        A tick may legitimately re-enter a phase, and letting the second close
        overwrite the first would make the record claim the phase succeeded at
        an instant it did not, and hide whatever the first close recorded.
        """
        record = self._tick(tick_id)
        if record is None:
            return None
        phase_record = record.phase_record(phase)
        if phase_record is None:
            return None
        if phase_record.completed_at is not None:
            return phase_record
        phase_record.completed_at = now_ms
        phase_record.ok = ok
        if detail:
            phase_record.detail = detail
        record.updated_at = now_ms
        self._persist_tick(record)
        return phase_record

    def complete_tick(
        self, now_ms: Millis, *, tick_id: str | None = None
    ) -> OrchestrationTickRecord | None:
        """Close the tick as COMPLETE.

        A tick already marked FAILED stays FAILED. Nothing here may convert a
        recorded failure into a success.

        A tick already marked COMPLETE is left exactly as it was, and
        ``ticks_completed`` does not move. The same terminal fact arriving
        twice is one tick, not two: a lifetime counter that grew on a repeated
        close would report a session that ran more ticks than it did.
        """
        record = self._tick(tick_id)
        if record is None:
            return None
        if record.is_terminal:
            return record
        record.status = OrchestrationTickStatus.COMPLETE
        record.current_phase = OrchestrationPhase.IDLE
        record.completed_at = now_ms
        record.updated_at = now_ms
        self.ticks_completed += 1
        if tick_id is None or tick_id == self.current_tick_id:
            self.current_tick_id = None
        self._persist_tick(record)
        return record

    def fail_tick(
        self, now_ms: Millis, *, error: str = "", tick_id: str | None = None
    ) -> OrchestrationTickRecord | None:
        """Record that the tick raised.

        **The exception is not handled here and is not handled by the caller
        either.** The orchestrator records the failure and re-raises exactly
        what it caught: same exception, same type, no retry and no
        translation. A framework that turned a raised tick into a logged note
        would be making a recovery decision nobody asked it to make.

        A tick that already reached a terminal state keeps it, with its first
        error and its first completion instant. Recording the same failure
        twice is one failed tick, and a second call cannot rewrite the first
        error into a later one — the first is the one that ended the tick.
        A tick already COMPLETE stays COMPLETE for the same reason: history
        that can be overwritten afterwards is not history.
        """
        record = self._tick(tick_id)
        if record is None:
            return None
        if record.is_terminal:
            return record
        record.status = OrchestrationTickStatus.FAILED
        record.completed_at = now_ms
        record.updated_at = now_ms
        if error:
            record.error = error
        open_phase = record.phase_record(record.current_phase)
        if open_phase is not None and open_phase.completed_at is None:
            open_phase.completed_at = now_ms
            open_phase.ok = False
            if error and not open_phase.detail:
                open_phase.detail = error
        self.ticks_failed += 1
        if tick_id is None or tick_id == self.current_tick_id:
            self.current_tick_id = None
        self._persist_tick(record)
        return record

    def note_tick(
        self, note: str, *, tick_id: str | None = None, now_ms: Millis | None = None
    ) -> None:
        """Attach a free-text note to the open tick."""
        record = self._tick(tick_id)
        if record is None or not note:
            return
        record.notes = [*record.notes, note]
        if now_ms is not None:
            record.updated_at = now_ms
        self._persist_tick(record)

    def count_tick(
        self,
        *,
        tick_id: str | None = None,
        opportunities_seen: int = 0,
        opportunities_created: int = 0,
        consensus_requests: int = 0,
        risk_evaluations: int = 0,
        execution_plans: int = 0,
        now_ms: Millis | None = None,
    ) -> None:
        """Add to the open tick's counters. Counting only; nothing branches."""
        record = self._tick(tick_id)
        if record is None:
            return
        record.opportunities_seen += opportunities_seen
        record.opportunities_created += opportunities_created
        record.consensus_requests += consensus_requests
        record.risk_evaluations += risk_evaluations
        record.execution_plans += execution_plans
        if now_ms is not None:
            record.updated_at = now_ms
        self._persist_tick(record)

    # -- tick queries ------------------------------------------------------

    def get_tick(self, tick_id: str) -> OrchestrationTickRecord | None:
        return self.ticks.get(tick_id)

    def current_tick(self) -> OrchestrationTickRecord | None:
        if self.current_tick_id is None:
            return None
        return self.ticks.get(self.current_tick_id)

    def recent_ticks(self, limit: int = 20) -> list[OrchestrationTickRecord]:
        """The most recently opened ticks, newest last.

        Newest last because that is the order a reader follows a session in.
        """
        if limit <= 0:
            return []
        recent = self._tick_order[-limit:]
        return [self.ticks[tick_id] for tick_id in recent if tick_id in self.ticks]

    # ------------------------------------------------------------------
    # consensus coordination
    # ------------------------------------------------------------------

    def register_consensus_request(
        self,
        correlation_id: str,
        now_ms: Millis,
        *,
        purpose: ConsensusPurpose = ConsensusPurpose.ENTRY,
        required_agents: set[AgentId] | list[AgentId] | None = None,
        symbol: str = "",
        strategy: str = "",
        deadline_ms: Millis | None = None,
    ) -> ConsensusRequestRecord:
        """Open the record for one round of asking the agents.

        Called beside ``ResponseBarrier.expect``, never instead of it. The
        barrier remains the only thing that decides who answered; this records
        that the question was asked.

        A correlation id names one *round* of asking, not the opportunity's
        whole life. Re-registering while a round is still open — which the
        platform legitimately does when a wait is re-entered — returns the
        record already held. Registering after a round finished, or for a
        different purpose, opens a new record: an opportunity is asked about
        again on every tick it is held, and folding a continuation into the
        entry record would leave the record claiming an entry consensus was
        computed at exit time.

        The finished record is kept; only the correlation lookup moves to the
        new round.
        """
        existing_id = self._request_by_correlation.get(correlation_id)
        if existing_id is not None:
            existing = self.consensus_requests.get(existing_id)
            if (
                existing is not None
                and existing.purpose is purpose
                and existing.status
                in (
                    ConsensusRequestStatus.CREATED,
                    ConsensusRequestStatus.WAITING,
                    ConsensusRequestStatus.READY,
                )
            ):
                return existing

        required = sorted(required_agents or [])
        record = ConsensusRequestRecord(
            correlation_id=correlation_id,
            purpose=purpose,
            symbol=symbol,
            strategy=strategy,
            created_at=now_ms,
            updated_at=now_ms,
            deadline_ms=deadline_ms,
            required_agents=required,
            missing_agents=list(required),
            status=ConsensusRequestStatus.WAITING,
        )
        self.consensus_requests[record.request_id] = record
        self._request_by_correlation[correlation_id] = record.request_id
        self.requests_registered += 1
        if purpose is ConsensusPurpose.ENTRY:
            self.entry_consensus += 1
        else:
            self.continuation_consensus += 1
        self.count_tick(consensus_requests=1, now_ms=now_ms)
        self._persist_request(record)
        return record

    def record_barrier_result(
        self,
        correlation_id: str,
        now_ms: Millis,
        *,
        responded: set[AgentId] | list[AgentId] | None = None,
        required: set[AgentId] | list[AgentId] | None = None,
        missing: set[AgentId] | list[AgentId] | None = None,
        complete: bool | None = None,
        timed_out: bool = False,
        waited_ms: int = 0,
    ) -> ConsensusRequestRecord | None:
        """Copy down what the barrier reported.

        The arguments mirror ``BarrierResult`` field for field. The registry
        does not compute completeness, does not subtract sets to second-guess
        ``missing``, and does not wait: it writes what it was told.

        TIMED_OUT is a normal outcome, not a failure. The platform decides with
        what arrived, exactly as it did before this record existed.
        """
        record = self._request_for(correlation_id)
        if record is None:
            return None

        #: Captured before any mutation below, because the lifetime counter
        #: counts transitions into TIMED_OUT rather than observations of it.
        #: A wait can be reported more than once — re-entered, re-observed —
        #: and each report is the same timeout, not another one.
        was_timed_out = record.status is ConsensusRequestStatus.TIMED_OUT

        responded_list = sorted(responded or [])
        if required is not None:
            record.required_agents = sorted(required)
        if missing is not None:
            record.missing_agents = sorted(missing)
        else:
            record.missing_agents = sorted(
                set(record.required_agents) - set(responded_list)
            )
        self.agent_responses += max(
            0, len(responded_list) - len(record.responded_agents)
        )
        record.responded_agents = responded_list
        record.timed_out = timed_out
        record.waited_ms = waited_ms
        record.updated_at = now_ms

        is_complete = complete if complete is not None else not record.missing_agents
        if timed_out and not is_complete:
            record.status = ConsensusRequestStatus.TIMED_OUT
            if not was_timed_out:
                self.requests_timed_out += 1
        else:
            record.status = ConsensusRequestStatus.READY
        self._persist_request(record)
        return record

    def record_barrier_snapshot(
        self, snapshot: BarrierSnapshot, now_ms: Millis
    ) -> ConsensusRequestRecord | None:
        """Copy down a mid-flight barrier observation.

        Distinct from :meth:`record_barrier_result`: a snapshot is a look at a
        wait still in progress, so it never sets a terminal status.
        """
        record = self._request_for(snapshot.correlation_id)
        if record is None:
            return None
        record.required_agents = list(snapshot.required)
        record.responded_agents = list(snapshot.responded)
        record.missing_agents = list(snapshot.missing)
        record.updated_at = now_ms
        if record.status is ConsensusRequestStatus.CREATED:
            record.status = ConsensusRequestStatus.WAITING
        self._persist_request(record)
        return record

    def record_consensus(
        self,
        result,
        now_ms: Millis,
        *,
        purpose: ConsensusPurpose,
        correlation_id: str | None = None,
        required_agents: list[AgentId] | None = None,
        opinion_refs: list[OpinionReference] | None = None,
        entry_threshold: float | None = None,
        continuation_threshold: float | None = None,
        allowed: bool | None = None,
        opportunity_id: str | None = None,
    ) -> ConsensusEvaluationRecord:
        """Record a ``ConsensusResult`` the engine has already produced.

        ``result`` is a ``core.models.agent.ConsensusResult``; it is duck-typed
        here only so this module does not have to import the agent models to
        annotate a value it never inspects beyond copying.

        ``allowed`` is what ``ConsensusEngine.entry_allowed`` or
        ``continuation_allowed`` returned, passed down by the caller that
        invoked it. It is never inferred from ``agreement`` and a threshold —
        inferring it would make this a second, untested decision authority
        capable of disagreeing with the first.
        """
        link_correlation = correlation_id or getattr(result, "correlation_id", None)
        request = (
            self._request_for(link_correlation) if link_correlation else None
        )
        record = consensus_evaluation_from_result(
            result,
            purpose=purpose,
            now_ms=now_ms,
            request_id=request.request_id if request else None,
            required_agents=required_agents,
            opinion_refs=opinion_refs,
            entry_threshold=entry_threshold,
            continuation_threshold=continuation_threshold,
            allowed=allowed,
        )
        self.consensus_evaluations[record.evaluation_id] = record
        self.evaluations_recorded += 1

        if allowed is not None:
            if purpose is ConsensusPurpose.ENTRY:
                if allowed:
                    self.entry_allowed += 1
                else:
                    self.entry_blocked += 1
            elif allowed:
                self.continuation_allowed += 1
            else:
                self.continuation_blocked += 1

        if request is not None:
            was_completed = request.status is ConsensusRequestStatus.COMPLETED
            request.consensus_evaluation_id = record.evaluation_id
            request.status = ConsensusRequestStatus.COMPLETED
            request.updated_at = now_ms
            # Transitions, not observations — as with the timeout counter
            # above. A second evaluation against the same open round replaces
            # which evaluation the round points at; it does not mean the
            # platform asked and completed two rounds.
            if not was_completed:
                self.requests_completed += 1
            self._persist_request(request)

        if opportunity_id is not None:
            trace = self.traces.get(self._trace_by_opportunity.get(opportunity_id, ""))
            if trace is not None:
                trace.consensus_evaluation_ids = [
                    *trace.consensus_evaluation_ids,
                    record.evaluation_id,
                ]
                if request is not None and (
                    request.request_id not in trace.consensus_request_ids
                ):
                    trace.consensus_request_ids = [
                        *trace.consensus_request_ids,
                        request.request_id,
                    ]
                trace.updated_at = now_ms
                self._persist_trace(trace)

        self._persist_evaluation(record)
        return record

    def fail_consensus_request(
        self, correlation_id: str, now_ms: Millis, *, detail: str = ""
    ) -> ConsensusRequestRecord | None:
        """Mark a request FAILED. Nothing calls this automatically."""
        record = self._request_for(correlation_id)
        if record is None:
            return None
        record.status = ConsensusRequestStatus.FAILED
        record.updated_at = now_ms
        if detail:
            log.debug("consensus request %s failed: %s", record.request_id, detail)
        self._persist_request(record)
        return record

    # -- consensus queries -------------------------------------------------

    def get_consensus_request(self, request_id: str) -> ConsensusRequestRecord | None:
        return self.consensus_requests.get(request_id)

    def request_for_correlation(
        self, correlation_id: str
    ) -> ConsensusRequestRecord | None:
        return self._request_for(correlation_id)

    def get_consensus_evaluation(
        self, evaluation_id: str
    ) -> ConsensusEvaluationRecord | None:
        return self.consensus_evaluations.get(evaluation_id)

    def pending_requests(self) -> list[ConsensusRequestRecord]:
        """Requests that have not reached a terminal coordination state."""
        return [
            record
            for record in self.consensus_requests.values()
            if record.status
            in (
                ConsensusRequestStatus.CREATED,
                ConsensusRequestStatus.WAITING,
                ConsensusRequestStatus.READY,
            )
        ]

    # ------------------------------------------------------------------
    # decision traces
    # ------------------------------------------------------------------

    def trace_for_opportunity(
        self,
        opportunity_id: str,
        now_ms: Millis | None = None,
        *,
        create: bool = False,
        correlation_id: str | None = None,
        strategy: str = "",
        symbol: str = "",
    ) -> DecisionTrace | None:
        """The trace for one opportunity, optionally opening one.

        Reading never creates. ``create=True`` requires ``now_ms``, because a
        record whose creation instant was invented could not be replayed.
        """
        trace_id = self._trace_by_opportunity.get(opportunity_id)
        if trace_id is not None:
            return self.traces.get(trace_id)
        if not create:
            return None
        if now_ms is None:
            raise ValueError("creating a decision trace requires now_ms")
        trace = DecisionTrace(
            opportunity_id=opportunity_id,
            correlation_id=correlation_id,
            strategy=strategy,
            symbol=symbol,
            created_at=now_ms,
            updated_at=now_ms,
        )
        self.traces[trace.trace_id] = trace
        self._trace_by_opportunity[opportunity_id] = trace.trace_id
        self.traces_created += 1
        self._persist_trace(trace)
        return trace

    def get_trace(self, trace_id: str) -> DecisionTrace | None:
        return self.traces.get(trace_id)

    def link_consensus(
        self,
        opportunity_id: str,
        evaluation_id: str,
        now_ms: Millis,
        *,
        request_id: str | None = None,
    ) -> DecisionTrace | None:
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        if evaluation_id not in trace.consensus_evaluation_ids:
            trace.consensus_evaluation_ids = [
                *trace.consensus_evaluation_ids,
                evaluation_id,
            ]
        if request_id is not None and request_id not in trace.consensus_request_ids:
            trace.consensus_request_ids = [*trace.consensus_request_ids, request_id]
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_intent(
        self, opportunity_id: str, intent_id: str, now_ms: Millis
    ) -> DecisionTrace | None:
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        trace.intent_id = intent_id
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_risk_decision(
        self, opportunity_id: str, decision_id: str, now_ms: Millis
    ) -> DecisionTrace | None:
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        trace.risk_decision_id = decision_id
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_execution_plan(
        self,
        opportunity_id: str,
        plan_id: str,
        now_ms: Millis,
        *,
        order_ids: list[str] | None = None,
    ) -> DecisionTrace | None:
        """Link a plan by id. Phase 6's registry still owns the plan itself."""
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        if plan_id not in trace.execution_plan_ids:
            trace.execution_plan_ids = [*trace.execution_plan_ids, plan_id]
        for order_id in order_ids or []:
            if order_id not in trace.order_ids:
                trace.order_ids = [*trace.order_ids, order_id]
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_orders(
        self, opportunity_id: str, order_ids: list[str], now_ms: Millis
    ) -> DecisionTrace | None:
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        for order_id in order_ids:
            if order_id not in trace.order_ids:
                trace.order_ids = [*trace.order_ids, order_id]
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_reconciliation_run(
        self, opportunity_id: str, run_id: str, now_ms: Millis
    ) -> DecisionTrace | None:
        """Link a reconciliation run to a trace.

        Nothing calls this from the tick path. Reconciliation is periodic and
        platform-wide rather than per-opportunity, so most traces carry none;
        inventing a relationship to fill the field would be worse than leaving
        it empty.
        """
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        if run_id not in trace.reconciliation_run_ids:
            trace.reconciliation_run_ids = [*trace.reconciliation_run_ids, run_id]
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def link_trade_ref(
        self, opportunity_id: str, trade_ref: str, now_ms: Millis
    ) -> DecisionTrace | None:
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        trace.trade_ref = trade_ref
        trace.updated_at = now_ms
        self._persist_trace(trace)
        return trace

    def update_trace_state(
        self,
        opportunity_id: str,
        state: StrategyState | None,
        now_ms: Millis,
        *,
        rejected_reason: str | None = None,
    ) -> DecisionTrace | None:
        """Mirror the opportunity's state onto its trace.

        ``OpportunityRecord.state`` stays authoritative. This is a copy for
        readers of the trace, and a copy that disagrees with the record means
        the mirror is stale — never that the record is wrong.
        """
        trace = self.trace_for_opportunity(opportunity_id)
        if trace is None:
            return None
        was_closed = trace.is_closed
        trace.state = state
        if rejected_reason is not None:
            trace.rejected_reason = rejected_reason
        trace.updated_at = now_ms
        if trace.is_closed and not was_closed:
            self.traces_closed += 1
        self._persist_trace(trace)
        return trace

    # ------------------------------------------------------------------
    # metrics and retention
    # ------------------------------------------------------------------

    def metrics(self, *, late_agent_responses: int = 0) -> CoordinationMetrics:
        """Counters, for display. Nothing reads these to decide anything."""
        return CoordinationMetrics(
            ticks_started=self.ticks_started,
            ticks_completed=self.ticks_completed,
            ticks_failed=self.ticks_failed,
            consensus_requests=self.requests_registered,
            consensus_completed=self.requests_completed,
            consensus_timeouts=self.requests_timed_out,
            agent_responses=self.agent_responses,
            late_agent_responses=late_agent_responses,
            entry_consensus=self.entry_consensus,
            continuation_consensus=self.continuation_consensus,
            entry_allowed=self.entry_allowed,
            entry_blocked=self.entry_blocked,
            continuation_allowed=self.continuation_allowed,
            continuation_blocked=self.continuation_blocked,
            traces_created=self.traces_created,
            traces_closed=self.traces_closed,
        )

    def compact(
        self,
        *,
        keep_ticks: int | None = None,
        keep_open_traces: bool = True,
    ) -> int:
        """Release finished coordination records. Conservative by construction.

        With the defaults nothing is released. The framework supplies the hook
        and the safety rules; the policy belongs to a later pass that has
        measured what retention actually costs.

        Even when asked to release, the rules below are absolute:

        * the tick currently open is never released;
        * a tick that is not terminal is never released;
        * an open trace — one whose opportunity has not reached CLOSED or
          REJECTED — is never released;
        * a consensus request that has not reached a terminal coordination
          state is never released, and neither is the evaluation it points at.

        Dropping any of those is how a platform decides a question stopped
        existing because it stopped tracking the answer.

        Returns the number of tick records released.
        """
        if keep_ticks is None:
            return 0
        if keep_ticks < 0:
            raise ValueError("keep_ticks may not be negative")

        eligible = [
            tick_id
            for tick_id in self._tick_order
            if tick_id != self.current_tick_id
            and (record := self.ticks.get(tick_id)) is not None
            and record.is_terminal
        ]
        excess = len(eligible) - keep_ticks
        if excess <= 0:
            return 0

        doomed = eligible[:excess]
        for tick_id in doomed:
            self.ticks.pop(tick_id, None)
        self._tick_order = [
            tick_id for tick_id in self._tick_order if tick_id in self.ticks
        ]

        if not keep_open_traces:
            self._release_closed_traces()
        return len(doomed)

    def _release_closed_traces(self) -> None:
        """Release traces whose opportunity is closed or rejected.

        Only closed traces, and only their own consensus records. A trace still
        in flight, and every request still coordinating, stays resident.
        """
        for opportunity_id, trace_id in list(self._trace_by_opportunity.items()):
            trace = self.traces.get(trace_id)
            if trace is None or not trace.is_closed:
                continue
            for evaluation_id in trace.consensus_evaluation_ids:
                self.consensus_evaluations.pop(evaluation_id, None)
            for request_id in trace.consensus_request_ids:
                request = self.consensus_requests.get(request_id)
                if request is None:
                    continue
                if request.status in (
                    ConsensusRequestStatus.COMPLETED,
                    ConsensusRequestStatus.TIMED_OUT,
                    ConsensusRequestStatus.FAILED,
                ):
                    self.consensus_requests.pop(request_id, None)
                    # Only when the lookup still points at THIS round. A later
                    # round on the same correlation id owns the mapping now,
                    # and dropping it would orphan a live request.
                    if (
                        self._request_by_correlation.get(request.correlation_id)
                        == request_id
                    ):
                        self._request_by_correlation.pop(request.correlation_id, None)
            self.traces.pop(trace_id, None)
            self._trace_by_opportunity.pop(opportunity_id, None)

    @property
    def resident_ticks(self) -> int:
        return len(self.ticks)

    @property
    def resident_traces(self) -> int:
        return len(self.traces)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _tick(self, tick_id: str | None) -> OrchestrationTickRecord | None:
        target = tick_id if tick_id is not None else self.current_tick_id
        if target is None:
            return None
        return self.ticks.get(target)

    def _request_for(
        self, correlation_id: str | None
    ) -> ConsensusRequestRecord | None:
        if correlation_id is None:
            return None
        request_id = self._request_by_correlation.get(correlation_id)
        if request_id is None:
            return None
        return self.consensus_requests.get(request_id)

    # -- write-through --------------------------------------------------
    #
    # Every helper below is best-effort by construction. A backend that is
    # optional to configure cannot be mandatory to succeed: if writing history
    # could raise into the tick path, an observability dependency would have
    # become a trading dependency, and the platform would stop for a reason
    # that has nothing to do with the market.
    #
    # So a store failure is logged with its traceback and swallowed. Resident
    # truth is already committed by the time these run, so the in-memory
    # record stays correct and only the durable copy is behind.
    #
    # The swallowing matters most in the one place it is easiest to miss.
    # ``fail_tick`` runs inside the orchestrator's ``except`` block while the
    # original exception is still in flight. If persistence raised there, that
    # new error would replace the trading error on its way out, and the caller
    # would be told the store broke rather than that the tick did. Catching
    # here means the ``raise`` upstream re-raises exactly what it caught.

    def _persist_tick(self, record: OrchestrationTickRecord) -> None:
        if self.store is None:
            return
        try:
            self.store.put_tick(record)
        except Exception:
            log.exception(
                "coordination store failed to persist tick %s", record.tick_id
            )

    def _persist_request(self, record: ConsensusRequestRecord) -> None:
        if self.store is None:
            return
        try:
            self.store.put_consensus_request(record)
        except Exception:
            log.exception(
                "coordination store failed to persist consensus request %s",
                record.request_id,
            )

    def _persist_evaluation(self, record: ConsensusEvaluationRecord) -> None:
        if self.store is None:
            return
        try:
            self.store.put_consensus_evaluation(record)
        except Exception:
            log.exception(
                "coordination store failed to persist consensus evaluation %s",
                record.evaluation_id,
            )

    def _persist_trace(self, trace: DecisionTrace) -> None:
        if self.store is None:
            return
        try:
            self.store.put_trace(trace)
        except Exception:
            log.exception(
                "coordination store failed to persist decision trace %s",
                trace.trace_id,
            )


__all__ = ["CoordinationRegistry", "CoordinationStore"]
