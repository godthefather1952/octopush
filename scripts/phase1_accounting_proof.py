"""Phase 1 polish -- deterministic accounting-proof run.

Drives a fully offline, deterministic simulated paper session (in-memory bus
and store, ``ManualClock``, synthetic market with its default recurring
dislocations) long enough to produce multiple completed trades, then prints
every number the Phase 1 polish mission's accounting proof requires plus the
reconciliation equations against them.

Not a pytest test: a one-shot report script, run manually and pasted into the
validation report. Nothing here is asserted; the reconciliation check is
printed for a human (or this script's own exit code) to judge.
"""

from __future__ import annotations

import asyncio
import sys

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings, simulated_venues
from simulation.market import default_market
from storage import InMemoryEventStore

START_MS = 1_788_000_000_000
TICKS = 1_500


async def main() -> int:
    settings = load_settings().model_copy(update={"venues": simulated_venues()})
    clock = ManualClock(START_MS)
    bus = InMemoryEventBus(raise_on_handler_error=True)
    store = InMemoryEventStore()
    platform = build_platform(
        settings,
        clock=clock,
        bus=bus,
        store=store,
        market=default_market(start_ms=START_MS),
        raise_on_handler_error=True,
    )

    await platform.start(record=True)
    for _ in range(TICKS):
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()

    account = platform.account
    portfolio = account.snapshot()
    trades = platform.orchestrator.scorecard.trades
    closed_trades = [t for t in trades if t.closed_at is not None]
    attribution_total = sum(t.realized_pnl or 0.0 for t in closed_trades)

    open_positions = {k: v for k, v in portfolio.positions.items() if abs(v.quantity) > 1e-9}
    flat = not open_positions

    print("=" * 70)
    print("PHASE 1 ACCOUNTING PROOF -- deterministic simulated session")
    print("=" * 70)
    print(f"ticks run:                 {TICKS}")
    print(f"closed opportunities:      {len(closed_trades)}")
    print(f"open positions:            {len(open_positions)} ({'FLAT' if flat else 'NOT FLAT'})")
    print("-" * 70)
    print(f"initial_balance:           {portfolio.initial_balance:,.2f}")
    print(f"cash:                      {portfolio.cash:,.2f}")
    print(f"equity:                    {portfolio.equity:,.2f}")
    print(f"gross_pnl:                 {portfolio.gross_pnl:,.2f}")
    print(f"fees_paid:                 {portfolio.fees_paid:,.2f}")
    print(f"net_pnl:                   {portfolio.net_pnl:,.2f}")
    print(f"realized_pnl:              {portfolio.realized_pnl:,.2f}")
    print(f"unrealized_pnl:            {portfolio.unrealized_pnl:,.2f}")
    print(f"gross_exposure:            {portfolio.gross_exposure:,.2f}")
    print(f"net_exposure:              {portfolio.net_exposure:,.2f}")
    print(f"sum(attribution.realized_pnl) over {len(closed_trades)} closed trades: "
          f"{attribution_total:,.2f}")
    print("-" * 70)

    eps = 1e-6
    ok = True

    eq_check = portfolio.equity - portfolio.initial_balance
    print(f"equity - initial_balance = {eq_check:,.6f}")
    print(f"net_pnl                  = {portfolio.net_pnl:,.6f}")
    if flat:
        diff = abs(eq_check - portfolio.net_pnl)
        print(f"  |diff| = {diff:.6f}  ->  {'RECONCILES' if diff < eps else 'MISMATCH'}")
        ok &= diff < eps
    else:
        print("  account is NOT flat: equity - initial_balance includes mark-to-market "
              "and is expected to equal net_pnl too, since net_pnl already includes "
              "unrealized_pnl.")
        diff = abs(eq_check - portfolio.net_pnl)
        print(f"  |diff| = {diff:.6f}  ->  {'RECONCILES' if diff < eps else 'MISMATCH'}")
        ok &= diff < eps

    gross_minus_fees = portfolio.gross_pnl - portfolio.fees_paid
    diff2 = abs(gross_minus_fees - portfolio.net_pnl)
    print(f"gross_pnl - fees_paid     = {gross_minus_fees:,.6f}")
    print(f"net_pnl                   = {portfolio.net_pnl:,.6f}")
    print(f"  |diff| = {diff2:.6f}  ->  {'RECONCILES' if diff2 < eps else 'MISMATCH'}")
    ok &= diff2 < eps

    if not flat:
        realized_check = portfolio.realized_pnl + portfolio.unrealized_pnl - portfolio.fees_paid
        diff3 = abs(realized_check - portfolio.net_pnl)
        print(f"realized + unrealized - fees_paid = {realized_check:,.6f}")
        print(f"net_pnl                            = {portfolio.net_pnl:,.6f}")
        print(f"  |diff| = {diff3:.6f}  ->  {'RECONCILES' if diff3 < eps else 'MISMATCH'}")
        ok &= diff3 < eps

    print("-" * 70)
    print(
        "Attribution category reconciliation. sum(attribution.realized_pnl) only "
        "covers opportunities that were fully CLOSED during this run; a still-open "
        "opportunity's accumulated-so-far contribution sits in its live "
        "AttributionBuilder (not yet in scorecard.trades), and OKAPI hedge fills "
        "carry a hedge_id as their correlation_id, which never matches an "
        "opportunity attribution builder at all. Replaying the fill log with the "
        "exact same per-fill delta logic Orchestrator._track_realized_delta uses, "
        "bucketed by category:"
    )
    closed_ids = {t.opportunity_id for t in closed_trades}
    open_ids = set(platform.orchestrator.attributions.keys())
    baseline: dict[str, float] = {}
    gross_closed = fees_closed = gross_open = fees_open = gross_other = fees_other = 0.0
    for f in account.fill_log:
        key = f"{f.venue}:{f.symbol}"
        position = account.positions.get(key)
        current = position.realized_pnl if position is not None else 0.0
        delta = current - baseline.get(key, 0.0)
        baseline[key] = current
        cid = f.correlation_id or ""
        if cid in closed_ids:
            gross_closed += delta
            fees_closed += f.fee
        elif cid in open_ids:
            gross_open += delta
            fees_open += f.fee
        else:
            gross_other += delta
            fees_other += f.fee

    print(f"  closed opportunities:  gross={gross_closed:,.6f}  fees={fees_closed:,.6f}  "
          f"net={gross_closed - fees_closed:,.6f}")
    print(f"  still-open opportunities (pending, not yet in scorecard.trades): "
          f"gross={gross_open:,.6f}  fees={fees_open:,.6f}  net={gross_open - fees_open:,.6f}")
    print(f"  hedges / fills outside any tracked opportunity: "
          f"gross={gross_other:,.6f}  fees={fees_other:,.6f}  net={gross_other - fees_other:,.6f}")

    diff4 = abs((gross_closed - fees_closed) - attribution_total)
    print(f"  closed-category net ({gross_closed - fees_closed:,.6f}) vs "
          f"sum(attribution.realized_pnl) ({attribution_total:,.6f}): "
          f"|diff|={diff4:.6f}  ->  {'RECONCILES' if diff4 < eps else 'MISMATCH'}")
    ok &= diff4 < eps

    total_net = (gross_closed - fees_closed) + (gross_open - fees_open) + (gross_other - fees_other)
    account_net_realized = portfolio.realized_pnl - portfolio.fees_paid
    diff5 = abs(total_net - account_net_realized)
    print(f"  sum of all three categories ({total_net:,.6f}) vs "
          f"realized_pnl - fees_paid ({account_net_realized:,.6f}): "
          f"|diff|={diff5:.6f}  ->  {'RECONCILES' if diff5 < eps else 'MISMATCH'}")
    ok &= diff5 < eps

    print("=" * 70)
    print("OVERALL:", "RECONCILES" if ok else "MISMATCH -- SEE ABOVE")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
