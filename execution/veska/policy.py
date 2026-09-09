"""Execution policy — canonical time-in-force and order-type semantics.

This pure module states what IOC, FOK, POST_ONLY and GTC mean so the router,
preflight gate and paper executor share one vocabulary.

Batch B now enforces the validated portions of that contract: IOC receives one
arrival-time attempt, unsupported FOK is refused before OMS mutation,
POST_ONLY never removes liquidity, and actual crossing determines whether a
GTC limit is a taker. The policy remains decision-free and contains no state or
clock reads; enforcement stays in the executor and submission boundary.
"""

from __future__ import annotations

from core.models.common import OrderType, TimeInForce

#: Every time-in-force the schemas accept.
ALL_TIME_IN_FORCE: frozenset[TimeInForce] = frozenset(TimeInForce)

#: Those that grant exactly one executable attempt on arrival.
IMMEDIATE: frozenset[TimeInForce] = frozenset({TimeInForce.IOC, TimeInForce.FOK})

#: Those whose unfilled remainder may stay on the book.
RESTING: frozenset[TimeInForce] = frozenset({TimeInForce.GTC, TimeInForce.POST_ONLY})

#: Those that must fill in full or not at all.
ALL_OR_NOTHING: frozenset[TimeInForce] = frozenset({TimeInForce.FOK})

#: Those that must never remove liquidity.
MAKER_ONLY: frozenset[TimeInForce] = frozenset({TimeInForce.POST_ONLY})


def is_immediate(tif: TimeInForce) -> bool:
    """One executable attempt on arrival; any remainder terminates at once."""
    return tif in IMMEDIATE


def can_rest(tif: TimeInForce) -> bool:
    """Whether an unfilled remainder may stay working on the book."""
    return tif in RESTING


def requires_full_fill(tif: TimeInForce) -> bool:
    """Whether a partial fill is an illegal outcome for this instruction."""
    return tif in ALL_OR_NOTHING


def must_not_take(tif: TimeInForce) -> bool:
    """Whether this order is forbidden from removing liquidity.

    A post-only order that would cross on arrival is rejected or repriced by a
    real venue precisely so that it cannot take. That is the meaning; enforcing
    it is a later pass's work.
    """
    return tif in MAKER_ONLY


def expects_maker_fee(tif: TimeInForce) -> bool:
    """Whether a fill under this instruction should be priced at the maker tier.

    Only true where the instruction guarantees the order added liquidity. GTC
    can do either, so it is not included: its liquidity flag has to come from
    what actually happened, not from the instruction.
    """
    return tif in MAKER_ONLY


def crosses_by_construction(order_type: OrderType, tif: TimeInForce) -> bool:
    """Whether this combination is an aggressive instruction.

    A MARKET order always crosses. A LIMIT order crosses when its
    time-in-force says it gets one immediate attempt.
    """
    return order_type is OrderType.MARKET or is_immediate(tif)


def describe(order_type: OrderType, tif: TimeInForce) -> str:
    """A short human-readable statement of the instruction's meaning."""
    parts = [f"{order_type.value} {tif.value}"]
    parts.append("aggressive" if crosses_by_construction(order_type, tif) else "passive")
    if requires_full_fill(tif):
        parts.append("all-or-nothing")
    if must_not_take(tif):
        parts.append("must not take")
    if can_rest(tif):
        parts.append("may rest")
    else:
        parts.append("terminates on arrival")
    return "; ".join(parts)


__all__ = [
    "ALL_OR_NOTHING",
    "ALL_TIME_IN_FORCE",
    "IMMEDIATE",
    "MAKER_ONLY",
    "RESTING",
    "can_rest",
    "crosses_by_construction",
    "describe",
    "expects_maker_fee",
    "is_immediate",
    "must_not_take",
    "requires_full_fill",
]
