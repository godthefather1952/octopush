"""Shared builders and audit-only comparator models for the Phase 3 NORO audit.

Nothing here is production code, and nothing here may be wired into
production. The comparators exist to answer one question with evidence rather
than argument: *would a different benchmark materially change what NORO
concludes?*

The builders deliberately construct ``VenueMarketState`` by hand rather than
through ``tests/conftest.venue_state_from_book``. That helper derives depth
buckets from the book it is given, which is convenient but makes it impossible
to construct the shapes this audit needs -- a venue with tiny near-touch depth
and an enormous far book, say. Every metric here is set explicitly so the
audit controls exactly one variable at a time.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from agents.noro.fair_value import FairValue, compute_fair_value, venue_price
from agents.tidal.metrics import DEPTH_BUCKETS_BPS
from core.config import NoroConfig
from core.models.common import DataQuality, Side
from core.models.market import (
    BookMetrics,
    MarketState,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg

START_MS = 1_788_000_000_000
SYMBOL = "BTC-USD"

#: The buckets TIDAL actually publishes. NORO's config accepts any positive
#: float, so this tuple is the real domain of "a window that will hit a
#: measured bucket".
PUBLISHED_BUCKETS = DEPTH_BUCKETS_BPS


def venue(
    name: str,
    mid: float,
    *,
    liquidity: float = 100_000.0,
    bid_liquidity: float | None = None,
    ask_liquidity: float | None = None,
    microprice: float | None = None,
    spread_bps: float = 2.0,
    quality: DataQuality = DataQuality.FRESH,
    symbol: str = SYMBOL,
    exchange_ts: int = START_MS,
    buckets: dict[float, tuple[float, float]] | None = None,
    full_book: tuple[float, float] | None = None,
    book: OrderBookSnapshot | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> VenueMarketState:
    """One venue's state with every metric set explicitly.

    ``buckets`` maps a bps distance to ``(bid_notional, ask_notional)``; when
    omitted every published bucket carries ``liquidity`` on both sides, so the
    venue behaves identically at any window that hits a real bucket.
    ``full_book`` sets the total (unbucketed) depth used by NORO's fallback
    path, defaulting to the same value so the fallback is invisible unless a
    test deliberately makes it visible.
    """
    bid_liq = bid_liquidity if bid_liquidity is not None else liquidity
    ask_liq = ask_liquidity if ask_liquidity is not None else liquidity
    if buckets is None:
        buckets = {b: (bid_liq, ask_liq) for b in PUBLISHED_BUCKETS}
    if full_book is None:
        full_book = (bid_liq, ask_liq)

    # Explicit touch prices win, so a test can build the one shape that
    # matters for H4: a venue whose TOUCH the detector prefers while its
    # mid/microprice says something else.
    if best_bid is None or best_ask is None:
        half = mid * spread_bps / 20_000
        best_bid, best_ask = mid - half, mid + half
    total = full_book[0] + full_book[1]
    metrics = BookMetrics(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        microprice=mid if microprice is None else microprice,
        spread=best_ask - best_bid,
        spread_bps=spread_bps,
        bid_depth_notional=full_book[0],
        ask_depth_notional=full_book[1],
        bid_depth_by_bps={f"{b:g}": v[0] for b, v in buckets.items()},
        ask_depth_by_bps={f"{b:g}": v[1] for b, v in buckets.items()},
        imbalance=(
            (full_book[0] - full_book[1]) / total if total > 0 else 0.0
        ),
    )
    return VenueMarketState(
        venue=name,
        symbol=symbol,
        metrics=metrics,
        book=book,
        exchange_ts=exchange_ts,
        last_update_ts=exchange_ts,
        as_of=START_MS,
        quality=quality,
        latency_ms=5.0,
        connected=True,
    )


def ladder_book(
    name: str,
    mid: float,
    *,
    near_notional: float,
    far_notional: float,
    near_bps: float = 8.0,
    far_bps: float = 40.0,
    symbol: str = SYMBOL,
) -> OrderBookSnapshot:
    """A book with a deliberate near/far liquidity cliff.

    One level just inside ``near_bps`` carrying ``near_notional`` per side, and
    one level out at ``far_bps`` carrying ``far_notional``. Built so that
    depth measured within a tight window is small while total book depth is
    enormous -- the shape that makes NORO's bucket fallback observable.
    """
    near_off = mid * near_bps / 10_000
    far_off = mid * far_bps / 10_000
    return OrderBookSnapshot(
        venue=name,
        symbol=symbol,
        exchange_ts=START_MS,
        received_ts=START_MS,
        sequence=1,
        bids=[
            PriceLevel(price=mid - near_off, size=near_notional / (mid - near_off)),
            PriceLevel(price=mid - far_off, size=far_notional / (mid - far_off)),
        ],
        asks=[
            PriceLevel(price=mid + near_off, size=near_notional / (mid + near_off)),
            PriceLevel(price=mid + far_off, size=far_notional / (mid + far_off)),
        ],
        is_checkpoint=True,
    )


def cliff_venue(
    name: str,
    mid: float,
    *,
    near_notional: float,
    far_notional: float,
    near_bps: float = 8.0,
    **kwargs,
) -> VenueMarketState:
    """A venue whose measured buckets reflect a real near/far liquidity cliff.

    Buckets at or beyond ``near_bps`` see the near level only when the bucket
    covers it; the full-book total sees everything. Exactly the production
    relationship, constructed deterministically.
    """
    buckets = {}
    for bucket in PUBLISHED_BUCKETS:
        inside = near_notional if bucket >= near_bps else 0.0
        buckets[bucket] = (inside, inside)
    total = near_notional + far_notional
    return venue(
        name,
        mid,
        buckets=buckets,
        full_book=(total, total),
        book=ladder_book(
            name, mid, near_notional=near_notional, far_notional=far_notional,
            near_bps=near_bps,
        ),
        **kwargs,
    )


def market(*states: VenueMarketState, created_at: int = START_MS) -> MarketState:
    return MarketState(
        created_at=created_at,
        source_data_timestamp=max(
            (s.exchange_ts for s in states if s.exchange_ts is not None),
            default=None,
        ),
        venues={f"{s.venue}:{s.symbol}": s for s in states},
        consolidated={},
    )


def opportunity(
    buy_venue: str,
    sell_venue: str,
    *,
    symbol: str = SYMBOL,
    created_at: int = START_MS,
    buy_price: float = 100.0,
    sell_price: float = 100.1,
    gross_edge_bps: float = 10.0,
) -> Opportunity:
    opp = Opportunity(
        created_at=created_at,
        source_data_timestamp=created_at,
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol=symbol,
        legs=[
            OpportunityLeg(
                venue=buy_venue, symbol=symbol, side=Side.BUY,
                reference_price=buy_price,
            ),
            OpportunityLeg(
                venue=sell_venue, symbol=symbol, side=Side.SELL,
                reference_price=sell_price,
            ),
        ],
        gross_edge_bps=gross_edge_bps,
        expires_at=created_at + 2_000,
        reason_codes=["CROSS_VENUE_DISLOCATION"],
    )
    opp.correlation_id = opp.opportunity_id
    return opp


# ======================================================================
# Audit-only comparator benchmarks (Section 60)
# ======================================================================
#
# Production computes one benchmark: the liquidity-weighted mean of every
# usable venue, INCLUDING the venue being judged. These are alternatives,
# implemented only to measure how much that choice matters. None of them is a
# recommendation, and none may be wired into production.


@dataclass(frozen=True)
class Contributor:
    venue: str
    price: float
    liquidity: float


def contributors(
    states: list[VenueMarketState], config: NoroConfig
) -> list[Contributor]:
    """The (venue, price, liquidity) triples production would use."""
    from agents.noro.fair_value import usable_liquidity

    out = []
    for state in states:
        if not state.quality.is_usable:
            continue
        price = venue_price(state, config.microprice_weight)
        if price is None:
            continue
        liquidity = usable_liquidity(state, config.liquidity_window_bps)
        if liquidity <= 0:
            continue
        out.append(Contributor(state.venue, price, liquidity))
    return out


def weighted_mean(items: list[Contributor]) -> float | None:
    total = sum(c.liquidity for c in items)
    if total <= 0:
        return None
    return sum(c.price * c.liquidity for c in items) / total


def leave_one_out(items: list[Contributor], venue_name: str) -> float | None:
    """The benchmark a venue would be judged against if it were excluded.

    The question this answers: does a venue's own weight in the benchmark
    stop it from ever looking like an outlier?
    """
    others = [c for c in items if c.venue != venue_name]
    return weighted_mean(others)


def median_price(items: list[Contributor]) -> float | None:
    return statistics.median(c.price for c in items) if items else None


def weighted_median(items: list[Contributor]) -> float | None:
    """The price at which cumulative liquidity crosses half the total."""
    if not items:
        return None
    ordered = sorted(items, key=lambda c: c.price)
    total = sum(c.liquidity for c in ordered)
    if total <= 0:
        return None
    seen = 0.0
    for item in ordered:
        seen += item.liquidity
        if seen >= total / 2:
            return item.price
    return ordered[-1].price


def trimmed_mean(items: list[Contributor], trim: float = 0.2) -> float | None:
    """Unweighted mean after dropping the extreme tails. Needs 3+ venues."""
    if len(items) < 3:
        return weighted_mean(items)
    prices = sorted(c.price for c in items)
    drop = int(len(prices) * trim)
    kept = prices[drop : len(prices) - drop] or prices
    return sum(kept) / len(kept)


def bps(value: float, reference: float) -> float:
    return (value - reference) / reference * 10_000


def deviations(fair: FairValue) -> dict[str, float]:
    return {v.venue: v.deviation_bps for v in fair.venues}


def weights(fair: FairValue) -> dict[str, float]:
    return {v.venue: v.weight for v in fair.venues}


def fair_of(states: list[VenueMarketState], config: NoroConfig) -> FairValue | None:
    return compute_fair_value(SYMBOL, states, config)
