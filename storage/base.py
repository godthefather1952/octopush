"""Event store interface.

The store is the system's memory.  Everything that happened — market data,
agent outputs, decisions, orders, fills, failures — goes in, in order, so a
session can be replayed exactly.

SESSION LIFECYCLE (Phase 2 Batch 2)
===================================
"Replayable" is a claim about *completeness*, and completeness is not
something ``ended_at IS NOT NULL`` can express. A session that ended is not
necessarily a session that kept everything: the pre-Batch-2 recorder could
fail a write, discard the batch, count the loss in RAM only, and still end
the session — leaving a row that looked finished and was not.

So a session now carries an explicit :class:`SessionStatus`, persisted:

``OPEN``
    Recording. Events may be appended. Not safe for exact replay: whatever
    the recorder still held when the process stopped is not here.
``COMPLETE``
    Every event the recorder accepted is durably present and the session was
    finalised cleanly. The only status exact replay accepts by default.
``INCOMPLETE``
    Finalised, but known to be missing history (or otherwise untrustworthy).
    The reason and the number of abandoned events are recorded with it.
``LEGACY_UNVERIFIED``
    Written before this schema existed. It may well be complete — but the
    build that wrote it could not tell, so neither can we. Readable and
    replayable behind an explicit override; never silently certified.

The transitions are deliberately one-way and narrow. There is no resume
protocol in this batch: a session id is used once, and reusing one is an
error rather than something to be quietly reconciled.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from core.events import Event, EventType
from core.models.common import Millis, StrEnum


class SessionStatus(StrEnum):
    """Persisted recording status — see the module docstring."""

    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"

    @property
    def is_terminal(self) -> bool:
        """Whether the session can no longer accept events."""
        return self is not SessionStatus.OPEN

    @property
    def is_verified_complete(self) -> bool:
        """Whether exact replay may trust this session's history."""
        return self is SessionStatus.COMPLETE


# -- errors ----------------------------------------------------------------


class StorageIntegrityError(RuntimeError):
    """Base for every refusal that protects recorded history."""


class SessionAlreadyExists(StorageIntegrityError):
    """Raised when ``start_session`` is given an id the store already holds.

    Every backend used to resolve this differently — the memory store
    replaced the SessionInfo (silently clearing ``ended_at``), SQLite's
    ``INSERT OR REPLACE`` did the same, and PostgreSQL's ``ON CONFLICT``
    updated the metadata but kept ``ended_at``. The same call therefore
    produced three different versions of the truth. It is now one error
    everywhere, and the existing session is left completely untouched.
    """


class UnknownSession(StorageIntegrityError):
    """Raised when a write names a session that was never started.

    Appending to an unknown id used to create storage implicitly, which
    turned a wiring mistake into a second, invisible session.
    """


class SessionNotOpen(StorageIntegrityError):
    """Raised when a write (or a second finalisation) targets a terminal session.

    Appending after finalisation would mean a session reported COMPLETE grew
    afterwards — precisely the claim COMPLETE is supposed to rule out.
    """


class EventIdCollision(StorageIntegrityError):
    """Raised when one session holds two *different* events under one id.

    Storage deliberately treats a re-delivered event as a no-op, because the
    recorder must be able to retry a batch whose commit outcome it never
    learned. That is only sound when the retry carries the *same* event: an
    id reused for different content is a genuine integrity failure, and
    silently keeping whichever arrived first would quietly rewrite history.
    """


# -- session metadata ------------------------------------------------------


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    started_at: Millis
    ended_at: Millis | None
    event_count: int
    label: str = ""
    #: Configuration digest, so a replay can refuse to compare sessions that
    #: were produced by materially different configuration.
    config_hash: str = ""
    #: Recording status — the integrity claim itself, never inferred from
    #: ``ended_at``. See the module docstring.
    status: SessionStatus = SessionStatus.OPEN
    #: Events the recorder accepted and then permanently abandoned. Only ever
    #: nonzero on an INCOMPLETE session; a transient write failure that later
    #: succeeded is not loss.
    events_lost: int = 0
    #: Why a session finalised INCOMPLETE, in plain words.
    failure_reason: str = ""

    @property
    def is_verified_complete(self) -> bool:
        return self.status.is_verified_complete


