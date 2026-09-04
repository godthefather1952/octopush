"""The execution interface.

``PaperExecutor`` is the only implementation in this repository and the only
one this build can construct.  There is no live executor, and adding one is
intended to require a deliberate, separate piece of work rather than flipping
a flag.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.common import Millis
from core.models.execution import ExecutionReport, FillEvent, PaperOrder
from core.models.opportunity import ExecutionPlan


class Executor(ABC):
    """Works execution plans and reports what happened.

    EXECUTION TIME IS ALWAYS EXPLICIT (Phase 2 Batch 1.3 -- P2-14)
    ==============================================================
    Every method that makes a time-dependent execution decision takes the
    logical time to make it at, rather than reading a clock itself.
    ``poll(now_ms)`` was always shaped this way; ``submit``, ``cancel`` and
    ``cancel_all`` now are too, because they are just as time-dependent:
    submission fixes an order's ``submitted_at`` and therefore its
    acknowledgement deadline, and a cancel fixes the moment the cancel
    reaches the venue -- both of which decide whether a later poll sees the
    order as acknowledged, cancellable, fillable or expired.

    The caller is the orchestrator, which passes the one canonical logical
    time of the tick the call belongs to (``Orchestrator.tick_time``). That
    is what makes execution replayable: a recorded session preserves one
    timestamp per tick (the ``ORCHESTRATOR_TICK`` marker), so any execution
    decision taken at some *other* instant -- a clock read that happened to
    land later, mid-tick, because a live feed advanced the clock while the
    tick was still running -- is a decision replay can never reconstruct.
    See ``apps/orchestrator/orchestrator.py``'s "TICK TIME" section.
    """

    #: Every implementation must declare whether it can reach a real exchange.
    #: The composition root refuses to construct one that can.
    is_paper: bool = True

    @abstractmethod
    async def submit(self, plan: ExecutionPlan, now_ms: Millis) -> ExecutionReport:
        """Accept a plan and begin working it, as of ``now_ms``."""

    @abstractmethod
    async def cancel(self, client_order_id: str, now_ms: Millis) -> None:
        """Request cancellation as of ``now_ms``.

        The cancel may still lose the race to a fill.
        """

    @abstractmethod
    async def cancel_all(self, now_ms: Millis) -> int:
        """Request cancellation of every live order. Returns the count."""

    @abstractmethod
    async def poll(self, now_ms: Millis) -> list[FillEvent]:
        """Advance simulated execution to ``now_ms`` and return new fills."""

    @abstractmethod
    def open_orders(self) -> list[PaperOrder]:
        ...
