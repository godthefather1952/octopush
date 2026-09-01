"""Event bus interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from core.events import Event, EventType

Handler = Callable[[Event], Awaitable[None]]
Middleware = Callable[[Event], Awaitable[None]]


@dataclass
class Subscription:
    name: str
    types: frozenset[EventType] | None
    handler: Handler
    #: Events this subscription has seen; exposed for health/metrics.
    delivered: int = 0
    errors: int = 0
    #: Ids already delivered, so a duplicate publication is a no-op for
    #: handlers that opt into de-duplication.
    _seen: set[str] = field(default_factory=set, repr=False)

    def wants(self, event: Event) -> bool:
        return self.types is None or event.type in self.types


class EventBus(ABC):
    """Publish/subscribe transport between components."""

    @abstractmethod
    async def publish(self, event: Event) -> None: ...

    @abstractmethod
    def subscribe(
        self,
        handler: Handler,
        types: Iterable[EventType] | None = None,
        name: str | None = None,
    ) -> Subscription: ...

    @abstractmethod
    def unsubscribe(self, subscription: Subscription) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def drain(self) -> None:
        """Process everything currently queued, including cascades."""

    @property
    @abstractmethod
    def queue_depth(self) -> int: ...

    def add_middleware(self, middleware: Middleware) -> None:
        """Register a hook invoked for every published event, before delivery.

        Used by the recorder to persist the event stream.
        """
        raise NotImplementedError
