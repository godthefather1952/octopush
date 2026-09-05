"""NORO's valuation model.

NORO owns one question: *relative to independent market evidence, is the
proposed direction actually mispriced?* ZEPHR owns the other one — whether the
trade can be executed profitably at size. Nothing here walks a book, models
slippage, charges a fee or asks how much could be traded.

The estimator is a **reliability-weighted median** of contributor prices:

    price_v   = mid_v, displaced towards the microprice by at most
                ``microprice_max_displacement_bps``
    r_v       = min(1, near_touch_notional_v / reliability_saturation_notional)
    benchmark = weighted_median({(price_v, r_v)})

Three properties make that the right shape here:

* The median always **lies within contributor prices**, so the benchmark is a
  price some venue is genuinely quoting rather than an average of two that
  nobody is.
* One extreme venue **cannot drag it**. A liquidity-weighted *mean* moves with
  every outlier in proportion to its size, which is exactly how a single
  enormous venue came to define fair value and dilute the outlier detection it
  was supposed to perform.
* The weight is **bounded at 1.0**. Depth past the saturation point buys no
  further influence, so the weight expresses *how much price discovery stands
  behind this quote* — reliability — and not *how much could be traded here*,
  which is not NORO's question and would duplicate ZEPHR.

Near-touch depth comes from an exact TIDAL bucket. A venue with no measurement
at the requested distance is **excluded**, never backfilled with whole-book
depth: those two numbers answer different questions and differ by orders of
magnitude.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from core.config import NoroConfig
from core.models.common import Millis
from core.models.market import VenueMarketState, safe_bps


@dataclass(frozen=True)
class VenueValuation:
    """One venue's contribution to a valuation."""

    venue: str
    symbol: str
    price: float
    #: Notional resting within the configured window of this venue's mid, on
    #: the thinner side. Evidence of price discovery, reported for
    #: observability. It is *not* an executable size.
    near_touch_notional: float
    #: ``near_touch_notional`` mapped into ``(0, 1]``. Bounded on purpose: see
    #: the module docstring.
    reliability: float
    #: Exchange observation time behind this contributor, if known.
    exchange_ts: Millis | None


@dataclass(frozen=True)
class FairValue:
    """A benchmark price and the contributors that produced it."""

    symbol: str
    fair_value: float
    venues: tuple[VenueValuation, ...]
    #: Widest absolute deviation of any contributor from the benchmark, in bps.
    #: Zero for a single contributor, which is why breadth is scored
    #: separately from agreement.
    dispersion_bps: float

    @property
    def contributor_count(self) -> int:
        return len(self.venues)

    @property
    def total_near_touch_notional(self) -> float:
        return sum(v.near_touch_notional for v in self.venues)

    @property
    def mean_reliability(self) -> float:
        if not self.venues:
            return 0.0
        return sum(v.reliability for v in self.venues) / len(self.venues)

    def contributor(self, venue: str) -> VenueValuation | None:
        for valuation in self.venues:
            if valuation.venue == venue:
                return valuation
        return None

    def deviation_of(self, price: float) -> float | None:
        """``price`` relative to the benchmark, in bps. ``None`` if unusable."""
        return safe_bps(price - self.fair_value, self.fair_value)


def venue_price(state: VenueMarketState, config: NoroConfig) -> float | None:
    """One venue's valuation price: its mid, nudged towards the microprice.

    The microprice leans towards the side about to be consumed, so it leads the
    mid slightly and is genuine information. But it lives strictly between bid
    and ask, so on a wide book its distance from mid scales with the spread — a
    40 bps market can hand the valuation a 10 bps displacement out of nothing
    but touch imbalance, and that then outweighs every other venue in the
    benchmark.

    So the displacement is weighted *and then capped*:

        displacement_bps = bps(microprice - mid) * microprice_weight
        price            = mid * (1 + clamp(displacement_bps, +/- cap) / 10_000)

    On a tight book the cap never binds and this is the old blend exactly. On a
    wide one, touch imbalance refines the price by at most ``cap`` bps instead
    of dominating it.
    """
    mid = state.metrics.mid
    if mid is None or not math.isfinite(mid) or mid <= 0:
        return None
    micro = state.metrics.microprice
    if micro is None or not math.isfinite(micro):
        return mid
    raw_bps = safe_bps(micro - mid, mid)
    if raw_bps is None:
        return mid
    weight = min(1.0, max(0.0, config.microprice_weight))
    cap = config.microprice_max_displacement_bps
    displacement = max(-cap, min(cap, raw_bps * weight))
    price = mid * (1 + displacement / 10_000)
    return price if math.isfinite(price) and price > 0 else None


