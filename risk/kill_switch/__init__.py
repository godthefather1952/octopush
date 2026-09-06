"""Kill switch.

Four independent actions, because they answer different questions:

* ``HALT_NEW_TRADES`` — stop opening; keep managing what is open.
* ``CANCEL_ALL`` — pull every resting order.
* ``FLATTEN`` — close every position.
* ``DISABLE_EXECUTION`` — refuse new submissions entirely.

Triggers are deterministic predicates evaluated every tick.  Engaging is
automatic; clearing is manual, because a condition that stopped trading should
be understood before trading resumes.

PRE-TRADE PREVENTION IS NOT POST-FILL DETECTION
===============================================
RUNE answers "can this trade be authorised given the state we know now?".
This module answers a different question, every tick: "has the state that
ACTUALLY exists now crossed a hard boundary?".

The two are not interchangeable. A multi-leg trade can transiently create
exposure no pre-trade projection can guarantee away — leg A fills before leg
B, fills land away from expected prices, a fill partials, a cancel loses a
race, a hedge is briefly incomplete. ``RISK_LIMIT_BREACH`` is the layer that
notices, and it needs no confirmation delay: a breached hard limit is true
the first tick it is observed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.models.common import Millis
from core.models.ops import (
    HealthStatus,
    KillAction,
    KillSwitchState,
    Severity,
    SystemEvent,
    SystemHealth,
)
from core.models.portfolio import PortfolioState
from core.models.risk import CommittedExposure

log = logging.getLogger(__name__)

SERVICE = "KILL_SWITCH"

#: What each trigger does when it fires.
#:
#: EVERY ACTION SET CONTAINING ``FLATTEN`` ALSO CONTAINS ``CANCEL_ALL``.
#: Flattening without cancelling first leaves the opening orders that were
#: already resting when the trigger fired; one of them filling afterwards
#: re-opens the exposure the flatten just closed (P5-6). The orchestrator
#: applies CANCEL_ALL before FLATTEN within the tick for the same reason.
#:
#: ``DISABLE_EXECUTION`` is deliberately absent from every action set that
#: flattens: closing a position and hedging a residual are themselves
#: submissions, so a trigger that must reduce exposure cannot also refuse to
#: submit.
TRIGGER_ACTIONS: dict[str, tuple[KillAction, ...]] = {
    "MAX_DRAWDOWN_BREACHED": (
        KillAction.HALT_NEW_TRADES,
        KillAction.CANCEL_ALL,
        KillAction.FLATTEN,
    ),
    "MAX_DAILY_LOSS_BREACHED": (
        KillAction.HALT_NEW_TRADES,
        KillAction.CANCEL_ALL,
        KillAction.FLATTEN,
    ),
    "MARKET_DATA_OUTAGE": (KillAction.HALT_NEW_TRADES, KillAction.CANCEL_ALL),
    "BOOK_CORRUPTION": (KillAction.HALT_NEW_TRADES, KillAction.CANCEL_ALL),
    "RECONCILIATION_MISMATCH": (
        KillAction.HALT_NEW_TRADES,
        KillAction.CANCEL_ALL,
        KillAction.DISABLE_EXECUTION,
    ),
    "UNEXPECTED_POSITION": (KillAction.HALT_NEW_TRADES, KillAction.DISABLE_EXECUTION),
    "SYSTEM_HEALTH_FAILURE": (KillAction.HALT_NEW_TRADES,),
    "EXCESSIVE_LATENCY": (KillAction.HALT_NEW_TRADES,),
    # ``AGENT_FAILURE`` used to sit here with ``(HALT_NEW_TRADES,)`` and no
    # predicate and no caller (P5-17). It was removed rather than given one:
    # its action set was byte-identical to SYSTEM_HEALTH_FAILURE's, whose
    # predicate already covers a required agent going unhealthy, and a
    # required agent that answers with nothing is separately caught by
    # consensus completeness. A dead entry in a safety table invites the
    # reader to assume a response exists that no code path delivers.
    "STORAGE_FAILURE": (KillAction.HALT_NEW_TRADES,),
    "RISK_LIMIT_BREACH": (
        KillAction.HALT_NEW_TRADES,
        KillAction.CANCEL_ALL,
        KillAction.FLATTEN,
    ),
    "MANUAL": (KillAction.HALT_NEW_TRADES, KillAction.CANCEL_ALL, KillAction.FLATTEN),
}


@dataclass
class KillSwitchInputs:
    """Everything the triggers examine.

    Deliberately state, not verdicts: the orchestrator supplies measurements
    and this module decides what they mean. A pre-computed "is breached"
    boolean would move the safety decision out of :data:`TRIGGERS`, and the
    audit could no longer prove from the predicates which limits are actually
    enforced.
    """

    portfolio: PortfolioState
    health: SystemHealth | None
    reconciliation_ok: bool = True
    market_data_ok: bool = True
    book_corruption: bool = False
    max_latency_ms: float = 0.0
    storage_ok: bool = True
    unexpected_position: bool = False
    #: Components whose failure suspends trading.
    required_components: list[str] = field(default_factory=list)
    #: Exposure from entry orders that are working and have not filled, derived
    #: by ``Orchestrator._current_committed_exposure``. Current risk just as
    #: much as a filled position is, so the exposure predicates below add it to
    #: the portfolio rather than waiting for a fill to make it visible.
    committed_exposure: CommittedExposure = field(default_factory=CommittedExposure)
    #: Canonical full-lifecycle gross strategy exposure, from
    #: ``Orchestrator._current_strategy_exposure``.
    strategy_exposure: float = 0.0
    #: OKAPI's measurement of unhedged residual delta in the FILLED book.
    unhedged_notional: float = 0.0


Trigger = Callable[[KillSwitchInputs, Settings], bool]


def _drawdown(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return inputs.portfolio.drawdown >= settings.risk.max_drawdown


def _daily_loss(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return -inputs.portfolio.day_realized_pnl >= settings.risk.max_daily_loss


def _market_data(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return not inputs.market_data_ok


def _book_corruption(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return inputs.book_corruption


def _reconciliation(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return not inputs.reconciliation_ok


def _unexpected_position(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return inputs.unexpected_position


def _health(inputs: KillSwitchInputs, settings: Settings) -> bool:
    if inputs.health is None:
        return True
    ok, _bad = inputs.health.required_ok(inputs.required_components)
    return not ok


def _latency(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return inputs.max_latency_ms > settings.risk.max_data_age_ms * 5


def _storage(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return not inputs.storage_ok


def live_risk_breaches(inputs: KillSwitchInputs, settings: Settings) -> list[str]:
    """Every hard risk limit the state that exists RIGHT NOW exceeds.

    Pure and side-effect free: the same portfolio, committed exposure,
    strategy exposure, unhedged measurement and settings always produce the
    same answer. It reads no wall time, draws no sample, and reaches no
    network or model — a replayed run reaches the same verdict as the live
    one that produced it.

    FILLED PLUS COMMITTED
    =====================
    The exposure dimensions use the portfolio PLUS
    ``inputs.committed_exposure`` — the same :class:`CommittedExposure`
    derivation RUNE's pre-trade gates use since P5-1 — because an entry order
    that is working and unfilled is risk the platform has already accepted.
    There is deliberately no second calculation model.

    BOUNDARY SEMANTICS
    ==================
    These are maximum limits, matching RUNE's gate convention: ``value <=
    limit`` is safe and only ``value > limit`` is a breach. Sitting exactly on
    a limit is permitted, so the emergency layer never fires on a state the
    pre-trade layer would have authorised.

    The returned names are diagnostic — for logs, tests and operator
    observability. Nothing branches on their order; the caller only asks
    whether the list is empty.
    """
    limits = settings.risk
    portfolio = inputs.portfolio
    committed = inputs.committed_exposure
    breached: list[str] = []

    gross_current = portfolio.gross_exposure + committed.gross_exposure
    if gross_current > limits.max_gross_exposure:
        breached.append("MAX_GROSS_EXPOSURE")

    net_current = portfolio.net_exposure + committed.net_exposure
    if abs(net_current) > limits.max_net_exposure:
        breached.append("MAX_NET_EXPOSURE")

    # Non-positive equity carrying exposure is unbounded leverage, reported as
    # a breach rather than divided by. Flat and broke is left alone: there is
    # no leverage to speak of, and insolvency is a different condition from an
    # over-levered book.
    equity = portfolio.equity
    if gross_current > 0.0 and (
        equity <= 0.0 or gross_current / equity > limits.max_leverage
    ):
        breached.append("MAX_LEVERAGE")

    by_venue = portfolio.exposure_by_venue()
    for venue, amount in committed.venue_exposure.items():
        by_venue[venue] = by_venue.get(venue, 0.0) + amount
    if any(amount > limits.max_venue_exposure for amount in by_venue.values()):
        breached.append("MAX_VENUE_EXPOSURE")

    # Conservative in the same way RUNE is: opposing legs on one venue:symbol
    # are added, never netted.
    by_position = {key: held.notional for key, held in portfolio.positions.items()}
    for key, amount in committed.position_exposure.items():
        by_position[key] = by_position.get(key, 0.0) + amount
    if any(amount > limits.max_position_notional for amount in by_position.values()):
        breached.append("MAX_POSITION_NOTIONAL")

    # Committed exposure is deliberately NOT added: ``strategy_exposure`` is
    # reserved at authorisation and held for the whole lifecycle, so it already
    # covers working trades. Adding it again would double-count them (P5-2).
    if inputs.strategy_exposure > limits.max_strategy_exposure:
        breached.append("MAX_STRATEGY_EXPOSURE")

    # Watched at its OWN limit, not at the 3x catastrophic level
    # ``UNEXPECTED_POSITION`` uses. Between the two there was a band in which
    # the configured hard gate said "unsafe" and the emergency layer said
    # nothing at all — which is exactly where the production probe's
    # 25,103 against a 10,000 limit sat.
    #
    # Committed net exposure is deliberately NOT added here. An unfilled
    # second leg is a commitment, but it has not neutralised the one-sided
    # position that exists now, and this measures the residual that exists now.
    if abs(inputs.unhedged_notional) > limits.max_unhedged_notional:
        breached.append("MAX_UNHEDGED_EXPOSURE")

    return breached


def _risk_limit_breach(inputs: KillSwitchInputs, settings: Settings) -> bool:
    return bool(live_risk_breaches(inputs, settings))


TRIGGERS: dict[str, Trigger] = {
    "MAX_DRAWDOWN_BREACHED": _drawdown,
    "MAX_DAILY_LOSS_BREACHED": _daily_loss,
    "RISK_LIMIT_BREACH": _risk_limit_breach,
    "MARKET_DATA_OUTAGE": _market_data,
    "BOOK_CORRUPTION": _book_corruption,
    "RECONCILIATION_MISMATCH": _reconciliation,
    "UNEXPECTED_POSITION": _unexpected_position,
    "SYSTEM_HEALTH_FAILURE": _health,
    "EXCESSIVE_LATENCY": _latency,
    "STORAGE_FAILURE": _storage,
}

#: Consecutive evaluations a trigger must fire on before it engages.
#:
#: A breached limit or a corrupted book is true the moment it is observed, so
#: those engage immediately — ``RISK_LIMIT_BREACH`` included, and deliberately
#: absent from this map. A *measurement* — component health, observed
#: latency — can blip for one tick without anything being wrong, and since
#: clearing the switch is manual, letting a single sample halt the platform
#: permanently would make it useless. Sustained failure still engages.
CONFIRMATIONS: dict[str, int] = {
    "SYSTEM_HEALTH_FAILURE": 3,
    "EXCESSIVE_LATENCY": 3,
    "MARKET_DATA_OUTAGE": 2,
}


class KillSwitch:
    def __init__(self, bus: EventBus, clock: Clock, settings: Settings) -> None:
        self.bus = bus
        self.clock = clock
        self.settings = settings
        self.state = KillSwitchState()
        self.history: list[tuple[int, str]] = []
        #: Consecutive evaluations each trigger has fired on.
        self.streaks: dict[str, int] = {}

    # -- engagement --------------------------------------------------------

    def _apply(self, actions: tuple[KillAction, ...]) -> None:
        for action in actions:
            if action is KillAction.HALT_NEW_TRADES:
                self.state.halt_new_trades = True
            elif action is KillAction.CANCEL_ALL:
                self.state.cancel_all_requested = True
            elif action is KillAction.FLATTEN:
                self.state.flatten_requested = True
            elif action is KillAction.DISABLE_EXECUTION:
                self.state.execution_disabled = True

    async def engage(
        self, trigger: str, detail: str = "", now_ms: Millis | None = None
    ) -> KillSwitchState:
        """Fire a trigger by name. Idempotent for an already-fired trigger.

        ``now_ms`` is the caller's canonical logical time. An AUTOMATIC
        engagement happens because of state the orchestrator observed during
        one tick, so it must be stamped at that tick's instant rather than at
        whatever the live clock reads by the time the event is published
        (P5-16): the economic action is deterministic either way, but the
        recorded causal ordering of a safety event would otherwise differ
        between a run and its replay. The clock remains the fallback for a
        genuinely manual operator action, which happens outside any tick.
        """
        if trigger in self.state.triggered_by:
            return self.state
        now: Millis = self.clock.now_ms() if now_ms is None else now_ms
        self.state.triggered_by = [*self.state.triggered_by, trigger]
        self.state.triggered_at = self.state.triggered_at or now
        self._apply(TRIGGER_ACTIONS.get(trigger, (KillAction.HALT_NEW_TRADES,)))
        self.history.append((now, trigger))
        log.error(
            "kill switch engaged", extra={"trigger": trigger, "detail": detail}
        )
        await self.bus.publish(
            Event(
                type=EventType.KILL_SWITCH_TRIGGERED,
                ts_ms=now,
                source=SERVICE,
                schema_name="SystemEvent",
                payload=SystemEvent(
                    created_at=now,
                    kind=trigger,
                    severity=Severity.CRITICAL,
                    component=SERVICE,
                    message=detail or f"kill switch trigger {trigger}",
                    detail={
                        "halt_new_trades": self.state.halt_new_trades,
                        "cancel_all": self.state.cancel_all_requested,
                        "flatten": self.state.flatten_requested,
                        "execution_disabled": self.state.execution_disabled,
                    },
                ).to_json_dict(),
            )
        )
        return self.state

    async def evaluate(
        self, inputs: KillSwitchInputs, now_ms: Millis | None = None
    ) -> list[str]:
        """Run every trigger; engage those that fire. Returns the new ones.

        ONE INSTANT PER EVALUATION
        ==========================
        Every engagement this call produces is stamped at ``now_ms`` — the
        orchestrator's tick time — so a safety event lands in the same logical
        instant as the market snapshot, portfolio and risk decisions that
        justified it (P5-16).

        A PREDICATE THAT RAISES IS A SAFETY FAILURE
        ===========================================
        It used to be logged and skipped, which meant a crashing predicate
        silently stopped protecting and the platform carried on trading with
        one fewer safety condition than it believed it had — fail-open on the
        emergency boundary (P5-12).

        Now it engages SYSTEM_HEALTH_FAILURE. That is the honest
        classification: a mandatory check that cannot be evaluated has not
        been satisfied, which is the same fail-closed rule RUNE applies to an
        UNKNOWN gate. No new trigger is invented, and the failing predicate is
        never called again — ``engage`` applies state and actions directly, so
        this stays safe even when the predicate that raised was
        SYSTEM_HEALTH_FAILURE itself.
        """
        now: Millis = self.clock.now_ms() if now_ms is None else now_ms
        fired: list[str] = []
        for name, predicate in TRIGGERS.items():
            if name in self.state.triggered_by:
                continue
            try:
                condition = bool(predicate(inputs, self.settings))
            except Exception:
                log.exception("kill switch trigger %s failed", name)
                already = "SYSTEM_HEALTH_FAILURE" in self.state.triggered_by
                await self.engage(
                    "SYSTEM_HEALTH_FAILURE",
                    f"kill-switch predicate {name} raised; treating an "
                    "unevaluable safety condition as unsatisfied",
                    now_ms=now,
                )
                if not already:
                    fired.append("SYSTEM_HEALTH_FAILURE")
                continue

            if not condition:
                # The condition cleared before it was confirmed.
                self.streaks.pop(name, None)
                continue

            streak = self.streaks.get(name, 0) + 1
            self.streaks[name] = streak
            required = CONFIRMATIONS.get(name, 1)
            if streak < required:
                log.warning(
                    "kill switch condition observed",
                    extra={"trigger": name, "streak": streak, "required": required},
                )
                continue
            await self.engage(
                name,
                f"confirmed on {streak} consecutive evaluations",
                now_ms=now,
            )
            fired.append(name)
        return fired

    async def clear(
        self, reason: str = "manual reset", now_ms: Millis | None = None
    ) -> KillSwitchState:
        """Manual reset. Never automatic.

        Resets THIS object's state only. Restoring the application-side
        latches a trigger set — ``PaperExecutor.execution_disabled`` above all
        — belongs to the orchestrator, which owns those effects;
        ``Orchestrator.clear_kill_switch`` is the one coordinated recovery
        path (P5-5). Giving the kill switch a reference to the executor would
        invert that boundary.
        """
        now: Millis = self.clock.now_ms() if now_ms is None else now_ms
        self.state = KillSwitchState()
        self.streaks.clear()
        await self.bus.publish(
            Event(
                type=EventType.KILL_SWITCH_CLEARED,
                ts_ms=now,
                source=SERVICE,
                schema_name="SystemEvent",
                payload=SystemEvent(
                    created_at=now,
                    kind="KILL_SWITCH_CLEARED",
                    severity=Severity.WARNING,
                    component=SERVICE,
                    message=reason,
                ).to_json_dict(),
            )
        )
        return self.state

    def acknowledge_cancel_all(self) -> None:
        self.state.cancel_all_requested = False

    def acknowledge_flatten(self) -> None:
        self.state.flatten_requested = False


__all__ = [
    "CONFIRMATIONS",
    "SERVICE",
    "TRIGGERS",
    "TRIGGER_ACTIONS",
    "CommittedExposure",
    "HealthStatus",
    "KillAction",
    "KillSwitch",
    "KillSwitchInputs",
    "KillSwitchState",
    "live_risk_breaches",
]
