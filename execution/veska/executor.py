"""The execution interface.

``PaperExecutor`` is the only implementation in this repository and the only
one this build can construct.  There is no live executor, and adding one is
intended to require a deliberate, separate piece of work rather than flipping
a flag.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.common import Millis
from core.models.execution import (
    ExecutionCommandResult,
    ExecutionReport,
    ExecutionSnapshot,
    ExecutorCapabilities,
    FillEvent,
    OrderStatus,
    PaperOrder,
)
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

    COMMANDS AND QUERIES (Phase 6)
    ==============================
    The interface has two halves. ``submit``, ``cancel``, ``cancel_all`` and
    ``poll`` are commands: they change what the venue is being asked to do, and
    their semantics are unchanged by Phase 6. The rest are queries: they answer
    questions about execution's current belief without changing anything.

    The split matters because everything that will later want to *understand*
    execution -- reconciliation, an operator surface, a plan-level control --
    needs the second half and must never reach into an executor's private state
    to get it. An executor that keeps its orders in a dictionary, one that
    keeps them in a database and one that asks a venue over the wire all answer
    the same questions here.
    """

    #: Every implementation must declare whether it can reach a real exchange.
    #: The composition root refuses to construct one that can.
    is_paper: bool = True

    # -- identity ----------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this executor, e.g. ``"paper"``."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Version string for the running implementation."""

    @property
    @abstractmethod
    def capabilities(self) -> ExecutorCapabilities:
        """What this executor claims to implement.

        A claim, not a proof: whether the implementation honours every
        capability it advertises is a validation question.
        """

    # -- commands ----------------------------------------------------------

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
    async def resolve_unknown(
        self,
        client_order_id: str,
        authoritative_status: OrderStatus,
        now_ms: Millis,
    ) -> ExecutionCommandResult:
        """Resolve an UNKNOWN order to a status something authoritative reports.

        THE ONLY WAY OUT OF UNKNOWN, AND IT IS EXPLICIT
        ===============================================
        Nothing resolves an UNKNOWN order on its own -- not a poll, not an
        expiry, not a timeout, not this method firing itself. An order reaches
        UNKNOWN precisely because the platform does not know what happened to
        it, and the only honest way out is for a caller to arrive holding an
        answer from somewhere authoritative.

        In this build there is no such source: the paper venue is the executor
        itself, and it does not learn anything new by being asked twice. The
        method exists so that reconciliation (Phase 7) has a defined way in
        when it does have an answer, rather than reaching into the OMS.

        Implementations must refuse to resolve an order that is not UNKNOWN,
        and must never guess at ``authoritative_status``.
        """

    # -- queries -----------------------------------------------------------

    @abstractmethod
    def open_orders(self) -> list[PaperOrder]:
        """Orders known to be working right now. Excludes UNKNOWN."""

    @abstractmethod
    def outstanding_orders(self) -> list[PaperOrder]:
        """Orders whose final venue truth is not yet known.

        A superset of :meth:`open_orders`: it also includes UNKNOWN orders,
        which may still be working even though nobody knows. See
        ``PaperOrder.is_outstanding``.
        """

    @abstractmethod
    def unknown_orders(self) -> list[PaperOrder]:
        """Orders explicitly in the UNKNOWN state."""

    @abstractmethod
    def all_orders(self) -> list[PaperOrder]:
        """Every order the executor currently holds, whatever its state."""

    @abstractmethod
    def get_order(self, client_order_id: str) -> PaperOrder | None:
        """One order by its client id, or ``None``."""

    @abstractmethod
    def orders_for_plan(self, plan_id: str) -> list[PaperOrder]:
        """Every order created for one plan."""

    @abstractmethod
    def execution_snapshot(self, now_ms: Millis) -> ExecutionSnapshot:
        """One canonical view of execution's belief at ``now_ms``.

        The instant is supplied, never read, so a replay can ask what execution
        believed at a logical time and get a deterministic answer.
        """

    # -- retention ---------------------------------------------------------

    @abstractmethod
    def compact_terminal_state(self, *, unsealed_fills: set[str]) -> int:
        """Release per-order bookkeeping for orders that are finished with.

        ``unsealed_fills`` are the fill ids some other layer has yet to
        reconcile; an order holding one of them is not finished with, whatever
        its status says. An order that is not terminal is never eligible, and
        an UNKNOWN order is never terminal.

        Returns the number of records released. An implementation with nothing
        to release returns zero rather than pretending.
        """