def near_touch_notional(state: VenueMarketState, config: NoroConfig) -> float | None:
    """Notional resting within the configured window, on the thinner side.

    ``None`` when TIDAL published no measurement at that distance. The caller
    must then exclude the venue: there is deliberately no fallback to
    whole-book depth, because a venue that measured nothing near the touch
    would come back looking like the deepest contributor in the market.

    The thinner side governs. A venue quoting a wall of bids and nothing on the
    ask is not a venue whose price is well discovered.
    """
    depth = state.metrics.depth_within(config.liquidity_window_bps)
    if depth is None:
        return None
    bid, ask = depth
    if not math.isfinite(bid) or not math.isfinite(ask):
        return None
    return min(bid, ask)


def _reliability(notional: float, config: NoroConfig) -> float:
    return min(1.0, notional / config.reliability_saturation_notional)


def build_contributors(
    symbol: str, states: Sequence[VenueMarketState], config: NoroConfig
) -> list[VenueValuation]:
    """Every venue that can supply valuation evidence for ``symbol``.

    A contributor must be for this symbol, of usable quality, priceable, and
    carry a measured near-touch depth greater than zero. Anything else has no
    evidence to offer and is excluded rather than defaulted.

    Symbol identity is checked here and not only by the caller: two instruments
    sharing a base (``BTC-USDT`` and ``BTC-USD``) are not one instrument, and
    letting one price the other values the stablecoin basis as a bitcoin
    dislocation.
    """
    contributors: list[VenueValuation] = []
    for state in states:
        if state.symbol != symbol or not state.quality.is_usable:
            continue
        price = venue_price(state, config)
        if price is None:
            continue
        notional = near_touch_notional(state, config)
        if notional is None or notional <= 0:
            continue
        contributors.append(
            VenueValuation(
                venue=state.venue,
                symbol=symbol,
                price=price,
                near_touch_notional=notional,
                reliability=_reliability(notional, config),
                exchange_ts=state.exchange_ts,
            )
        )
    # Deterministic order regardless of how the market state was assembled.
    contributors.sort(key=lambda c: c.venue)
    return contributors


def weighted_median(contributors: Sequence[VenueValuation]) -> float | None:
    """Reliability-weighted median of contributor prices.

    Contributors are ordered by ``(price, venue)`` — the venue name breaks
    price ties — so the result never depends on dict or list ordering upstream.
    Walking that order, the median is the first price at which the accumulated
    weight passes half the total; when the accumulated weight lands *exactly*
    on half, the two straddling prices are averaged, which is the standard
    lower/upper weighted-median convention and keeps two equally reliable
    venues symmetric.

    The result always lies within ``[min(price), max(price)]``.
    """
    if not contributors:
        return None
    ordered = sorted(contributors, key=lambda c: (c.price, c.venue))
    total = sum(c.reliability for c in ordered)
    if total <= 0:
        return None
    half = total / 2.0
    cumulative = 0.0
    for index, contributor in enumerate(ordered):
        cumulative += contributor.reliability
        if cumulative > half:
            return contributor.price
        if cumulative == half:
            upper = ordered[index + 1] if index + 1 < len(ordered) else contributor
            return (contributor.price + upper.price) / 2.0
    return ordered[-1].price


def valuation_from(symbol: str, contributors: Sequence[VenueValuation]) -> FairValue | None:
    """A :class:`FairValue` over an already-selected contributor set."""
    benchmark = weighted_median(contributors)
    if benchmark is None or not math.isfinite(benchmark) or benchmark <= 0:
        return None
    dispersion = 0.0
    for contributor in contributors:
        deviation = safe_bps(contributor.price - benchmark, benchmark)
        if deviation is None:
            return None
        dispersion = max(dispersion, abs(deviation))
    return FairValue(
        symbol=symbol,
        fair_value=benchmark,
        venues=tuple(contributors),
        dispersion_bps=dispersion,
    )


def compute_fair_value(
    symbol: str, states: Sequence[VenueMarketState], config: NoroConfig
) -> FairValue | None:
    """Symbol-wide valuation across every usable contributor.

    This is the *diagnostic* view — health, the dashboard, an operator asking
    "what does the market think this is worth". It is **not** what an
    opportunity is judged against: a benchmark that includes the venues under
    judgement confirms them by construction. See
    :meth:`agents.noro.agent.Noro.independent_valuation`.
    """
    return valuation_from(symbol, build_contributors(symbol, states, config))
