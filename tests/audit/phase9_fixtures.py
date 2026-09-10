"""Focused deterministic fixtures for the Phase 9 OKAPI audit."""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.okapi import Okapi
from agents.okapi.registry import HedgeRegistry, HedgeStore
from core.models.common import DataQuality, Side
from core.models.market import MarketState
from core.models.ops import HedgeIntent
from core.models.portfolio import PortfolioState, PositionState
from tests.conftest import START_MS, make_book, venue_state_from_book


def make_portfolio(
    *positions: tuple[str, str, float, float],
    now_ms: int = START_MS,
) -> PortfolioState:
    """Build a paper portfolio from (venue, symbol, quantity, mark) rows."""
    held: dict[str, PositionState] = {}
    for venue, symbol, quantity, mark in positions:
        position = PositionState(
            venue=venue,
            symbol=symbol,
            quantity=quantity,
            average_entry_price=mark,
            mark_price=mark,
            updated_at=now_ms,
        )
        held[position.key] = position
    return PortfolioState(
        created_at=now_ms,
        source_data_timestamp=now_ms,
        initial_balance=100_000.0,
        cash=100_000.0,
        positions=held,
        peak_equity=100_000.0,
    )


def make_market(
    *rows: tuple[str, str, float, int, DataQuality],
    now_ms: int = START_MS,
    source_data_timestamp: int | None = None,
) -> MarketState:
    """Build a market from (venue, symbol, mid, exchange_ts, quality) rows."""
    states = {}
    timestamps: list[int] = []
    for venue, symbol, mid, exchange_ts, quality in rows:
        book = make_book(venue, symbol, mid, ts=exchange_ts)
        state = venue_state_from_book(book, as_of=now_ms, quality=quality)
        states[f"{venue}:{symbol}"] = state
        timestamps.append(exchange_ts)
    newest = max(timestamps) if timestamps else None
    return MarketState(
        created_at=now_ms,
        source_data_timestamp=(
            newest if source_data_timestamp is None else source_data_timestamp
        ),
        venues=states,
    )


def make_intent(
    *,
    hedge_id: str = "hdg-audit",
    symbol: str = "BTC-USD",
    venue: str = "VENUE_A",
    side: Side = Side.SELL,
    notional: float = 1_000.0,
    current_delta: float = 1_000.0,
    target_delta: float = 0.0,
    now_ms: int = START_MS,
) -> HedgeIntent:
    return HedgeIntent(
        hedge_id=hedge_id,
        created_at=now_ms,
        source_data_timestamp=now_ms,
        symbol=symbol,
        venue=venue,
        side=side,
        notional=notional,
        current_delta=current_delta,
        target_delta=target_delta,
        reason_codes=["UNHEDGED_DELTA"],
        urgency=0.5,
    )


def build_okapi(*, bus, clock, settings, health, store: HedgeStore | None = None) -> Okapi:
    return Okapi(
        bus=bus,
        clock=clock,
        settings=settings,
        health=health,
        hedge_registry=HedgeRegistry(store=store),
    )


@dataclass
class CapturingHedgeStore(HedgeStore):
    writes: list = field(default_factory=list)

    def put_hedge(self, record):
        self.writes.append(record.model_copy(deep=True))

    def get_hedge(self, hedge_id):
        for record in reversed(self.writes):
            if record.hedge_id == hedge_id:
                return record.model_copy(deep=True)
        return None

    def outstanding_hedges(self):
        return [r.model_copy(deep=True) for r in self.writes if r.is_outstanding]


class ExplodingHedgeStore(HedgeStore):
    def put_hedge(self, record):
        raise RuntimeError("phase9 audit store failure")

    def get_hedge(self, hedge_id):
        return None

    def outstanding_hedges(self):
        return []


__all__ = [
    "CapturingHedgeStore",
    "ExplodingHedgeStore",
    "build_okapi",
    "make_intent",
    "make_market",
    "make_portfolio",
]
