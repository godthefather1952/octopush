"""Component health.

Every service heartbeats.  A component that stops heartbeating goes DEGRADED
and then OFFLINE — it never silently disappears, and an OFFLINE agent is never
interpreted as a neutral opinion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import Millis
from core.models.ops import HealthState, HealthStatus, SystemHealth


@dataclass
class HealthPolicy:
    """Thresholds converting heartbeat age into a status."""

    degraded_after_ms: int = 5_000
    offline_after_ms: int = 15_000
    #: Errors within the window above which a component is DEGRADED.
    error_budget: int = 10


@dataclass
class HealthRegistry:
    """Central registry of component heartbeats."""

    clock: Clock
    policy: HealthPolicy = field(default_factory=HealthPolicy)
    _states: dict[str, HealthState] = field(default_factory=dict)

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
        state = HealthState(
            service=service,
            status=status,
            last_event_age_ms=last_event_age_ms,
            last_heartbeat_ms=self.clock.now_ms(),
            queue_depth=queue_depth,
            error_count=self._states[service].error_count if service in self._states else 0,
            version=version,
            detail=detail,
        )
        self._states[service] = state
        return state

    def record_error(self, service: str, detail: str = "") -> None:
        state = self._states.get(service) or HealthState(service=service)
        state.error_count += 1
        if state.error_count >= self.policy.error_budget and state.status is HealthStatus.HEALTHY:
            state.status = HealthStatus.DEGRADED
            state.detail = detail or "error budget exceeded"
        self._states[service] = state

    def clear_errors(self, service: str) -> None:
        if service in self._states:
            self._states[service].error_count = 0

    def _aged(self, state: HealthState, now: Millis) -> HealthState:
        """Apply heartbeat-age decay without mutating the stored record."""
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

    def snapshot(self) -> SystemHealth:
        now = self.clock.now_ms()
        return SystemHealth(
            created_at=now,
            components={name: self._aged(state, now) for name, state in self._states.items()},
        )

    def status_of(self, service: str) -> HealthStatus:
        state = self._states.get(service)
        if state is None:
            return HealthStatus.OFFLINE
        return self._aged(state, self.clock.now_ms()).status

    def all_healthy(self, required: list[str]) -> tuple[bool, list[str]]:
        return self.snapshot().required_ok(required)


__all__ = ["HealthPolicy", "HealthRegistry", "HealthState", "HealthStatus", "SystemHealth"]
