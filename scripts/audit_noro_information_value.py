"""Phase 3 audit experiment: does NORO add information to the detector?

Audit-only. Runs the REAL ``CrossVenueDetector`` and the REAL ``Noro`` over a
deterministic grid of two-, three-, four- and five-venue markets, and reports
how often NORO's verdict differs from "yes, this dislocation is real".

    python -m scripts.audit_noro_information_value

Nothing here touches production. The grid is fixed (no RNG seeding required
beyond the explicit lists) so two runs of this script produce identical
numbers.
"""

from __future__ import annotations

import itertools
import random
import statistics
import sys

sys.path.insert(0, ".")

from core.config import NoroConfig
from strategies.cross_venue.detector import find_dislocation
from tests.audit.helpers import SYMBOL, contributors, leave_one_out, venue
from tests.audit.noro_fixtures import opinion_for

CONFIG = NoroConfig()
MIN_EDGE_BPS = 4.0

#: The grid. Every axis the mission named, at deterministic points.
GAPS_BPS = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]
DEPTHS = {
    "tiny": 1_000.0,
    "small": 10_000.0,
    "medium": 100_000.0,
    "large": 500_000.0,
    "huge": 5_000_000.0,
}
IMBALANCE = {"strong bid": (4.0, 0.25), "balanced": (1.0, 1.0), "strong ask": (0.25, 4.0)}
MICRO = {"below": -0.4, "at mid": 0.0, "above": 0.4}
SPREADS_BPS = {"tight": 1.0, "normal": 5.0, "wide": 60.0}


def build_venue(name, mid, depth, imbalance, micro_shift, spread_name):
    bid_mult, ask_mult = IMBALANCE[imbalance]
    spread_bps = SPREADS_BPS[spread_name]
    half = mid * spread_bps / 20_000
    micro = mid + MICRO[micro_shift] * half
    return venue(
        name,
        mid,
        bid_liquidity=DEPTHS[depth] * bid_mult,
        ask_liquidity=DEPTHS[depth] * ask_mult,
        microprice=micro,
        spread_bps=spread_bps,
    )