# -- event identity --------------------------------------------------------

#: Every field the stores persist. Two events sharing an id must agree on all
#: of them to count as the same event (see :class:`EventIdCollision`).
PERSISTED_FIELDS = (
    "id",
    "sequence",
    "ts_ms",
    "type",
    "source",
    "schema_name",
    "schema_version",
    "correlation_id",
    "causation_id",
    "payload",
)


def canonical_payload(payload: object) -> str:
    """Serialise a payload so two equal payloads always compare equal.

    ``sort_keys`` matters: two dicts built in different orders are equal in
    Python but serialise differently, and a retry that rebuilt its payload
    from a different code path would otherwise look like a collision.
    """
    return json.dumps(payload, sort_keys=True, default=str, allow_nan=False)


def event_fingerprint(event: Event) -> tuple:
    """The identity of an event, as storage sees it."""
    return (
        event.id,
        None if event.sequence is None else int(event.sequence),
        int(event.ts_ms),
        event.type.value,
        event.source,
        event.schema_name,
        int(event.schema_version),
        event.correlation_id,
        event.causation_id,
        canonical_payload(event.payload),
    )


def describe_fingerprint_mismatch(stored: tuple, incoming: tuple) -> str:
    """Name the fields that differ, for an actionable error message."""
    diffs = [
        f"{name}: stored={s!r} incoming={i!r}"
        for name, s, i in zip(PERSISTED_FIELDS, stored, incoming, strict=True)
        if s != i
    ]
    return "; ".join(diffs)


class EventStore(ABC):
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def start_session(
        self, session_id: str, started_at: Millis, label: str = "", config_hash: str = ""
    ) -> None:
        """Begin a new session in :attr:`SessionStatus.OPEN`.

        Raises :class:`SessionAlreadyExists` if the id is already known, in
        any status, leaving the existing session entirely unmodified.
        """

    @abstractmethod
    async def finalize_session(
        self,
        session_id: str,
        ended_at: Millis,
        *,
        status: SessionStatus,
        events_lost: int = 0,
        failure_reason: str = "",
    ) -> None:
        """Close a session, recording what its history is actually worth.

        ``status`` must be terminal. Raises :class:`UnknownSession` for an
        id the store never started, and :class:`SessionNotOpen` if it has
        already been finalised — re-finalising would let a COMPLETE claim be
        overwritten (or vice versa) with no record that it happened.
        """

    @abstractmethod
    async def append(self, session_id: str, event: Event) -> None:
        """Append one event to an OPEN session.

        Re-appending an identical event is a no-op (the recorder retries
        batches whose outcome it never learned). Appending a *different*
        event under an id the session already holds raises
        :class:`EventIdCollision`.
        """

    @abstractmethod
    async def append_many(self, session_id: str, events: Iterable[Event]) -> None:
        """Append a batch atomically.

        Either every intended event in the batch is durably present
        afterwards, or the call raises and storage is left as it was, so an
        exact retry converges on the intended history. There is no partial,
        silently-accepted batch.
        """

    @abstractmethod
    def read(
        self,
        session_id: str,
        *,
        types: Iterable[EventType] | None = None,
        start_ms: Millis | None = None,
        end_ms: Millis | None = None,
    ) -> AsyncIterator[Event]:
        """Yield events in deterministic order: (ts_ms, sequence, id)."""

    @abstractmethod
    async def sessions(self) -> list[SessionInfo]: ...

    @abstractmethod
    async def session(self, session_id: str) -> SessionInfo | None: ...

    @abstractmethod
    async def count(self, session_id: str) -> int: ...
