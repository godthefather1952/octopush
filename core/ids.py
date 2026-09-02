"""Identifier generation.

Two requirements pull in opposite directions:

* A live session needs identifiers that never collide, across processes and
  across restarts, with no coordination.
* A replay needs identifiers that are *reproducible*, so two replays of one
  recorded session can be diffed entity by entity.

An audit found the codebase serving only the first: every id was
``uuid4().hex[:12]``. Three replays of the same session produced identical
economic state and completely different identifiers, so a replay could not be
compared to its own recording, and attribution keyed by opportunity id was not
comparable across runs. The 48-bit truncation was a second problem: a 50%
collision chance at ~16.7M ids, and store writes use ``INSERT OR IGNORE``, so
a collision would silently drop an event rather than fail.

The fix is an explicit abstraction with two implementations and an explicit
mode switch. Live runs keep random ids — widened to a full 128 bits. Replay
installs a deterministic generator derived from the session and a per-namespace
counter.

Determinism here rests on call *order*, not on object contents. Deriving an id
from a serialised object would be fragile: adding a field would silently change
every downstream id. The counter approach is stable under such changes, and
call order is already deterministic because economic replay is.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict


class IdGenerator(ABC):
    """Mints identifiers for a namespace such as ``fill`` or ``opp``."""

    #: True when the same call sequence reproduces the same identifiers.
    deterministic: bool = False

    @abstractmethod
    def new_id(self, namespace: str) -> str: ...

    @abstractmethod
    def reset(self) -> None:
        """Return to the initial state. Meaningful only for deterministic
        generators; a no-op for random ones."""


class RandomIdGenerator(IdGenerator):
    """Collision-resistant random identifiers. The default everywhere."""

    deterministic = False

    def new_id(self, namespace: str) -> str:
        # Full 128-bit uuid4, not a truncation: the previous 48-bit form had a
        # realistic collision probability for a long-running session.
        return f"{namespace}-{uuid.uuid4().hex}"

    def reset(self) -> None:
        return None


class DeterministicIdGenerator(IdGenerator):
    """Reproducible identifiers for replay.

    Ids are ``<namespace>-<16 hex>`` derived from
    ``sha256(seed | namespace | ordinal)``. Per-namespace counters mean that
    adding a call in one namespace does not shift ids in another, which keeps
    a diff between two replays readable when the code under test has changed.
    """

    deterministic = True

    def __init__(self, seed: str) -> None:
        self.seed = seed
        self._counters: defaultdict[str, int] = defaultdict(int)

    def new_id(self, namespace: str) -> str:
        ordinal = self._counters[namespace]
        self._counters[namespace] += 1
        digest = hashlib.sha256(f"{self.seed}|{namespace}|{ordinal}".encode()).hexdigest()
        return f"{namespace}-{digest[:16]}"

    def reset(self) -> None:
        self._counters.clear()

    @property
    def issued(self) -> int:
        return sum(self._counters.values())


#: The generator in force. Process-wide by design: replay is a whole-process
#: mode (the CLI builds a fresh platform for it), so a context variable would
#: add inheritance subtleties — background tasks started before the switch
#: would not see it — without buying anything.
_current: IdGenerator = RandomIdGenerator()


def new_id(namespace: str) -> str:
    """Mint an identifier in ``namespace`` using the generator in force."""
    return _current.new_id(namespace)


def current_generator() -> IdGenerator:
    return _current


def set_id_generator(generator: IdGenerator) -> IdGenerator:
    """Install a generator, returning the previous one."""
    global _current
    previous, _current = _current, generator
    return previous


@contextlib.contextmanager
def using_id_generator(generator: IdGenerator):
    """Scope a generator to a block, restoring the previous one on exit."""
    previous = set_id_generator(generator)
    try:
        yield generator
    finally:
        set_id_generator(previous)


def deterministic_ids(seed: str):
    """Convenience: scope deterministic ids derived from ``seed``."""
    return using_id_generator(DeterministicIdGenerator(seed))


__all__ = [
    "DeterministicIdGenerator",
    "IdGenerator",
    "RandomIdGenerator",
    "current_generator",
    "deterministic_ids",
    "new_id",
    "set_id_generator",
    "using_id_generator",
]