def percentile(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[index]


def correlation(xs, ys):
    if len(xs) < 2:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / (vx * vy) ** 0.5 if vx > 0 and vy > 0 else float("nan")


def run_two_venue():
    rows = []
    for gap, depth_a, depth_b, imb, micro, spread in itertools.product(
        GAPS_BPS, DEPTHS, DEPTHS, IMBALANCE, MICRO, SPREADS_BPS
    ):
        a = build_venue("VENUE_A", 100.0, depth_a, imb, micro, spread)
        b = build_venue(
            "VENUE_B", 100.0 * (1 + gap / 10_000), depth_b, imb, micro, spread
        )
        dislocation = find_dislocation(SYMBOL, [a, b], None)
        if dislocation is None or dislocation.gross_edge_bps < MIN_EDGE_BPS:
            continue
        opinion = opinion_for(
            [a, b], CONFIG, dislocation.buy_venue, dislocation.sell_venue
        )
        if opinion is None:
            rows.append((dislocation.gross_edge_bps, None, None, None))
            continue
        items = contributors([a, b], CONFIG)
        rows.append(
            (
                dislocation.gross_edge_bps,
                opinion.signal,
                opinion.confidence,
                min(c.liquidity for c in items) / sum(c.liquidity for c in items),
            )
        )
    return rows


def run_multi_venue(extra: int):
    """Two dislocated venues plus ``extra`` anchors that can actually move it.

    The anchors carry a WIDE spread deliberately. A tight anchor priced
    outside the [buy, sell] interval simply becomes the new extreme and the
    detector re-targets the opportunity onto it -- so the pair under
    evaluation changes and NORO confirms the new pair. A wide-spread anchor
    keeps its bid below the sell venue's and its ask above the buy venue's,
    so it never wins either extreme, while its mid/microprice still pulls
    fair value out of the interval. That is the only shape in which a third
    venue can make NORO disagree.
    """
    placements = {
        "far below buy": -3.0,
        "clusters with buy": 0.0,
        "between": 0.5,
        "clusters with sell": 1.0,
        "far above sell": 4.0,
    }
    rows = []
    for gap, depth, anchor_depth, placement, anchor_spread in itertools.product(
        GAPS_BPS,
        ["small", "medium", "large"],
        ["small", "medium", "huge"],
        placements,
        ["normal", "wide"],
    ):
        low, high = 100.0, 100.0 * (1 + gap / 10_000)
        a = build_venue("VENUE_A", low, depth, "balanced", "at mid", "tight")
        b = build_venue("VENUE_B", high, depth, "balanced", "at mid", "tight")
        anchor_price = low + (high - low) * placements[placement]
        anchors = [
            build_venue(f"ANCHOR_{i}", anchor_price, anchor_depth, "balanced",
                        "at mid", anchor_spread)
            for i in range(extra)
        ]
        states = [a, b, *anchors]
        dislocation = find_dislocation(SYMBOL, states, None)
        if dislocation is None or dislocation.gross_edge_bps < MIN_EDGE_BPS:
            continue
        opinion = opinion_for(
            states, CONFIG, dislocation.buy_venue, dislocation.sell_venue
        )
        if opinion is None:
            continue
        retargeted = {dislocation.buy_venue, dislocation.sell_venue} != {
            "VENUE_A", "VENUE_B"
        }
        rows.append((placement, dislocation.gross_edge_bps, opinion.signal,
                     retargeted))
    return rows


def report_two_venue(rows):
    print("=" * 72)
    print("TWO-VENUE INFORMATION VALUE")
    print("=" * 72)
    signals = [s for _, s, _, _ in rows if s is not None]
    missing = sum(1 for _, s, _, _ in rows if s is None)
    print(f"detector opportunities (edge >= {MIN_EDGE_BPS} bps): {len(rows)}")
    print(f"NORO returned no opinion:                            {missing}")
    if not signals:
        return
    buckets = [
        ("<= 0        ", lambda s: s <= 0),
        ("(0, 0.25]   ", lambda s: 0 < s <= 0.25),
        ("(0.25, 0.5] ", lambda s: 0.25 < s <= 0.5),
        ("(0.5, 0.75] ", lambda s: 0.5 < s <= 0.75),
        ("(0.75, 1)   ", lambda s: 0.75 < s < 1),
        ("== 1        ", lambda s: s >= 1),
    ]
    for label, predicate in buckets:
        count = sum(1 for s in signals if predicate(s))
        print(f"  signal {label}: {count:>6}  ({100 * count / len(signals):5.1f}%)")
    print()
    for label, fraction in (("p1", 0.01), ("p5", 0.05), ("p25", 0.25),
                            ("p50", 0.50), ("p75", 0.75), ("p95", 0.95),
                            ("p99", 0.99)):
        print(f"  signal {label:>4}: {percentile(signals, fraction):+.4f}")
    print()
    edges = [e for e, s, _, _ in rows if s is not None]
    balances = [b for _, s, _, b in rows if s is not None]
    confidences = [c for _, s, c, _ in rows if s is not None]
    print(f"  corr(signal, detector edge_bps) = {correlation(edges, signals):+.4f}")
    print(f"  corr(signal, liquidity balance) = {correlation(balances, signals):+.4f}")
    print(f"  confidence min/median/max       = {min(confidences):.3f} / "
          f"{statistics.median(confidences):.3f} / {max(confidences):.3f}")
    rejected = sum(1 for s in signals if s <= 0)
    print(f"\n  REJECTION RATE: {rejected}/{len(signals)} "
          f"({100 * rejected / len(signals):.3f}%)")


def report_multi(rows, venues: int):
    print()
    print("=" * 72)
    print(f"{venues}-VENUE INFORMATION VALUE")
    print("=" * 72)
    if not rows:
        print("  no opportunities generated")
        return
    retargeted = sum(1 for _, _, _, r in rows if r)
    print(f"  opportunities: {len(rows)}  "
          f"(detector re-targeted onto an anchor in {retargeted})")
    by_placement: dict[str, list[float]] = {}
    for placement, _, signal, _ in rows:
        by_placement.setdefault(placement, []).append(signal)
    total_rejected = 0
    for placement in sorted(by_placement):
        signals = by_placement[placement]
        rejected = sum(1 for s in signals if s <= 0)
        total_rejected += rejected
        print(f"  anchor {placement:<20}: {len(signals):>4} opportunities, "
              f"{rejected:>4} rejected ({100 * rejected / len(signals):5.1f}%), "
              f"median signal {statistics.median(signals):+.3f}")
    print(f"\n  REJECTION RATE: {total_rejected}/{len(rows)} "
          f"({100 * total_rejected / len(rows):.3f}%)")


def run_random(venues: int, draws: int = 40_000, seed: int = 23):
    """Seeded random books -- the arm that actually finds the rare rejections.

    The structured grid above sweeps named shapes and finds none, because a
    third venue priced outside the buy/sell interval usually becomes the new
    extreme and the detector re-targets onto it. Rejection needs a venue that
    pulls fair value out of the interval WITHOUT winning either extreme,
    which is a narrow corner of the space that random books reach and a
    hand-built grid does not.
    """
    rng = random.Random(seed)
    emitted = 0
    rejected = []
    for _ in range(draws):
        states = []
        for i in range(venues):
            bid = rng.uniform(99.0, 101.0)
            spread = rng.uniform(0.0005, 0.8)
            ask = bid + spread
            states.append(
                venue(
                    f"V{i}", (bid + ask) / 2, best_bid=bid, best_ask=ask,
                    microprice=bid + rng.random() * spread,
                    liquidity=rng.uniform(1e3, 5e6),
                )
            )
        dislocation = find_dislocation(SYMBOL, states, None)
        if dislocation is None or dislocation.gross_edge_bps < MIN_EDGE_BPS:
            continue
        emitted += 1
        opinion = opinion_for(
            states, CONFIG, dislocation.buy_venue, dislocation.sell_venue
        )
        if opinion is not None and opinion.signal <= 0:
            rejected.append(opinion.signal)
    return emitted, rejected


def report_random():
    print()
    print("=" * 72)
    print("RANDOM-BOOK REJECTION RATE (seeded, deterministic)")
    print("=" * 72)
    print(f"{'venues':>7} {'opportunities':>15} {'rejected':>10} {'rate':>10}")
    for venues in (2, 3, 4, 5, 8):
        emitted, rejected = run_random(venues)
        rate = 100 * len(rejected) / emitted if emitted else 0.0
        print(f"{venues:>7} {emitted:>15,} {len(rejected):>10} {rate:>9.3f}%")


def report_leave_one_out():
    """How often would a leave-one-out benchmark change the verdict?"""
    print()
    print("=" * 72)
    print("SELF-INCLUSION vs LEAVE-ONE-OUT (3-venue grid)")
    print("=" * 72)
    changed_sign = 0
    materially_larger = 0
    total = 0
    for gap, outlier_depth in itertools.product(GAPS_BPS, DEPTHS):
        states = [
            venue("VENUE_A", 100.0, liquidity=100_000.0),
            venue("VENUE_B", 100.0, liquidity=100_000.0),
            venue("VENUE_C", 100.0 * (1 + gap / 10_000),
                  liquidity=DEPTHS[outlier_depth]),
        ]
        from tests.audit.helpers import fair_of

        fair = fair_of(states, CONFIG)
        items = contributors(states, CONFIG)
        for item in items:
            other = leave_one_out(items, item.venue)
            if other is None:
                continue
            total += 1
            loo_dev = (item.price - other) / other * 10_000
            self_dev = fair.deviation(item.venue)
            if loo_dev * self_dev < 0:
                changed_sign += 1
            if abs(self_dev) > 1e-9 and abs(loo_dev) > 2 * abs(self_dev):
                materially_larger += 1
    print(f"  contributor deviations compared: {total}")
    print(f"  sign changed under LOO:          {changed_sign} "
          f"({100 * changed_sign / total:.1f}%)")
    print(f"  |LOO| more than 2x |self|:       {materially_larger} "
          f"({100 * materially_larger / total:.1f}%)")


def main() -> None:
    report_two_venue(run_two_venue())
    for extra in (1, 2, 3):
        report_multi(run_multi_venue(extra), 2 + extra)
    report_random()
    report_leave_one_out()


if __name__ == "__main__":
    main()
