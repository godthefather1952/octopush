"""Structured JSON logging.

One line per event, machine-parseable, with a per-record ``extra`` dict merged
into the top level so log aggregation can filter on domain fields.

Records carry two times, deliberately. ``ts`` is host wall-clock: when the
line was really emitted. ``clock_ms`` is the platform clock's reading, and is
present only when a clock is bound. Under a replay the two diverge by years —
events carry recorded time while the process runs today — and without the
second field a log line cannot be placed against the event it describes.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

#: The platform clock, when one is bound. A context variable rather than a
#: module global so a replay running alongside other work cannot stamp its
#: logical time onto unrelated log lines.
_clock: contextvars.ContextVar[Any | None] = contextvars.ContextVar("tf_log_clock", default=None)


def bind_clock(clock: Any | None) -> Any | None:
    """Bind the clock whose reading appears as ``clock_ms``. Returns the old one."""
    previous = _clock.get()
    _clock.set(clock)
    return previous


@contextlib.contextmanager
def clock_context(clock: Any):
    """Bind ``clock`` for the duration of the block."""
    previous = bind_clock(clock)
    try:
        yield clock
    finally:
        bind_clock(previous)


def _platform_time() -> int | None:
    """The bound clock's reading, or None.

    Never raises: logging must not be the thing that fails, and a log line
    with no ``clock_ms`` is better than a lost line or a wrong number.
    """
    clock = _clock.get()
    if clock is None:
        return None
    try:
        return int(clock.now_ms())
    except Exception:
        return None

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = "trading-floor") -> None:
        super().__init__()
        self.service = service

    @staticmethod
    def _timestamp(created: float) -> str:
        """RFC 3339 / ISO 8601 in UTC with real milliseconds.

        The previous implementation passed ``"%Y-%m-%dT%H:%M:%S.%03dZ"`` to
        ``strftime``, where ``%03d`` is not a millisecond directive — it
        rendered the zero-padded *day of month*, so three lines 250 ms apart
        all carried ``.002Z``. It also used ``time.localtime`` while labelling
        the result ``Z``, which is only correct on a UTC host.
        """
        moment = datetime.fromtimestamp(created, tz=UTC)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self._timestamp(record.created),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        platform_ms = _platform_time()
        if platform_ms is not None:
            payload["clock_ms"] = platform_ms
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s")


def configure_logging(
    level: str = "INFO", fmt: str = "json", service: str = "trading-floor"
) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("asyncio").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
