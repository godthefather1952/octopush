"""The exposure targets the platform intends to carry — a mirror, not the source.

``Okapi.desired_delta`` remains the economic authority. This registry only
records target metadata and must not expose resident mutable objects to readers
or historical snapshots.
"""

from __future__ import annotations

from core.models.common import Millis
from core.models.hedging import HedgeTarget, HedgeTargetSource


class HedgeTargetRegistry:
    """Metadata for entries in ``Okapi.desired_delta``."""

    def __init__(self) -> None:
        self._targets: dict[str, HedgeTarget] = {}

    def set_target(
        self,
        symbol: str,
        target_notional: float,
        now_ms: Millis,
        *,
        strategy: str = "",
        source: HedgeTargetSource = HedgeTargetSource.STRATEGY,
        reason_codes: list[str] | None = None,
        active: bool = True,
    ) -> HedgeTarget:
        """Record metadata for one intended exposure target."""
        existing = self._targets.get(symbol)
        if existing is not None:
            existing.target_notional = target_notional
            existing.updated_at = now_ms
            existing.source = source
            existing.active = active
            if strategy:
                existing.strategy = strategy
            if reason_codes is not None:
                existing.reason_codes = list(reason_codes)
            return existing

        target = HedgeTarget(
            created_at=now_ms,
            updated_at=now_ms,
            symbol=symbol,
            strategy=strategy,
            target_notional=target_notional,
            source=source,
            reason_codes=list(reason_codes or []),
            active=active,
        )
        self._targets[symbol] = target
        return target

    def mirror(
        self,
        desired_delta: dict[str, float],
        now_ms: Millis,
        *,
        strategy: str = "",
        source: HedgeTargetSource = HedgeTargetSource.STRATEGY,
    ) -> list[HedgeTarget]:
        """Mirror desired targets and return a detached point-in-time view."""
        for symbol, notional in sorted(desired_delta.items()):
            self.set_target(
                symbol,
                notional,
                now_ms,
                strategy=strategy,
                source=source,
            )
        return self.all_targets()

    def forget(self, symbol: str) -> None:
        self._targets.pop(symbol, None)

    def get_target(self, symbol: str) -> HedgeTarget | None:
        target = self._targets.get(symbol)
        return target.model_copy(deep=True) if target is not None else None

    def all_targets(self) -> list[HedgeTarget]:
        return [target.model_copy(deep=True) for target in self._targets.values()]

    def active_targets(self) -> list[HedgeTarget]:
        return [
            target.model_copy(deep=True)
            for target in self._targets.values()
            if target.active
        ]

    def targets_for_strategy(self, strategy: str) -> list[HedgeTarget]:
        return [
            target.model_copy(deep=True)
            for target in self._targets.values()
            if target.strategy == strategy
        ]

    def symbols(self) -> list[str]:
        return list(self._targets.keys())

    def __len__(self) -> int:
        return len(self._targets)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._targets


__all__ = ["HedgeTargetRegistry"]
