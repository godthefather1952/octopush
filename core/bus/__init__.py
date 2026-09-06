from core.bus.base import (
    DEFAULT_DELIVERY_WINDOW,
    DeliveryOutcomeWindow,
    EventBus,
    Handler,
    Middleware,
    Subscription,
)
from core.bus.memory import InMemoryEventBus

__all__ = [
    "DEFAULT_DELIVERY_WINDOW",
    "DeliveryOutcomeWindow",
    "EventBus",
    "Handler",
    "InMemoryEventBus",
    "Middleware",
    "Subscription",
    "build_bus",
]


def build_bus(kind: str, redis_url: str | None = None, **kwargs) -> EventBus:
    """Factory used by the composition root."""
    if kind == "memory":
        return InMemoryEventBus(**kwargs)
    if kind == "redis":
        from core.bus.redis_bus import RedisStreamBus

        if not redis_url:
            raise ValueError("redis bus requires a url")
        return RedisStreamBus(redis_url, **kwargs)
    raise ValueError(f"unknown bus kind: {kind}")
