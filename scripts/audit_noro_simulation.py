"""Phase 3 audit, Sections 43 / 44 / 48: NORO against the real simulated market.

Audit-only. Drives the REAL platform on its seeded synthetic market, captures
every detector opportunity and every NORO opinion, and reports the
distributions -- so the structural findings can be checked against what NORO
actually spends its life doing.

    python -m scripts.audit_noro_simulation [ticks]

Also benchmarks ``compute_fair_value`` across venue counts (Section 48).
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, ".")

from agents.noro.fair_value import compute_fair_value
from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import NoroConfig, load_settings, simulated_venues
from core.events import EventType
from core.models.common import AgentId
from simulation.market import default_market
from storage import InMemoryEventStore
from tests.audit.helpers import SYMBOL, venue
from tests.conftest import START_MS


def percentile(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def summarise(name, values, fmt="{:+.4f}"):
    if not values:
        print(f"  {name}: (none)")
        return
    cells = " ".join(
        f"{label}={fmt.format(percentile(values, f))}"
        for label, f in (("p1", 0.01), ("p5", 0.05), ("p25", 0.25),
                         ("p50", 0.50), ("p75", 0.75), ("p95", 0.95),
                         ("p99", 0.99))
    )
    print(f"  {name:<22} n={len(values):<6} {cells}")


async def run_simulation(ticks: int):
    settings = load_settings().model_copy(update={"venues": simulated_venues()})
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    platform = build_platform(
        settings,
        clock=clock,
        bus=bus,
        store=InMemoryEventStore(),
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )

    opportunities = []
    opinions = []

    async def watch(event):
        if event.type is EventType.OPPORTUNITY_DETECTED:
            opportunities.append(event.payload)
        elif (
            event.type is EventType.AGENT_OPINION
            and event.payload.get("agent_id") == AgentId.NORO.value
        ):
            opinions.append(event.payload)

    bus.subscribe(watch, name="audit-capture")

    await platform.start(record=False)
    for _ in range(ticks):
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
    await bus.drain()
    fair_values = dict(platform.noro.fair_values)
    await platform.stop()
    return opportunities, opinions, fair_values


def report(ticks, opportunities, opinions):
    print("=" * 72)
    print(f"SIMULATED MARKET: {ticks} ticks")
    print("=" * 72)
    print(f"  detector opportunities published : {len(opportunities)}")
    print(f"  NORO opinions published          : {len(opinions)}")
    if not opinions:
        print("  (no opinions -- nothing further to report)")
        return

    signals = [o["signal"] for o in opinions]
    confidences = [o["confidence"] for o in opinions]
    edges = [o["detail"]["confirmed_edge_bps"] for o in opinions]
    liquidity = [o["detail"]["total_liquidity"] for o in opinions]
    priced = [o["detail"]["venues_priced"] for o in opinions]
    raw = [o.get("gross_edge_bps") for o in opportunities if o.get("gross_edge_bps")]

    print()
    summarise("signal", signals)
    summarise("confidence", confidences, "{:.4f}")
    summarise("confirmed_edge_bps", edges)
    summarise("total_liquidity", liquidity, "{:,.0f}")
    summarise("opportunity edge_bps", raw)
    print(f"  venues_priced: {sorted(set(priced))}")

    print()
    print("  signal buckets:")
    buckets = [
        ("[-1, -0.75)", -1.0, -0.75), ("[-0.75, -0.5)", -0.75, -0.5),
        ("[-0.5, -0.25)", -0.5, -0.25), ("[-0.25, 0)", -0.25, 0.0),
        ("(0, 0.25]", 0.0, 0.25), ("(0.25, 0.5]", 0.25, 0.5),
        ("(0.5, 0.75]", 0.5, 0.75), ("(0.75, 1)", 0.75, 1.0),
    ]
    zeros = sum(1 for s in signals if s == 0)
    ones = sum(1 for s in signals if s >= 1.0)
    minus_ones = sum(1 for s in signals if s <= -1.0)
    print(f"    {'== -1':<16}: {minus_ones:>6}")
    for label, low, high in buckets:
        count = sum(1 for s in signals if low <= s < high and s not in (0.0,))
        print(f"    {label:<16}: {count:>6}")
    print(f"    {'== 0':<16}: {zeros:>6}")
    print(f"    {'== 1':<16}: {ones:>6}")

    print()
    positive = sum(1 for s in signals if s > 0)
    negative = sum(1 for s in signals if s < 0)
    print("  REJECTION VALUE (Section 44)")
    print(f"    opportunities detected      : {len(opportunities)}")
    print(f"    NORO opinions returned      : {len(opinions)}")
    print(f"    NORO missing (no opinion)   : "
          f"{max(0, len(opportunities) - len(opinions))}")
    print(f"    NORO positive               : {positive} "
          f"({100 * positive / len(signals):.1f}%)")
    print(f"    NORO neutral                : {zeros}")
    print(f"    NORO negative               : {negative} "
          f"({100 * negative / len(signals):.1f}%)")
    print(f"    saturated at +1             : {ones} "
          f"({100 * ones / len(signals):.1f}%)")


def benchmark():
    print()
    print("=" * 72)
    print("compute_fair_value SCALING (Section 48 / 66)")
    print("=" * 72)
    config = NoroConfig()
    print(f"{'venues':>7} {'calls':>8} {'total ms':>10} {'us/call':>10} "
          f"{'us/venue':>10} {'relative':>10}")
    baseline = None
    for count in (2, 5, 10, 25, 50, 100):
        states = [
            venue(f"V{i}", 100.0 + i * 0.01, liquidity=100_000.0 + i)
            for i in range(count)
        ]
        calls = max(200, 20_000 // count)
        compute_fair_value(SYMBOL, states, config)  # warm
        started = time.perf_counter()
        for _ in range(calls):
            compute_fair_value(SYMBOL, states, config)
        elapsed_ms = (time.perf_counter() - started) * 1000
        us_per_call = elapsed_ms * 1000 / calls
        if baseline is None:
            baseline = us_per_call / count
        print(f"{count:>7} {calls:>8} {elapsed_ms:>10.2f} {us_per_call:>10.2f} "
              f"{us_per_call / count:>10.3f} "
              f"{(us_per_call / count) / baseline:>10.2f}x")


def benchmark_on_market_state():
    print()
    print("=" * 72)
    print("Noro.on_market_state SCALING (symbols x 2 venues)")
    print("=" * 72)
    from tests.audit.helpers import market
    from tests.audit.noro_fixtures import build_noro

    print(f"{'symbols':>8} {'calls':>8} {'total ms':>10} {'us/call':>10}")
    for symbol_count in (2, 10, 100):
        symbols = [f"S{i}-USD" for i in range(symbol_count)]
        noro = build_noro(NoroConfig(), symbols=symbols)
        state = market(
            *[
                venue(f"V{v}", 100.0 + v * 0.01, symbol=symbol)
                for symbol in symbols
                for v in range(2)
            ]
        )
        calls = max(50, 2_000 // symbol_count)
        noro.on_market_state(state)  # warm
        started = time.perf_counter()
        for _ in range(calls):
            noro.on_market_state(state)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"{symbol_count:>8} {calls:>8} {elapsed_ms:>10.2f} "
              f"{elapsed_ms * 1000 / calls:>10.2f}")


def main() -> None:
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 5_000
    opportunities, opinions, _ = asyncio.run(run_simulation(ticks))
    report(ticks, opportunities, opinions)
    benchmark()
    benchmark_on_market_state()


if __name__ == "__main__":
    main()
