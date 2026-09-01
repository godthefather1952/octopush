"""Deterministic synthetic market generator.

Used by the offline default run, by every integration test and by failure
injection.  Given the same seed it produces byte-identical output, which is
what makes "strategy v1.2 vs v1.3 on exactly the same market" a meaningful
comparison before any recorded session exists.

Two venues quote the same instrument.  Each venue's mid is the common true
price plus a mean-reverting venue basis; a dislocation schedule occasionally
pushes one venue's basis away so that the cross-venue strategy has something
to find.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from core.models.common import Millis, Side
from core.models.market import OrderBookSnapshot, PriceLevel, TradeEvent
from venues.base.messages import BookDelta


@dataclass
class SymbolSpec:
    symbol: str
    initial_price: float
    #: Per-step lognormal volatility of the true price.
    vol: float = 0.0004
    tick_size: float = 0.01
    #: Typical size resting at each level, in base units.
    level_size: float = 0.35
    #: Half-spread in bps for a venue's quotes.
    half_spread_bps: float = 1.5
    levels: int = 12


@dataclass
class VenueSpec:
    venue: str
    #: Standard deviation of the mean-reverting basis, in bps.
    basis_vol_bps: float = 0.8
    #: Mean-reversion speed of the basis, in [0, 1].
    basis_reversion: float = 0.25
    #: Multiplier on resting size — a thinner venue is less executable.
    depth_factor: float = 1.0
    #: Probability a step produces a trade print.
    trade_probability: float = 0.35


@dataclass
class DislocationSpec:
    """A scheduled cross-venue mispricing, so tests can assert on detection."""

    start_step: int
    duration_steps: int
    venue: str
    symbol: str
    magnitude_bps: float


@dataclass
class _BookState:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    sequence: int = 0


class SyntheticMarket:
    """Generates normalised venue messages on a fixed step interval."""

    def __init__(
        self,
        symbols: list[SymbolSpec],
        venues: list[VenueSpec],
        *,
        seed: int = 20260901,
        step_ms: int = 100,
        start_ms: Millis = 1_788_000_000_000,
        snapshot_every: int = 50,
        dislocations: list[DislocationSpec] | None = None,
    ) -> None:
        self.symbols = {s.symbol: s for s in symbols}
        self.venues = {v.venue: v for v in venues}
        self.step_ms = step_ms
        self.start_ms = start_ms
        self.snapshot_every = snapshot_every
        self.dislocations = list(dislocations or [])
        self._rng = random.Random(seed)
        self._step = 0
        self._true_price = {s.symbol: s.initial_price for s in symbols}
        self._basis: dict[tuple[str, str], float] = {
            (v.venue, s.symbol): 0.0 for v in venues for s in symbols
        }
        self._books: dict[tuple[str, str], _BookState] = {
            (v.venue, s.symbol): _BookState() for v in venues for s in symbols
        }

    # -- price process -----------------------------------------------------

    @property
    def step(self) -> int:
        return self._step

    def now_ms(self) -> Millis:
        return self.start_ms + self._step * self.step_ms

    def true_price(self, symbol: str) -> float:
        return self._true_price[symbol]

    def _dislocation_bps(self, venue: str, symbol: str) -> float:
        total = 0.0
        for spec in self.dislocations:
            if spec.venue != venue or spec.symbol != symbol:
                continue
            if spec.start_step <= self._step < spec.start_step + spec.duration_steps:
                total += spec.magnitude_bps
        return total

    def venue_mid(self, venue: str, symbol: str) -> float:
        basis = self._basis[(venue, symbol)] + self._dislocation_bps(venue, symbol)
        return self._true_price[symbol] * (1 + basis / 10_000)

    def _advance_prices(self) -> None:
        for symbol, spec in self.symbols.items():
            shock = self._rng.gauss(0.0, spec.vol)
            self._true_price[symbol] *= math.exp(shock)
        for (venue, symbol), basis in list(self._basis.items()):
            vspec = self.venues[venue]
            pull = -vspec.basis_reversion * basis
            noise = self._rng.gauss(0.0, vspec.basis_vol_bps)
            self._basis[(venue, symbol)] = basis + pull + noise

    # -- book construction -------------------------------------------------

    def _target_book(
        self, venue: str, symbol: str
    ) -> tuple[dict[float, float], dict[float, float]]:
        sspec = self.symbols[symbol]
        vspec = self.venues[venue]
        mid = self.venue_mid(venue, symbol)
        half_spread = mid * sspec.half_spread_bps / 10_000
        tick = sspec.tick_size
        best_bid = math.floor((mid - half_spread) / tick) * tick
        best_ask = math.ceil((mid + half_spread) / tick) * tick
        if best_ask <= best_bid:
            best_ask = best_bid + tick

        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        for i in range(sspec.levels):
            # Size grows with distance from the touch, as real books do.
            growth = 1.0 + 0.25 * i
            jitter_bid = 0.8 + 0.4 * self._rng.random()
            jitter_ask = 0.8 + 0.4 * self._rng.random()
            bid_px = round(best_bid - i * tick * (1 + i * 0.5), 8)
            ask_px = round(best_ask + i * tick * (1 + i * 0.5), 8)
            if bid_px <= 0:
                continue
            bids[bid_px] = round(sspec.level_size * growth * vspec.depth_factor * jitter_bid, 8)
            asks[ask_px] = round(sspec.level_size * growth * vspec.depth_factor * jitter_ask, 8)
        return bids, asks

    @staticmethod
    def _diff(
        old: dict[float, float], new: dict[float, float]
    ) -> list[PriceLevel]:
        """Levels that changed, with removals expressed as size 0."""
        changed: list[PriceLevel] = []
        for price, size in new.items():
            if abs(old.get(price, 0.0) - size) > 1e-12:
                changed.append(PriceLevel(price=price, size=size))
        for price in old:
            if price not in new:
                changed.append(PriceLevel(price=price, size=0.0))
        return changed

    def _snapshot(self, venue: str, symbol: str, ts: Millis) -> OrderBookSnapshot:
        book = self._books[(venue, symbol)]
        bids = [
            PriceLevel(price=p, size=s)
            for p, s in sorted(book.bids.items(), key=lambda kv: -kv[0])
            if s > 0
        ]
        asks = [
            PriceLevel(price=p, size=s)
            for p, s in sorted(book.asks.items(), key=lambda kv: kv[0])
            if s > 0
        ]
        return OrderBookSnapshot(
            venue=venue,
            symbol=symbol,
            exchange_ts=ts,
            received_ts=ts,
            sequence=book.sequence,
            bids=bids,
            asks=asks,
            is_checkpoint=True,
        )

    # -- stepping ----------------------------------------------------------

    def next_step(self) -> list[OrderBookSnapshot | BookDelta | TradeEvent]:
        """Advance one step and return every message generated."""
        self._advance_prices()
        ts = self.now_ms()
        out: list[OrderBookSnapshot | BookDelta | TradeEvent] = []
        emit_snapshot = self._step % self.snapshot_every == 0

        for venue in self.venues:
            for symbol in self.symbols:
                book = self._books[(venue, symbol)]
                new_bids, new_asks = self._target_book(venue, symbol)
                bid_changes = self._diff(book.bids, new_bids)
                ask_changes = self._diff(book.asks, new_asks)
                prev_sequence = book.sequence
                book.sequence += 1
                book.bids = new_bids
                book.asks = new_asks

                if emit_snapshot:
                    out.append(self._snapshot(venue, symbol, ts))
                elif bid_changes or ask_changes:
                    bid_changes.sort(key=lambda level: level.price, reverse=True)
                    ask_changes.sort(key=lambda level: level.price)
                    out.append(
                        BookDelta(
                            venue=venue,
                            symbol=symbol,
                            exchange_ts=ts,
                            received_ts=ts,
                            sequence=book.sequence,
                            prev_sequence=prev_sequence,
                            bids=bid_changes,
                            asks=ask_changes,
                        )
                    )

                vspec = self.venues[venue]
                if self._rng.random() < vspec.trade_probability:
                    aggressor = Side.BUY if self._rng.random() < 0.5 else Side.SELL
                    best = (
                        min(new_asks) if aggressor is Side.BUY else max(new_bids)
                    )
                    out.append(
                        TradeEvent(
                            venue=venue,
                            symbol=symbol,
                            exchange_ts=ts,
                            received_ts=ts,
                            price=best,
                            size=round(
                                self.symbols[symbol].level_size
                                * (0.1 + 0.9 * self._rng.random()),
                                8,
                            ),
                            aggressor=aggressor,
                            trade_id=f"{venue}-{symbol}-{self._step}",
                        )
                    )
        self._step += 1
        return out


def recurring_dislocations(
    symbols: list[str],
    *,
    period_steps: int = 90,
    duration_steps: int = 18,
    # Large enough to survive two taker legs plus spread, slippage and
    # hedge cost: a genuinely stressed-market dislocation. Smaller ones are
    # still produced by the basis process and are *supposed* to be rejected
    # by ZEPHR; that rejection is the system working.
    magnitude_bps: float = 35.0,
    venue: str = "VENUE_B",
    count: int = 40,
    offset_steps: int = 30,
) -> list[DislocationSpec]:
    """A repeating dislocation schedule.

    Real cross-venue arbitrage at the touch is rare; a market that never
    dislocates would exercise detection, sizing, risk and execution exactly
    never.  This schedule guarantees the pipeline is driven end to end, and
    the parameters are explicit so a test can assert on what it injected.

    The magnitude alternates sign so the strategy is not repeatedly handed
    the same direction on the same venue.
    """
    out: list[DislocationSpec] = []
    for index in range(count):
        for offset, symbol in enumerate(symbols):
            out.append(
                DislocationSpec(
                    start_step=offset_steps + index * period_steps + offset * 12,
                    duration_steps=duration_steps,
                    venue=venue,
                    symbol=symbol,
                    magnitude_bps=magnitude_bps * (1 if index % 2 == 0 else -1),
                )
            )
    return out


def default_market(
    *,
    seed: int = 20260901,
    symbols: list[str] | None = None,
    dislocations: list[DislocationSpec] | None = None,
    step_ms: int = 100,
    start_ms: Millis = 1_788_000_000_000,
) -> SyntheticMarket:
    """The two-venue BTC/ETH market used by the default offline run."""
    wanted = symbols or ["BTC-USD", "ETH-USD"]
    if dislocations is None:
        dislocations = recurring_dislocations(wanted)
    catalogue = {
        "BTC-USD": SymbolSpec(symbol="BTC-USD", initial_price=110_000.0, level_size=0.35),
        "ETH-USD": SymbolSpec(
            symbol="ETH-USD",
            initial_price=4_100.0,
            level_size=6.0,
            tick_size=0.01,
            half_spread_bps=2.0,
        ),
    }
    return SyntheticMarket(
        symbols=[catalogue[s] for s in wanted],
        venues=[
            VenueSpec(venue="VENUE_A", depth_factor=1.2, basis_vol_bps=0.7),
            VenueSpec(venue="VENUE_B", depth_factor=0.8, basis_vol_bps=1.1),
        ],
        seed=seed,
        step_ms=step_ms,
        start_ms=start_ms,
        dislocations=dislocations,
    )
