"""Event recorder.

Attaches to the bus as middleware and writes every published event to the
store.  Buffered, because a synchronous write per event would put storage
latency on the trading path; flushed on a size or age trigger and on shutdown.

If the store fails, the recorder reports it and keeps the platform running —
but it raises the failure to the health registry, and a storage failure is a
kill-switch trigger, so the platform stops opening new trades rather than
trading blind with no audit trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.bus import EventBus
from core.clock import Clock
from core.events import Event
from core.models.common import Millis, new_id
from storage.base import EventStore

log = logging.getLogger(__name__)

SERVICE = "RECORDER"


@dataclass
class Recorder:
    store: EventStore
    clock: Clock
    session_id: str = field(default_factory=lambda: new_id("session"))
    buffer_size: int = 200
    flush_interval_ms: int = 1_000
    label: str = ""
    config_hash: str = ""
    _buffer: list[Event] = field(default_factory=list)
    _last_flush: Millis = 0
    events_recorded: int = 0
    failures: int = 0
    healthy: bool = True
    #: Consecutive failed flushes. One transient error is not an outage; a
    #: run of them means the audit trail is gone.
    consecutive_failures: int = 0
    #: Events accepted into the buffer but never successfully persisted.
    events_lost: int = 0

    async def start(self) -> None:
        await self.store.open()
        await self.store.start_session(
            self.session_id, self.clock.now_ms(), self.label, self.config_hash
        )
        self._last_flush = self.clock.now_ms()

    async def stop(self) -> None:
        await self.flush()
        await self.store.end_session(self.session_id, self.clock.now_ms())
        await self.store.close()

    def attach(self, bus: EventBus) -> None:
        bus.add_middleware(self.record)

    async def record(self, event: Event) -> None:
        self._buffer.append(event)
        now = self.clock.now_ms()
        if (
            len(self._buffer) >= self.buffer_size
            or now - self._last_flush >= self.flush_interval_ms
        ):
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            self._last_flush = self.clock.now_ms()
            return
        batch, self._buffer = self._buffer, []
        try:
            await self.store.append_many(self.session_id, batch)
            self.events_recorded += len(batch)
            self.healthy = True
            self.consecutive_failures = 0
        except Exception as exc:
            self.failures += 1
            self.consecutive_failures += 1
            self.healthy = False
            # The batch is gone. Say so numerically rather than only in a log
            # line, so the condition is measurable and can gate trading.
            self.events_lost += len(batch)
            log.error(
                "failed to persist events",
                extra={"count": len(batch), "error": str(exc)},
            )
        finally:
            self._last_flush = self.clock.now_ms()
