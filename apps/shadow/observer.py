"""The shadow observer — it watches the bus, and does nothing else.

WHAT THIS IS
============
:class:`ShadowObserver` subscribes to events the platform already publishes and
writes what it learns into :class:`~apps.shadow.registry.ShadowRegistry`. That
is the whole of it.

WHAT IT MUST NOT DO, AND DOES NOT
=================================
It never calls RUNE, VESKA, ``PaperExecutor``, OKAPI or LUMEN. It never submits
an order, cancels one, touches the account, or moves an opportunity through its
state machine. It holds no reference to any of those components — not as a
matter of discipline but as a matter of construction: the only collaborators it
is given are an event bus to subscribe to and a registry to write to.

That matters more here than anywhere else in the platform. A shadow session
exists to observe what the platform would do; an observer that could influence
the run would be observing itself.

**No competing execution path.** Everything the observer records has already
happened, decided by the code that always decided it. It reads events after the
fact and copies identifiers down.

ACTIVATION
==========
The observer records only under ``OperationalProfile.SHADOW``. Under PAPER it
subscribes to nothing and accumulates nothing, so an ordinary paper session
does not grow a rehearsal history it will never read.

**That activation is observational, not economic.** No decision anywhere
differs because the observer is running. If it did, a shadow session would not
be rehearsing the platform — it would be rehearsing a different platform.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.shadow.registry import ShadowRegistry
from core.bus import EventBus
from core.events import Event, EventType
from core.models.common import Millis
from core.models.shadow import (
    ExecutionProvenance,
    ShadowDecisionStatus,
    ShadowMarketCheckpoint,
    ShadowOrderSummary,
    ShadowOutcomeCheckpoint,
)

log = logging.getLogger(__name__)

#: Which strategy states map to which rehearsal status. A copy of the
#: platform's own lifecycle, not a second one: the orchestrator's transition
#: has already happened by the time the event carrying it arrives.
_STATE_STATUS: dict[str, ShadowDecisionStatus] = {
    "AGENTS_EVALUATING": ShadowDecisionStatus.OBSERVED,
    "CONSENSUS_REACHED": ShadowDecisionStatus.CONSENSUS_RECORDED,
    "RISK_CHECK": ShadowDecisionStatus.CONSENSUS_RECORDED,
    "AUTHORIZED": ShadowDecisionStatus.AUTHORIZED,
    "EXECUTING": ShadowDecisionStatus.PAPER_WORKING,
    "WORKING": ShadowDecisionStatus.PAPER_WORKING,
    "EXITING": ShadowDecisionStatus.PAPER_WORKING,
    "CLOSED": ShadowDecisionStatus.CLOSED,
    "REJECTED": ShadowDecisionStatus.RISK_REJECTED,
}


class ShadowObserver:
    """Records the rehearsal from the bus. Decides nothing.

    Given only a bus and a registry, deliberately. There is no orchestrator
    reference, no executor reference and no account reference, so there is no
    path through this object to anything that trades.
    """

    def __init__(
        self,
        bus: EventBus,
        registry: ShadowRegistry,
        *,
        enabled: bool = False,
    ) -> None:
        self.bus = bus
        self.registry = registry
        #: True only under the SHADOW profile. See the module docstring.
        self.enabled = enabled
        self.events_seen = 0
        self.events_ignored = 0
        self._subscribed = False

    # -- wiring ------------------------------------------------------------

    def subscribe(self) -> None:
        """Attach to the events this observer reads.

        A no-op when disabled: under the PAPER profile the observer is not
        attached to the bus at all, so it costs nothing and accumulates
        nothing.

        Every subscription is read-only. None of these handlers publishes,
        replies, or acknowledges — a handler that published would put the
        observer into the very flow it is watching.
        """
        if self.enabled and not self._subscribed:
            self.bus.subscribe(
                self._on_event,
                types=[
                    EventType.OPPORTUNITY_DETECTED,
                    EventType.CONSENSUS_UPDATED,
                    EventType.TRADE_INTENT,
                    EventType.RISK_PASS,
                    EventType.RISK_FAIL,
                    EventType.EXECUTION_PLAN,
                    EventType.EXECUTION_REPORT,
                    EventType.PAPER_ORDER_CREATED,
                    EventType.PAPER_ORDER_UPDATED,
                    EventType.PAPER_FILL,
                    EventType.STRATEGY_STATE_CHANGED,
                    EventType.HEDGE_INTENT,
                    EventType.RECONCILIATION_COMPLETE,
                    EventType.RECONCILIATION_MISMATCH,
                ],
                name="shadow-observer",
            )
            self._subscribed = True

    # -- observation -------------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        """Route one event to its recorder.

        Never raises into the bus. A defect in bookkeeping must not take down
        the run it is bookkeeping for — the observer's failure mode is a gap in
        the record, which is visible, rather than a broken session, which is
        not what anyone asked for.
        """
        if not self.enabled:
            return
        self.events_seen += 1
        try:
            self._dispatch(event)
        except Exception:  # pragma: no cover - defensive, see docstring
            self.events_ignored += 1
            log.debug(
                "shadow observer could not record an event",
                extra={"type": event.type.value},
                exc_info=True,
            )

    def _dispatch(self, event: Event) -> None:
        handler = {
            EventType.OPPORTUNITY_DETECTED: self._on_opportunity,
            EventType.CONSENSUS_UPDATED: self._on_consensus,
            EventType.TRADE_INTENT: self._on_intent,
            EventType.RISK_PASS: self._on_risk,
            EventType.RISK_FAIL: self._on_risk,
            EventType.EXECUTION_PLAN: self._on_plan,
            EventType.EXECUTION_REPORT: self._on_report,
            EventType.PAPER_ORDER_CREATED: self._on_order,
            EventType.PAPER_ORDER_UPDATED: self._on_order,
            EventType.PAPER_FILL: self._on_fill,
            EventType.STRATEGY_STATE_CHANGED: self._on_state,
        }.get(event.type)
        if handler is None:
            # HEDGE_INTENT and the reconciliation events are subscribed so the
            # observer sees the whole shape of a session, but linking a hedge
            # or a run to a decision needs evidence the event does not carry.
            # An invented link is worse than none.
            self.events_ignored += 1
            return
        handler(event)

    def _decision_for(self, event: Event) -> Any:
        """The record this event belongs to, if one has been opened.

        Never creates. Only ``OPPORTUNITY_DETECTED`` opens a decision, because
        that is the one event that means "a new opportunity exists"; opening
        one from a downstream event would create records for hedges and exits
        that are not opportunities.
        """
        correlation = event.correlation_id
        if not correlation:
            return None
        return self.registry.for_opportunity(correlation)

    # -- handlers ----------------------------------------------------------

    def _on_opportunity(self, event: Event) -> None:
        payload = event.payload
        opportunity_id = payload.get("opportunity_id") or event.correlation_id
        if not opportunity_id:
            return
        self.registry.register_decision(
            opportunity_id,
            event.ts_ms,
            correlation_id=event.correlation_id,
            strategy=str(payload.get("strategy", "")),
            symbol=str(payload.get("symbol", "")),
        )

    def _on_consensus(self, event: Event) -> None:
        """Note that consensus was computed for this opportunity.

        The evaluation id lives in Phase 8's registry and is not on this event,
        so the link is made by whoever holds both — not guessed here. What this
        records is the status transition, which the platform has already made.
        """
        record = self._decision_for(event)
        if record is None:
            return
        self.registry.set_status(
            record.shadow_decision_id,
            ShadowDecisionStatus.CONSENSUS_RECORDED,
            event.ts_ms,
        )

    def _on_intent(self, event: Event) -> None:
        record = self._decision_for(event)
        if record is None:
            return
        intent_id = event.payload.get("intent_id")
        if intent_id:
            self.registry.link_intent(
                record.shadow_decision_id, str(intent_id), event.ts_ms
            )

    def _on_risk(self, event: Event) -> None:
        """Copy what RUNE answered.

        ``approved`` comes from the decision's own verdict field, as published.
        It is not inferred from which event type arrived, and not re-derived
        from the notionals — RUNE decides, and this copies.
        """
        record = self._decision_for(event)
        if record is None:
            return
        payload = event.payload
        decision_id = payload.get("decision_id")
        if not decision_id:
            return
        verdict = str(payload.get("verdict", "")).upper()
        approved = verdict == "APPROVED" if verdict else None
        self.registry.link_risk(
            record.shadow_decision_id,
            str(decision_id),
            event.ts_ms,
            approved=approved,
            requested_notional=float(payload.get("requested_notional", 0.0) or 0.0),
            approved_notional=float(payload.get("approved_notional", 0.0) or 0.0),
        )
        if approved is False:
            self.registry.set_status(
                record.shadow_decision_id,
                ShadowDecisionStatus.RISK_REJECTED,
                event.ts_ms,
            )

    def _on_plan(self, event: Event) -> None:
        record = self._decision_for(event)
        if record is None:
            return
        payload = event.payload
        plan_id = payload.get("plan_id")
        if not plan_id:
            return
        self.registry.link_plan(record.shadow_decision_id, str(plan_id), event.ts_ms)
        self.registry.register_execution(
            record.shadow_decision_id,
            event.ts_ms,
            plan_id=str(plan_id),
            planned_notional=float(payload.get("notional", 0.0) or 0.0),
        )
        self.registry.set_status(
            record.shadow_decision_id, ShadowDecisionStatus.PLANNED, event.ts_ms
        )

    def _on_report(self, event: Event) -> None:
        record = self._decision_for(event)
        if record is None:
            return
        order_ids = [
            str(o.get("client_order_id"))
            for o in event.payload.get("orders", [])
            if isinstance(o, dict) and o.get("client_order_id")
        ]
        if order_ids:
            self.registry.link_orders(
                record.shadow_decision_id, order_ids, event.ts_ms
            )

    def _on_order(self, event: Event) -> None:
        """Copy one paper order's current state onto its execution record.

        ``provenance`` is ``PAPER_SIMULATOR`` on every summary. These
        quantities and prices came from a fill simulator, not from a venue, and
        the field says so rather than leaving it to be inferred.
        """
        record = self._decision_for(event)
        if record is None:
            return
        payload = event.payload
        plan_id = payload.get("plan_id")
        executions = self.registry.execution_records(record.shadow_decision_id)
        execution = next(
            (e for e in executions if plan_id and e.plan_id == str(plan_id)),
            executions[-1] if executions else None,
        )
        if execution is None:
            return
        summary = ShadowOrderSummary(
            client_order_id=str(payload.get("client_order_id", "")),
            venue=str(payload.get("venue", "")),
            symbol=str(payload.get("symbol", "")),
            status=str(payload.get("status", "")),
            quantity=float(payload.get("quantity", 0.0) or 0.0),
            filled_quantity=float(payload.get("filled_quantity", 0.0) or 0.0),
            average_price=payload.get("average_price"),
            fees_paid=float(payload.get("fees_paid", 0.0) or 0.0),
            provenance=ExecutionProvenance.PAPER_SIMULATOR,
        )
        others = [
            o
            for o in execution.orders
            if o.client_order_id != summary.client_order_id
        ]
        self.registry.record_orders(
            execution.shadow_execution_id, [*others, summary], event.ts_ms
        )
        if str(payload.get("status", "")).upper() == "UNKNOWN":
            # UNKNOWN propagates upward and is never auto-resolved: the
            # rehearsal cannot claim to know what happened to an order the
            # platform cannot see.
            self.registry.set_status(
                record.shadow_decision_id,
                ShadowDecisionStatus.UNKNOWN,
                event.ts_ms,
                reason="ORDER_UNKNOWN",
            )

    def _on_fill(self, event: Event) -> None:
        """Copy a simulated fill.

        **This is not a real fill.** It is ``FillSimulator``'s estimate of what
        might have executed against a book nobody traded into, and every number
        here is copied from what ``PaperExecutor`` produced.
        """
        record = self._decision_for(event)
        if record is None:
            return
        executions = self.registry.execution_records(record.shadow_decision_id)
        if not executions:
            return
        execution = executions[-1]
        payload = event.payload
        fill_id = payload.get("fill_id") or payload.get("event_id")
        notional = float(payload.get("notional", 0.0) or 0.0)
        self.registry.link_fills(
            execution.shadow_execution_id,
            [str(fill_id)] if fill_id else [],
            event.ts_ms,
            paper_filled_notional=execution.paper_filled_notional + notional,
            paper_fees=execution.paper_fees + float(payload.get("fee", 0.0) or 0.0),
            paper_slippage_bps=float(payload.get("slippage_bps", 0.0) or 0.0),
        )

    def _on_state(self, event: Event) -> None:
        """Mirror the opportunity's own state onto the rehearsal record.

        ``OpportunityRecord.state`` stays authoritative. A mirror that
        disagrees is stale, never right — and an unmapped state is left alone
        rather than guessed at.
        """
        record = self._decision_for(event)
        if record is None:
            return
        detail = event.payload.get("detail") or {}
        target = str(detail.get("to", "")).upper()
        status = _STATE_STATUS.get(target)
        if status is None:
            return
        self.registry.set_status(record.shadow_decision_id, status, event.ts_ms)

    # -- explicit capture seams -------------------------------------------

    def capture_checkpoint(
        self,
        decision_id: str,
        market: Any,
        now_ms: Millis,
        *,
        horizon_ms: int | None = None,
    ) -> ShadowMarketCheckpoint | None:
        """Copy the market as it stands, against one decision.

        **Nothing schedules this.** No timer, no horizon policy, no default set
        of horizons — a caller captures when it wants to, and later validation
        decides which horizons are worth measuring.

        ``market`` is duck-typed as a ``MarketState``; the observer reads it
        and writes nothing back.
        """
        record = self.registry.get(decision_id)
        if record is None:
            return None
        touches: dict[str, list[float]] = {}
        reference: float | None = None
        try:
            for state in market.states_for(record.symbol):
                bid = state.metrics.best_bid
                ask = state.metrics.best_ask
                if bid is None or ask is None:
                    continue
                touches[f"{state.venue}:{state.symbol}"] = [float(bid), float(ask)]
            view = market.consolidated.get(record.symbol)
            reference = view.reference_price if view else None
        except AttributeError:  # pragma: no cover - defensive
            return None

        return self.registry.add_market_checkpoint(
            ShadowMarketCheckpoint(
                created_at=now_ms,
                decision_id=decision_id,
                symbol=record.symbol,
                reference_price=reference,
                venue_touches=touches,
                source_data_timestamp=getattr(market, "source_data_timestamp", None),
                horizon_ms=horizon_ms,
                market_data=self.registry.market_data,
            )
        )

    def capture_outcome(
        self,
        decision_id: str,
        now_ms: Millis,
        *,
        reference_price: float | None = None,
        current_price: float | None = None,
        horizon_ms: int | None = None,
        paper_position_notional: float = 0.0,
        paper_unrealized_pnl: float = 0.0,
        paper_realized_pnl: float = 0.0,
    ) -> ShadowOutcomeCheckpoint | None:
        """Record where the market went and what the paper position was worth.

        ``gross_move_bps`` is arithmetic on two prices the caller supplied, not
        a judgement. **Nothing here says whether the outcome was good** — no
        pass, no fail, no score. That is research, and research needs a
        hypothesis this phase has not got.
        """
        if self.registry.get(decision_id) is None:
            return None
        move: float | None = None
        if reference_price and current_price and reference_price > 0:
            move = (current_price - reference_price) / reference_price * 10_000.0
        return self.registry.add_outcome_checkpoint(
            ShadowOutcomeCheckpoint(
                decision_id=decision_id,
                created_at=now_ms,
                horizon_ms=horizon_ms,
                reference_price=reference_price,
                current_price=current_price,
                gross_move_bps=move,
                paper_position_notional=paper_position_notional,
                paper_unrealized_pnl=paper_unrealized_pnl,
                paper_realized_pnl=paper_realized_pnl,
                market_data=self.registry.market_data,
            )
        )


__all__ = ["ShadowObserver"]
