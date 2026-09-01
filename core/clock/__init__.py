"""Clock abstraction.

Domain code never calls ``time.time()``.  It asks a :class:`Clock`, so that a
replay session can drive the same code with recorded timestamps and produce
identical output.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from abc import ABC, abstractmethod

from core.models.common import Millis


class Clock(ABC):
    """Source of time and of sleeping."""

    @abstractmethod
    def now_ms(self) -> Millis:
        """Current logical time in epoch milliseconds."""

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Advance logical time by ``seconds``."""

    def now_s(self) -> float:
        return self.now_ms() / 1000.0


class SystemClock(Clock):
    """Wall-clock time; used for live paper trading."""

    def now_ms(self) -> Millis:
        return int(time.time() * 1000)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock(Clock):
    """Deterministic clock driven explicitly by tests and by replay.

    ``sleep`` registers a waiter that is released when logical time reaches the
    deadline, so ``asyncio`` code written against real time works unchanged
    while running as fast as the CPU allows.
    """

    def __init__(self, start_ms: Millis = 0) -> None:
        self._now = int(start_ms)
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

    def now_ms(self) -> Millis:
        return self._now

    def set(self, ts_ms: Millis) -> None:
        """Jump forward to ``ts_ms``. Never moves backwards."""
        if ts_ms < self._now:
            raise ValueError(f"clock cannot move backwards: {ts_ms} < {self._now}")
        self._now = int(ts_ms)
        self._release()

    def advance(self, ms: int) -> None:
        self.set(self._now + int(ms))

    async def sleep(self, seconds: float) -> None:
        deadline = self._now + round(seconds * 1000)
        if deadline <= self._now:
            # Yield so cooperative loops still interleave.
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (deadline, next(self._counter), fut))
        await fut

    @property
    def pending_sleepers(self) -> int:
        return len(self._waiters)

    def next_deadline(self) -> Millis | None:
        return self._waiters[0][0] if self._waiters else None

    def _release(self) -> None:
        while self._waiters and self._waiters[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)


__all__ = ["Clock", "ManualClock", "SystemClock"]
