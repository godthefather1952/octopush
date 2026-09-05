"""Shared builders and audit-only comparator models for the NORO audit suite.

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

Migrated to NORO v0.2. The valuation API these helpers wrap changed by design
in the Phase 3 remediation: ``usable_liquidity`` became
``near_touch_notional`` with no full-book fallback, ``venue_price`` takes the
whole ``NoroConfig`` so the microprice cap travels with it, and the estimator
is a reliability-weighted median rather than a liquidity-weighted mean. The
old spellings are gone from production on purpose and are not reconstructed
here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from agents.noro.fair_value import (
    FairValue,
    build_contributors,
    compute_fair_value,
    valuation_from,
)
from core.config import NoroConfig
from core.models.common import DataQuality, Side
from core.models.market import (
    DEPTH_BUCKETS_BPS,
    BookMetrics,
    MarketState,
    OrderBookSnapshot,
    PriceLevel,
    VenueMarketState,
)
from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg

START_MS = 1_788_000_000_000
SYMBOL = "BTC-USD"

#: The buckets TIDAL publishes, and -- since the remediation -- the entire
#: domain ``NoroConfig.liquidity_window_bps`` will accept. Before P3-1 was
#: closed the config took any positive float and quietly answered off-bucket
#: requests with whole-book depth.
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
    exchange_ts: int | None = START_MS,
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
    ``full_book`` sets the total (unbucketed) depth. NORO v0.2 never reads it
    -- that was the P3-1 fallback -- so tests set it to a deliberately
    enormous value to prove the fallback stays closed.
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
# Audit-only comparator benchmarks
# ======================================================================
#
# Production (v0.2) computes ONE benchmark: the reliability-weighted median of
# the contributors that are not participating in the opportunity. These are
# alternatives, implemented only to measure how much that choice matters --
# most importantly the liquidity-weighted mean, which is what production used
# BEFORE the remediation and which several regression tests here compare
# against to show the estimator genuinely changed. None of them is a
# recommendation, and none may be wired into production.


@dataclass(frozen=True)
class Contributor:
    venue: str
    price: float
    #: Raw near-touch notional -- the *unbounded* quantity the pre-remediation
    #: estimator weighted by. Production now bounds this into a reliability in
    #: [0, 1]; the comparators keep the raw number precisely so a test can show
    #: what the unbounded version would have done.
    liquidity: float


def contributors(
    states: list[VenueMarketState], config: NoroConfig
) -> list[Contributor]:
    """The (venue, price, raw near-touch notional) triples production sees.

    Built from production's own ``build_contributors``, so the selection rules
    (usable quality, priceable, an exactly-measured bucket with depth on both
    sides) can never drift away from the agent's.
    """
    return [
        Contributor(c.venue, c.price, c.near_touch_notional)
        for c in build_contributors(SYMBOL, states, config)
    ]


def weighted_mean(items: list[Contributor]) -> float | None:
    """The PRE-REMEDIATION estimator, kept as a comparison baseline.

    Production no longer computes this. It is retained so the audit can keep
    showing what it did: move with every outlier in proportion to its raw
    depth, which is how one enormous venue came to define fair value.
    """
    total = sum(c.liquidity for c in items)
    if total <= 0:
        return None
    return sum(c.price * c.liquidity for c in items) / total


def leave_one_out(items: list[Contributor], venue_name: str) -> float | None:
    """The benchmark a venue would be judged against if it were excluded.

    The question this answers: does a venue's own weight in the benchmark
    stop it from ever looking like an outlier? Production v0.2 goes further
    and excludes *both* opportunity venues -- see :func:`independent_of`.
    """
    others = [c for c in items if c.venue != venue_name]
    return weighted_mean(others)


def median_price(items: list[Contributor]) -> float | None:
    return statistics.median(c.price for c in items) if items else None


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
    """Each contributor's distance from the benchmark, in bps.

    v0.2 does not store a per-contributor deviation -- the benchmark a
    contributor is measured against depends on which opportunity is being
    judged, so a deviation baked into the contributor would be meaningless.
    It is derived here instead.
    """
    return {v.venue: fair.deviation_of(v.price) for v in fair.venues}


def reliabilities(fair: FairValue) -> dict[str, float]:
    """Each contributor's bounded reliability weight, in [0, 1].

    Replaces the old ``weights()``, which returned a normalised share of total
    liquidity. Shares had to sum to 1, so one venue's depth necessarily
    suppressed every other venue's influence; a bounded reliability does not,
    which is the point of the change (P3-9).
    """
    return {v.venue: v.reliability for v in fair.venues}


def fair_of(states: list[VenueMarketState], config: NoroConfig) -> FairValue | None:
    """The symbol-wide DIAGNOSTIC valuation, over every usable contributor.

    This is not what an opportunity is judged against -- see
    :func:`independent_of`. Keeping the two apart is the P3-4 fix.
    """
    return compute_fair_value(SYMBOL, states, config)


def independent_of(
    states: list[VenueMarketState],
    config: NoroConfig,
    *,
    exclude: tuple[str, ...],
) -> FairValue | None:
    """The benchmark an opportunity on ``exclude`` is actually judged against.

    Mirrors ``Noro.independent_valuation``: contributors minus the venues
    participating in the opportunity, and ``None`` when nothing independent
    remains.
    """
    independent = [
        c for c in build_contributors(SYMBOL, states, config) if c.venue not in exclude
    ]
    if not independent:
        return None
    return valuation_from(SYMBOL, independent)
