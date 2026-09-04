"""Component health.

Every service heartbeats.  A component that stops heartbeating goes DEGRADED
and then OFFLINE — it never silently disappears, and an OFFLINE agent is never
interpreted as a neutral opinion.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import Millis
from core.models.ops import HealthState, HealthStatus, SystemHealth


@dataclass
class HealthPolicy:
    """Thresholds converting heartbeat age into a status."""

    degraded_after_ms: int = 5_000
    offline_after_ms: int = 15_000
    #: Errors inside ``error_window_ms`` at or above which a component is
    #: DEGRADED; twice that many makes it OFFLINE.
    error_budget: int = 10
    #: Rolling window for the error budget. Bounded so an old burst does not
    #: poison a component permanently.
    error_window_ms: int = 60_000


@dataclass
class HealthRegistry:
    """Central registry of component heartbeats."""

    clock: Clock
    policy: HealthPolicy = field(default_factory=HealthPolicy)
    _states: dict[str, HealthState] = field(default_factory=dict)
    #: Error timestamps per service, trimmed to the rolling window.
    _errors: dict[str, deque] = field(default_factory=dict)

    def register(self, service: str, version: str = "0.1.0") -> None:
        self._states.setdefault(
            service, HealthState(service=service, status=HealthStatus.OFFLINE, version=version)
        )

    def heartbeat(
        self,
        service: str,
        *,
        status: HealthStatus = HealthStatus.HEALTHY,
        last_event_age_ms: int | None = None,
        queue_depth: int = 0,
        version: str = "0.1.0",
        detail: str = "",
    ) -> HealthState:
        """Report liveness and the component's own view of its status.

        Liveness and error history are separate signals. A component can be
        alive and still unhealthy, so the reported status is combined with the
        error-budget status rather than replacing it — an earlier version let
        any heartbeat clear a budget breach, which made the budget inert for
        every component that heartbeats each tick (i.e. all of them).
        """
        now = self.clock.now_ms()
        errors = self._errors.setdefault(service, deque())
        self._evict_errors(errors, now)
        reported = status
        budget_status = self._budget_status(errors)
        effective = max((reported, budget_status), key=lambda s: s.rank)
        if effective is not reported and not detail:
            detail = (
                f"{len(errors)} errors in the last "
                f"{self.policy.error_window_ms}ms exceeds a budget of "
                f"{self.policy.error_budget}"
            )
        state = HealthState(
            service=service,
            status=effective,
            last_event_age_ms=last_event_age_ms,
            last_heartbeat_ms=now,
            queue_depth=queue_depth,
            error_count=len(errors),
            version=version,
            detail=detail,
        )
        self._states[service] = state
        return state

    def _evict_errors(self, errors: deque, now: Millis) -> None:
        """Drop errors outside the rolling window.

        Bounded history matters in both directions: a component must not be
        poisoned for ever by an old burst, and must not look healthy while
        failing continuously.
        """
        cutoff = now - self.policy.error_window_ms
        while errors and errors[0] < cutoff:
            errors.popleft()

    def _budget_status(self, errors: deque) -> HealthStatus:
        if len(errors) >= self.policy.error_budget * 2:
            return HealthStatus.OFFLINE
        if len(errors) >= self.policy.error_budget:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def record_error(self, service: str, detail: str = "") -> None:
        now = self.clock.now_ms()
        errors = self._errors.setdefault(service, deque())
        errors.append(now)
        self._evict_errors(errors, now)
        state = self._states.get(service) or HealthState(service=service)
        state.error_count = len(errors)
        budget_status = self._budget_status(errors)
        if budget_status.rank > state.status.rank:
            state.status = budget_status
            state.detail = detail or "error budget exceeded"
        self._states[service] = state

    def error_rate(self, service: str, now_ms: Millis | None = None) -> int:
        """Errors inside the rolling window ending at ``now_ms``."""
        errors = self._errors.get(service)
        if not errors:
            return 0
        self._evict_errors(errors, self.clock.now_ms() if now_ms is None else now_ms)
        return len(errors)

    def clear_errors(self, service: str) -> None:
        self._errors.pop(service, None)
        if service in self._states:
            self._states[service].error_count = 0

    def _aged(self, state: HealthState, now: Millis) -> HealthState:
        """Apply heartbeat-age decay without mutating the stored record.

        A heartbeat stamped LATER than ``now`` is possible and is not an
        error (Phase 2 Batch 1.4): components heartbeat from the live clock,
        which a concurrent feed can advance past the canonical time of the
        tick currently asking. The resulting age is negative, and the rule
        here is deliberate rather than an accident of the comparisons below:
        a component that reported liveness at or after the instant being
        asked about cannot be stale at that instant, so it decays to
        neither DEGRADED nor OFFLINE. Nothing is clamped silently -- the age
        is only ever used for those two thresholds, and the recorded
        ``last_heartbeat_ms`` keeps its true value.

        This is safe for replay because the decayed STATUS is what feeds
        economic decisions (RUNE's health gate, the kill switch, warm-up),
        and it is identical whether the heartbeat landed slightly before or
        slightly after the evaluation instant. See
        ``tests/unit/test_logical_time_boundaries.py``.
        """
        if state.last_heartbeat_ms is None:
            return state.model_copy(update={"status": HealthStatus.OFFLINE})
        age = now - state.last_heartbeat_ms
        if age >= self.policy.offline_after_ms:
            return state.model_copy(
                update={"status": HealthStatus.OFFLINE, "detail": f"no heartbeat for {age}ms"}
            )
        if age >= self.policy.degraded_after_ms and state.status is HealthStatus.HEALTHY:
            return state.model_copy(
                update={"status": HealthStatus.DEGRADED, "detail": f"stale heartbeat {age}ms"}
            )
        return state

    def snapshot(self, now_ms: Millis | None = None) -> SystemHealth:
        """Health as judged at ``now_ms`` (default: the clock).

        Heartbeats keep the true instant they arrived; this is the separate
        question of how old they are *when a decision asks*. A tick supplies
        its canonical time, because health feeds RUNE's hard gates, the kill
        switch and warm-up -- so a clock advancing mid-tick could otherwise
        age a component past DEGRADED or OFFLINE between two reads inside
        one logical tick, and replay would never reproduce it (Phase 2 Batch
        1.4).
        """
        now = self.clock.now_ms() if now_ms is None else now_ms
        return SystemHealth(
            created_at=now,
            components={name: self._aged(state, now) for name, state in self._states.items()},
        )

    def status_of(self, service: str, now_ms: Millis | None = None) -> HealthStatus:
        state = self._states.get(service)
        if state is None:
            return HealthStatus.OFFLINE
        return self._aged(state, self.clock.now_ms() if now_ms is None else now_ms).status

    def all_healthy(
        self, required: list[str], now_ms: Millis | None = None
    ) -> tuple[bool, list[str]]:
        return self.snapshot(now_ms).required_ok(required)


__all__ = ["HealthPolicy", "HealthRegistry", "HealthState", "HealthStatus", "SystemHealth"]
