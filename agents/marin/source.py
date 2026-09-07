"""Reconciliation sources — where each account of the truth comes from.

A SOURCE SUPPLIES TRUTH. IT NEVER JUDGES ANOTHER SOURCE.
=======================================================
That separation is the whole design. A source that could decide the ledger was
wrong would be doing reconciliation inside a component nobody can test in
isolation, and the two halves — capture and comparison — would be impossible to
attack independently. Every source here answers exactly one question: *what do
you believe, as of this instant?*

Capture is synchronous. Every source this build has reads local state, and an
``async`` interface would have made every call site await something that never
yields. The venue seam below is the one place a future implementation genuinely
needs I/O, and it is documented as the point where that decision has to be
revisited — deliberately, rather than by discovering it mid-implementation.

Every source reports its own health. A source that could not answer says so
through :class:`SourceHealth`; it does not return an empty snapshot and let the
caller mistake "nothing to report" for "nothing happened".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.models.common import Millis
from core.models.execution import ExecutionSnapshot
from core.models.reconciliation import (
    AccountSnapshot,
    RecordedTruthSnapshot,
    ReconciliationSourceKind,
    SourceAuthority,
    SourceHealth,
    VenueTruthSnapshot,
)


@dataclass(frozen=True)
class SourceCapture:
    """One source's answer, plus whether it was able to give one.

    The pairing is the point. A snapshot on its own cannot express "the venue
    was unreachable", and a reconciler handed ``None`` cannot tell that from
    "the venue holds nothing" — which are opposite conclusions.
    """

    health: SourceHealth
    snapshot: object | None = None

    @property
    def ok(self) -> bool:
        return self.health.usable and self.snapshot is not None


class ReconciliationSource(ABC):
    """One account of what happened, capturable at a logical instant."""

    @property
    @abstractmethod
    def kind(self) -> ReconciliationSourceKind:
        """Which account of the truth this source represents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, unique among the configured sources."""

    @property
    def authority(self) -> SourceAuthority:
        """How much structural weight this source's account carries.

        Metadata, not comparison logic. Nothing in this build treats
        AUTHORITATIVE as "always right"; see
        :class:`~core.models.reconciliation.SourceAuthority`.
        """
        return SourceAuthority.INTERNAL

    @property
    def is_authoritative(self) -> bool:
        return self.authority is SourceAuthority.AUTHORITATIVE

    @abstractmethod
    def capture(self, now_ms: Millis) -> SourceCapture:
        """This source's belief at ``now_ms``.

        The instant is supplied, never read, so a capture made during replay
        carries the instant the original run recorded.

        Must not raise for an ordinary unavailability: return a
        :class:`SourceCapture` whose health says what went wrong. A source that
        throws takes the whole reconciliation down with it, which is a worse
        outcome than a run that records one missing account.
        """

    def _health(
        self,
        now_ms: Millis,
        *,
        available: bool = True,
        complete: bool = True,
        detail: str = "",
    ) -> SourceHealth:
        return SourceHealth(
            kind=self.kind,
            name=self.name,
            authority=self.authority,
            available=available,
            complete=complete,
            captured_at=now_ms,
            detail=detail,
        )


# ======================================================================
# local sources — the two this build actually has
# ======================================================================


class ExecutionReconciliationSource(ReconciliationSource):
    """What VESKA and the executor believe about orders and fills.

    A thin wrapper over the Phase 6 query seam, and deliberately nothing more:
    it holds no comparison logic and keeps no state of its own. It calls
    ``execution_snapshot(now_ms)`` rather than reaching into
    ``OrderManager.orders`` or ``PaperExecutor._pending``, which is the point of
    Phase 6 having built that seam.

    ``provider`` is anything exposing ``execution_snapshot(now_ms)`` — VESKA
    (which adds the plan-level view) or an executor directly. VESKA is
    preferred, because plan state is part of what execution believes.
    """

    def __init__(self, provider, *, name: str = "execution") -> None:
        self._provider = provider
        self._name = name

    @property
    def kind(self) -> ReconciliationSourceKind:
        return ReconciliationSourceKind.EXECUTION

    @property
    def name(self) -> str:
        return self._name

    @property
    def authority(self) -> SourceAuthority:
        # Internal: this is what the platform decided to do, not an external
        # confirmation that it happened.
        return SourceAuthority.INTERNAL

    def capture(self, now_ms: Millis) -> SourceCapture:
        snapshot: ExecutionSnapshot = self._provider.execution_snapshot(now_ms)
        return SourceCapture(health=self._health(now_ms), snapshot=snapshot)


class AccountReconciliationSource(ReconciliationSource):
    """What the internal ledger believes resulted from those fills.

    Wraps ``PaperAccount.reconciliation_snapshot(now_ms)``. No comparison
    logic; no state of its own.
    """

    def __init__(self, account, *, name: str = "paper-account") -> None:
        self._account = account
        self._name = name

    @property
    def kind(self) -> ReconciliationSourceKind:
        return ReconciliationSourceKind.ACCOUNT

    @property
    def name(self) -> str:
        return self._name

    @property
    def authority(self) -> SourceAuthority:
        return SourceAuthority.INTERNAL

    def capture(self, now_ms: Millis) -> SourceCapture:
        snapshot: AccountSnapshot = self._account.reconciliation_snapshot(now_ms)
        return SourceCapture(health=self._health(now_ms), snapshot=snapshot)


# ======================================================================
# future seams — interfaces, with nothing behind them
# ======================================================================


class VenueReconciliationSource(ReconciliationSource):
    """What an external venue authoritatively reports.

    **NO IMPLEMENTATION EXISTS.** This build has no authenticated venue
    connection, constructs no such source, and Phase 7 adds none. There is no
    credential handling, no HTTP, no WebSocket, no signing and no exchange SDK
    anywhere beneath this class — not stubbed, not commented out.

    A future implementation would consume an
    :class:`~execution.gateway.ExecutionVenueGateway` and normalise its replies
    into a :class:`~core.models.reconciliation.VenueTruthSnapshot`.

    TWO THINGS A FUTURE IMPLEMENTER MUST FACE
    =========================================
    **This source needs I/O, and the interface is synchronous.** Every source
    the platform has today reads local state, so ``capture`` is a plain call.
    A venue capture is a network round trip, and making it synchronous would
    block a tick. Whoever implements this has to either widen the interface to
    async or capture out of band and serve the most recent reply — a real
    decision, recorded here so it is made deliberately.

    **Partial answers are the normal case.** A paged order query that stopped,
    a rate limit hit halfway, a positions call that succeeded while a balances
    call did not — all of these must set ``complete=False`` rather than
    returning what arrived as though it were everything. A reconciler comparing
    against a silently truncated venue view will report fills as missing that
    were simply never fetched.
    """

    @property
    def kind(self) -> ReconciliationSourceKind:
        return ReconciliationSourceKind.VENUE

    @property
    def authority(self) -> SourceAuthority:
        # External and definitive about order existence, venue fills, balances
        # and positions. That does not make it right about everything: it knows
        # nothing about what the platform intended.
        return SourceAuthority.AUTHORITATIVE

    @property
    @abstractmethod
    def venue(self) -> str:
        """The venue this source speaks for."""

    @abstractmethod
    def capture(self, now_ms: Millis) -> SourceCapture:
        """A :class:`VenueTruthSnapshot` for this venue, or a health record
        saying why there is not one."""


class RecordedReconciliationSource(ReconciliationSource):
    """What durable event history reconstructs.

    **NO IMPLEMENTATION EXISTS.** Phase 2's replay engine is untouched and no
    reconstruction is performed here.

    The seam exists because this source catches a class of bug the others
    cannot: one where execution and the account agree with each other and both
    disagree with what was actually published. Two internal views that were
    updated by the same code path can be wrong together; the event log is the
    only account written for a different purpose.

    A future implementation would fold recorded events into a
    :class:`~core.models.reconciliation.RecordedTruthSnapshot`, and must set
    ``through_sequence`` honestly — a reconstruction is only an account of the
    events it actually read.
    """

    @property
    def kind(self) -> ReconciliationSourceKind:
        return ReconciliationSourceKind.RECORDED

    @property
    def authority(self) -> SourceAuthority:
        return SourceAuthority.DERIVED

    @abstractmethod
    def capture(self, now_ms: Millis) -> SourceCapture:
        """A :class:`RecordedTruthSnapshot`, or a health record saying why not."""


__all__ = [
    "AccountReconciliationSource",
    "ExecutionReconciliationSource",
    "ReconciliationSource",
    "RecordedReconciliationSource",
    "RecordedTruthSnapshot",
    "SourceCapture",
    "VenueReconciliationSource",
    "VenueTruthSnapshot",
]
