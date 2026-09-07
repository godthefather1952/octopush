"""The exposure targets the platform intends to carry — a mirror, not the source.

WHAT THIS IS
============
``Okapi.desired_delta`` is a plain ``dict[str, float]``: symbol to signed quote
notional. It is set once at wiring (zero for every symbol, because cross-venue
relative value intends to carry no directional exposure) and read on every
delta measurement. It is small, correct, and tested.

What it cannot answer is anything *about* a target: when it was set, who set
it, why, or whether it is still maintained. :class:`HedgeTargetRegistry` holds
that metadata beside it.

WHAT IT IS NOT
==============
**Not a replacement.** ``Okapi.target(symbol)`` still reads the dict, and
``delta_reports`` still measures against what the dict says. Nothing in the
hedging path consults this registry. If the registry and the dict ever
disagree, the dict is right and the mirror is stale — never the other way
around.

The registry does not default a missing symbol to zero the way ``target()``
does, and that difference is deliberate: ``target()`` answers "what exposure do
we intend?", where zero is the honest answer for a symbol nobody has spoken
about. The registry answers "what has been recorded?", where the honest answer
is ``None``.
"""

from __future__ import annotations

from core.models.common import Millis
from core.models.hedging import HedgeTarget, HedgeTargetSource


class HedgeTargetRegistry:
    """Metadata for the entries in ``Okapi.desired_delta``.

    Registration order is preserved. Re-setting a symbol updates the existing
    record in place — keeping its ``target_id`` and ``created_at`` — rather
    than appending a second target for the same symbol, because a symbol has
    one intended exposure and a directory that grew a second would be
    reporting a fiction.
    """

    def __init__(self) -> None:
        self._targets: dict[str, HedgeTarget] = {}

    # -- writes ------------------------------------------------------------

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
        """Record a symbol's intended exposure.

        ``now_ms`` is supplied by the caller, never read from a clock, so a
        replayed session records the instants the original recorded.

        This writes to the registry only. It does **not** call
        ``Okapi.set_desired_delta``: a mirror that wrote through would be able
        to change what the platform hedges against, which is exactly what it
        must not be able to do.
        """
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
        """Copy the whole of ``Okapi.desired_delta`` in at one instant.

        The bulk form, for a caller that already has a logical time in hand —
        a snapshot capture, or wiring. Symbols the dict no longer carries are
        left alone rather than deactivated: ``desired_delta`` has no concept of
        removing a target, and inventing one here would report a retirement
        that never happened.
        """
        return [
            self.set_target(
                symbol,
                notional,
                now_ms,
                strategy=strategy,
                source=source,
            )
            for symbol, notional in sorted(desired_delta.items())
        ]

    def forget(self, symbol: str) -> None:
        """Drop one record. Nothing about hedging changes."""
        self._targets.pop(symbol, None)

    # -- reads -------------------------------------------------------------

    def get_target(self, symbol: str) -> HedgeTarget | None:
        """The recorded target for one symbol, or ``None`` if never recorded.

        ``None`` means "not described here". It does not mean the intended
        exposure is zero — ``Okapi.target(symbol)`` answers that, and still
        does.
        """
        return self._targets.get(symbol)

    def all_targets(self) -> list[HedgeTarget]:
        return list(self._targets.values())

    def active_targets(self) -> list[HedgeTarget]:
        return [target for target in self._targets.values() if target.active]

    def targets_for_strategy(self, strategy: str) -> list[HedgeTarget]:
        return [
            target for target in self._targets.values() if target.strategy == strategy
        ]

    def symbols(self) -> list[str]:
        return list(self._targets.keys())

    def __len__(self) -> int:
        return len(self._targets)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._targets


__all__ = ["HedgeTargetRegistry"]
