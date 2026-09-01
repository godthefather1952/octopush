"""Kill switch.

Four independent actions, because they answer different questions:

* ``HALT_NEW_TRADES`` — stop opening; keep managing what is open.
* ``CANCEL_ALL`` — pull every resting order.
* ``FLATTEN`` — close every position.
* ``DISABLE_EXECUTION`` — refuse new submissions entirely.

Triggers are deterministic predicates evaluated every tick.  Engaging is
automatic; clearing is manual, because a condition that stopped trading should
be understood before trading resumes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.config import Settings
from core.events import Event, EventType
from core.models.ops import (
    HealthStatus,
    KillAction,
    KillSwitchState,
    Severity,
    SystemEvent,
    SystemHealth,
)
from core.models.portfolio import PortfolioState

log = logging.getLogger(__name__)

SERVICE = "KILL_SWITCH"

#: What each trigger does when it fires.
TRIGGER_ACTIONS: dict[str, tuple[KillAction, ...]] = {
    "MAX_DRAWDOWN_BREACHED": (KillAction.HALT_NEW_TRADES, KillAction.FLATTEN),
    "MAX_DAILY_LOSS_BREACHED": (KillAction.HALT_NEW_TRADES, KillAction.FLATTEN),
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
    "AGENT_FAILURE": (KillAction.HALT_NEW_TRADES,),
    "STORAGE_FAILURE": (KillAction.HALT_NEW_TRADES,),
    "RISK_LIMIT_BREACH": (KillAction.HALT_NEW_TRADES, KillAction.CANCEL_ALL),
    "MANUAL": (KillAction.HALT_NEW_TRADES, KillAction.CANCEL_ALL, KillAction.FLATTEN),
}


@dataclass
class KillSwitchInputs:
    """Everything the triggers examine."""

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


TRIGGERS: dict[str, Trigger] = {
    "MAX_DRAWDOWN_BREACHED": _drawdown,
    "MAX_DAILY_LOSS_BREACHED": _daily_loss,
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
#: those engage immediately. A *measurement* — component health, observed
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

    async def engage(self, trigger: str, detail: str = "") -> KillSwitchState:
        """Fire a trigger by name. Idempotent for an already-fired trigger."""
        if trigger in self.state.triggered_by:
            return self.state
        now = self.clock.now_ms()
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

    async def evaluate(self, inputs: KillSwitchInputs) -> list[str]:
        """Run every trigger; engage those that fire. Returns the new ones."""
        fired: list[str] = []
        for name, predicate in TRIGGERS.items():
            if name in self.state.triggered_by:
                continue
            try:
                condition = bool(predicate(inputs, self.settings))
            except Exception:
                log.exception("kill switch trigger %s failed", name)
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
            await self.engage(name, f"confirmed on {streak} consecutive evaluations")
            fired.append(name)
        return fired

    async def clear(self, reason: str = "manual reset") -> KillSwitchState:
        """Manual reset. Never automatic."""
        now = self.clock.now_ms()
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
    "HealthStatus",
    "KillAction",
    "KillSwitch",
    "KillSwitchInputs",
    "KillSwitchState",
]
