"""Explicit completion tracking for multi-agent request/response.

The orchestrator used to publish an opportunity, call ``bus.drain()``, and
assume every agent had answered. That works only because the in-process bus
dispatches synchronously; on any networked transport ``drain()`` returns long
before remote agents have replied, and consensus is then computed with zero
opinions — every opportunity rejected as CONSENSUS_INCOMPLETE.

The fix is to stop inferring completion from the transport and to track it
directly: name the responders you require, wait for their responses against a
deadline, and report exactly which ones are missing. This behaves identically
whether the responders are in-process or remote, so it is correct under both
buses and stays correct if agents are ever split into separate processes.

A timeout is not an error here. A missing agent is a legitimate, expected
outcome that the consensus engine already models (an absent agent is never a
neutral vote), so the barrier reports *who* answered rather than raising.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from core.clock import Clock
from core.models.common import AgentId, Millis


@dataclass
class BarrierResult:
    """What actually arrived before the deadline."""

    correlation_id: str
    responded: set[AgentId]
    required: set[AgentId]
    #: True when every required responder answered before the deadline.
    complete: bool
    waited_ms: int
    timed_out: bool

    @property
    def missing(self) -> set[AgentId]:
        return self.required - self.responded


@dataclass
class _Pending:
    required: set[AgentId]
    responded: set[AgentId] = field(default_factory=set)
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def record(self, agent: AgentId) -> None:
        self.responded.add(agent)
        if self.required <= self.responded:
            self.event.set()


class ResponseBarrier:
    """Tracks which agents have responded to which correlation id.

    Registration happens *before* the request is published, so a response that
    arrives faster than the caller can start waiting is never missed.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._pending: dict[str, _Pending] = {}
        #: Responses seen for a correlation id that was never registered, or
        #: that arrived after the wait completed. Kept bounded; used only to
        #: make late arrivals observable rather than silently dropped.
        self.late_responses = 0

    def expect(self, correlation_id: str, required: set[AgentId]) -> None:
        """Declare which agents must answer. Call before publishing the request."""
        self._pending[correlation_id] = _Pending(required=set(required))

    def record(self, correlation_id: str, agent: AgentId) -> None:
        """Note that ``agent`` has responded. Safe to call for unknown ids."""
        pending = self._pending.get(correlation_id)
        if pending is None:
            self.late_responses += 1
            return
        pending.record(agent)

    def forget(self, correlation_id: str) -> None:
        self._pending.pop(correlation_id, None)

    def responded(self, correlation_id: str) -> set[AgentId]:
        pending = self._pending.get(correlation_id)
        return set(pending.responded) if pending else set()

    @property
    def outstanding(self) -> int:
        return len(self._pending)

    async def wait(self, correlation_id: str, timeout_ms: int) -> BarrierResult:
        """Wait for every required responder, or until ``timeout_ms`` elapses.

        The wait is driven by :meth:`Clock.sleep`, so a replay or a test with a
        manual clock controls it exactly rather than waiting on wall time.
        """
        started: Millis = self.clock.now_ms()
        pending = self._pending.get(correlation_id)
        if pending is None:
            return BarrierResult(
                correlation_id=correlation_id,
                responded=set(),
                required=set(),
                complete=True,
                waited_ms=0,
                timed_out=False,
            )

        timed_out = False
        if not pending.event.is_set():
            # Race the completion signal against a clock-driven deadline. Both
            # are cancelled on exit so neither leaks a task.
            waiter = asyncio.ensure_future(pending.event.wait())
            timer = asyncio.ensure_future(self.clock.sleep(timeout_ms / 1000.0))
            try:
                done, _ = await asyncio.wait(
                    {waiter, timer}, return_when=asyncio.FIRST_COMPLETED
                )
                timed_out = waiter not in done
            finally:
                for task in (waiter, timer):
                    if not task.done():
                        task.cancel()
                        # The cancellation is the point; whatever the task
                        # raises on its way out is not this caller's problem.
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

        result = BarrierResult(
            correlation_id=correlation_id,
            responded=set(pending.responded),
            required=set(pending.required),
            complete=pending.required <= pending.responded,
            waited_ms=self.clock.now_ms() - started,
            timed_out=timed_out and not (pending.required <= pending.responded),
        )
        self.forget(correlation_id)
        return result


__all__ = ["BarrierResult", "ResponseBarrier"]
