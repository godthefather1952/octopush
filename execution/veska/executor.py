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
    """Works execution plans and reports what happened."""

    #: Every implementation must declare whether it can reach a real exchange.
    #: The composition root refuses to construct one that can.
    is_paper: bool = True

    @abstractmethod
    async def submit(self, plan: ExecutionPlan) -> ExecutionReport:
        """Accept a plan and begin working it."""

    @abstractmethod
    async def cancel(self, client_order_id: str) -> None:
        """Request cancellation. The cancel may lose the race to a fill."""

    @abstractmethod
    async def cancel_all(self) -> int:
        """Request cancellation of every live order. Returns the count."""

    @abstractmethod
    async def poll(self, now_ms: Millis) -> list[FillEvent]:
        """Advance simulated execution to ``now_ms`` and return new fills."""

    @abstractmethod
    def open_orders(self) -> list[PaperOrder]:
        ...
