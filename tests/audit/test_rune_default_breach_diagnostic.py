"""Phase 5 — what actually trips the kill switch on the default run?

DIAGNOSTIC. **This test is expected to FAIL while the shipped simulation still
engages `RISK_LIMIT_BREACH`.** Its job is to make CI print the exact state at
the first trigger, so the next remediation is chosen from a measurement rather
than from a guess.

WHY IT EXISTS
=============
Remediation D (P5-18) made MAX_UNHEDGED_EXPOSURE size-sensitive and sized the
default entry down from 25,000 to 9,990.01 per leg. External validation (CI
#39) confirms the sizing is present — every P5-18 direct test passes — and yet
`RISK_LIMIT_BREACH` still engages at `START_MS + 15,200ms`, the same instant it
did before. So the remaining breach is NOT the one P5-18 addressed, and no
further production change is justified until we know which dimension it is and
by how much.

WHAT IT MEASURES
================
The real platform: default market, normal settings, normal RUNE, VESKA,
execution simulator and kill switch. Nothing mocked, nothing stubbed. Every
tick is captured, and the tick that first carries the trigger is reported in
full — the breached dimensions with their observed values and limits, the entry
authorisations that preceded it with their MAX_UNHEDGED_EXPOSURE projections,
the orders those entries became, and a mark-versus-expected comparison for the
dominant one-sided position.

That last part is the point of the whole exercise. If the breach is unhedged
exposure, the residual is `quantity x price`, and the question is which price
moved: the fill landing away from what VESKA sized against (execution
slippage), the mark moving after the trade was sized (mark-to-market drift), or
a residual carried in from earlier. Those three have different fixes, and the
report distinguishes them instead of assuming one.

WHAT IT DOES NOT DO
===================
It does not propose a buffer, tune a limit, or clear the switch. It reports.
"""

from __future__ import annotations

from core.events import EventType
from risk.kill_switch import KillSwitchInputs, live_risk_breaches

#: Comfortably past the externally observed first trigger at tick ~152.
TICKS = 250


def _fmt(value, places: int = 4) -> str:
    if value is None:
        return "None"
    if isinstance(value, float):
        return f"{round(value, places)}"
    return str(value)


class TickSample:
    """Everything observable at the end of one orchestrator tick."""

    def __init__(self, tick: int, platform) -> None:
        orchestrator = platform.orchestrator
        portfolio = platform.account.snapshot()
        settings = platform.settings

        self.tick = tick
        self.tick_time = orchestrator.tick_time

        self.gross_exposure = portfolio.gross_exposure
        self.net_exposure = portfolio.net_exposure
        self.equity = portfolio.equity
        self.venue_exposure = portfolio.exposure_by_venue()
        self.positions = [
            {
                "venue": p.venue,
                "symbol": p.symbol,
                "quantity": p.quantity,
                "average_entry_price": p.average_entry_price,
                "mark_price": p.mark_price,
                "notional": p.notional,
                "signed_notional": p.signed_notional,
            }
            for p in portfolio.positions.values()
            if not p.is_flat
        ]

        self.committed = orchestrator._current_committed_exposure()
        self.strategy_exposure = orchestrator._current_strategy_exposure()
        self.actual_unhedged = platform.okapi.total_unhedged(portfolio)

        self.open_orders = [
            {
                "client_order_id": o.client_order_id,
                "intent_id": o.intent_id,
                "correlation_id": o.correlation_id,
                "venue": o.venue,
                "symbol": o.symbol,
                "side": o.side.value,
                "quantity": o.quantity,
                "filled_quantity": o.filled_quantity,
                "remaining_quantity": o.remaining_quantity,
                "expected_price": o.expected_price,
                "limit_price": o.limit_price,
                "status": o.status.value,
            }
            for o in platform.state.orders.values()
            if not o.is_terminal
        ]

        self.utilization = platform.state.risk_utilization
        self.kill_switch_triggers = list(platform.kill_switch.state.triggered_by)
        self.halt_new_trades = platform.kill_switch.state.halt_new_trades

        # The exposure inputs `_protect` builds, rebuilt here from the same
        # helpers. `live_risk_breaches` reads only these five plus settings —
        # health, reconciliation, market-data and storage inputs are consumed by
        # OTHER predicates and are deliberately left at their defaults, since
        # including them would suggest they influence this classification.
        self.inputs = KillSwitchInputs(
            portfolio=portfolio,
            health=platform.state.health,
            committed_exposure=self.committed,
            strategy_exposure=self.strategy_exposure,
            unhedged_notional=self.actual_unhedged,
        )
        self.breaches = live_risk_breaches(self.inputs, settings)

    # -- merged current values, matching the predicate's own arithmetic ----

    @property
    def gross_current(self) -> float:
        return self.gross_exposure + self.committed.gross_exposure

    @property
    def net_current(self) -> float:
        return abs(self.net_exposure + self.committed.net_exposure)

    @property
    def leverage_current(self) -> float | None:
        if self.equity <= 0:
            return None
        return self.gross_current / self.equity

    @property
    def venue_current(self) -> tuple[str | None, float]:
        merged = dict(self.venue_exposure)
        for venue, amount in self.committed.venue_exposure.items():
            merged[venue] = merged.get(venue, 0.0) + amount
        if not merged:
            return (None, 0.0)
        worst = max(merged, key=lambda v: merged[v])
        return (worst, merged[worst])

    @property
    def position_current(self) -> tuple[str | None, float]:
        merged = {
            f"{p['venue']}:{p['symbol']}": p["notional"] for p in self.positions
        }
        for key, amount in self.committed.position_exposure.items():
            merged[key] = merged.get(key, 0.0) + amount
        if not merged:
            return (None, 0.0)
        worst = max(merged, key=lambda k: merged[k])
        return (worst, merged[worst])

    @property
    def dominant_position(self) -> dict | None:
        """The largest one-sided position, which is what an unhedged breach is
        made of."""
        if not self.positions:
            return None
        return max(self.positions, key=lambda p: abs(p["signed_notional"]))


class EntryAuthorisation:
    """One approved entry, with the unhedged projection RUNE sized it against."""

    def __init__(self, tick: int, payload: dict) -> None:
        gate = next(
            (
                g
                for g in payload.get("gates", [])
                if g.get("name") == "MAX_UNHEDGED_EXPOSURE"
            ),
            {},
        )
        self.tick = tick
        self.intent_id = payload.get("intent_id")
        self.correlation_id = payload.get("correlation_id")
        self.requested_notional = payload.get("requested_notional")
        self.approved_notional = payload.get("approved_notional")
        self.unhedged_observed = gate.get("observed")
        self.unhedged_limit = gate.get("limit")
        self.unhedged_detail = gate.get("detail", "")

    def as_dict(self) -> dict:
        return {
            "tick": self.tick,
            "intent_id": self.intent_id,
            "correlation_id": self.correlation_id,
            "requested": _fmt(self.requested_notional),
            "approved": _fmt(self.approved_notional),
            "unhedged_observed": _fmt(self.unhedged_observed),
            "unhedged_limit": _fmt(self.unhedged_limit),
            "unhedged_detail": self.unhedged_detail,
        }


async def _noop() -> None:
    return None


async def run_diagnostic(platform):
    """Drive the shipped platform and capture every tick."""
    entries: list[EntryAuthorisation] = []
    cursor = {"tick": -1}

    def collect(event):
        if event.type is EventType.RISK_PASS:
            # Only entries reach RUNE; exits and hedges build their decisions
            # directly and publish no RISK_PASS.
            entries.append(EntryAuthorisation(cursor["tick"], event.payload))
        return _noop()

    platform.bus.subscribe(collect, types=[EventType.RISK_PASS], name="entry-observer")

    samples: list[TickSample] = []
    clock = platform.clock
    await platform.start(record=False, feeds=False)
    for tick in range(TICKS):
        cursor["tick"] = tick
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
        samples.append(TickSample(tick, platform))
    await platform.bus.drain()
    return samples, entries


def orders_for(sample: TickSample, intent_ids: set[str]) -> list[dict]:
    return [o for o in sample.open_orders if o.get("intent_id") in intent_ids]


def mark_vs_expected(position: dict, expected_price: float | None) -> dict:
    """Where the residual's value came from: the fill, or the mark.

    Three notionals for the same quantity. If they agree, the position is
    simply larger than the budget allowed. If ``mark_notional`` is the outlier,
    the sizing was right and the market moved afterwards. If ``entry_notional``
    is the outlier, the fill landed away from what VESKA sized against.
    """
    quantity = abs(position["quantity"])
    entry = position["average_entry_price"]
    mark = position["mark_price"]
    out = {
        "quantity": _fmt(position["quantity"], 8),
        "expected_price": _fmt(expected_price, 8),
        "average_entry_price": _fmt(entry, 8),
        "mark_price": _fmt(mark, 8),
        "entry_notional": _fmt(quantity * entry if entry else None),
        "mark_notional": _fmt(quantity * mark if mark else None),
    }
    if expected_price:
        out["expected_notional"] = _fmt(quantity * expected_price)
        if mark:
            out["mark_vs_expected_bps"] = _fmt(
                (mark / expected_price - 1.0) * 10_000.0, 4
            )
        if entry:
            out["entry_vs_expected_bps"] = _fmt(
                (entry / expected_price - 1.0) * 10_000.0, 4
            )
    else:
        out["expected_notional"] = "None"
    if entry and mark:
        out["mark_vs_entry_bps"] = _fmt((mark / entry - 1.0) * 10_000.0, 4)
    return out


def build_report(platform, samples, entries, first) -> str:
    """The deliverable. Printed whether or not the assertion fails."""
    limits = platform.settings.risk
    lines = ["", "PHASE5_D_FIRST_BREACH"]

    if first is None:
        breached_ticks = [s.tick for s in samples if s.breaches]
        lines += [
            "first_trigger=None",
            f"ticks_run={len(samples)}",
            f"entry_authorisations={len(entries)}",
            f"ticks_with_a_live_breach={breached_ticks[:10]}",
            "",
            "No RISK_LIMIT_BREACH observed. If this run also shows no breached "
            "ticks, the default strategy now sizes itself inside every hard "
            "limit and this diagnostic has served its purpose.",
        ]
        return "\n".join(lines)

    venue_name, venue_value = first.venue_current
    position_key, position_value = first.position_current
    leverage = first.leverage_current
    before = [e for e in entries if e.tick <= first.tick]
    dominant = first.dominant_position

    lines += [
        f"tick={first.tick}",
        f"time={first.tick_time}",
        f"breaches={first.breaches}",
        f"triggered_by={first.kill_switch_triggers}",
        f"halt_new_trades={first.halt_new_trades}",
        "",
        f"gross={_fmt(first.gross_current)} / {limits.max_gross_exposure}"
        f"   (filled {_fmt(first.gross_exposure)}"
        f" + committed {_fmt(first.committed.gross_exposure)})",
        f"net={_fmt(first.net_current)} / {limits.max_net_exposure}"
        f"   (filled {_fmt(first.net_exposure)}"
        f" + committed {_fmt(first.committed.net_exposure)})",
        f"leverage={_fmt(leverage)} / {limits.max_leverage}"
        f"   (equity {_fmt(first.equity)})",
        f"venue={venue_name} {_fmt(venue_value)} / {limits.max_venue_exposure}",
        f"position={position_key} {_fmt(position_value)}"
        f" / {limits.max_position_notional}",
        f"strategy={_fmt(first.strategy_exposure)} / {limits.max_strategy_exposure}",
        f"actual_unhedged={_fmt(first.actual_unhedged)}"
        f" / {limits.max_unhedged_notional}",
        "",
        "# reported separately: the kill switch does NOT use pending fill risk",
        "# for MAX_UNHEDGED_EXPOSURE -- it measures the residual that exists.",
        f"pending_unhedged_fill_risk={_fmt(first.committed.unhedged_fill_risk)}",
        "",
        "utilization="
        f"{{gross: {_fmt(first.utilization.gross_exposure)},"
        f" net: {_fmt(first.utilization.net_exposure)},"
        f" unhedged: {_fmt(first.utilization.unhedged_notional)},"
        f" pending_unhedged: {_fmt(first.utilization.pending_unhedged_fill_risk)},"
        f" worst_pct: {_fmt(first.utilization.worst_utilization())}}}",
        "",
        f"positions={first.positions}",
        f"open_orders={first.open_orders}",
        "",
        f"entries_before_trigger={[e.as_dict() for e in before]}",
    ]

    if dominant is not None:
        intent_ids = {e.intent_id for e in before if e.intent_id}
        related = [
            o
            for o in orders_for(first, intent_ids)
            if o["venue"] == dominant["venue"] and o["symbol"] == dominant["symbol"]
        ]
        expected_price = related[0]["expected_price"] if related else None
        lines += [
            "",
            f"dominant_position={mark_vs_expected(dominant, expected_price)}",
            f"dominant_position_orders={related}",
        ]
    else:
        lines += ["", "dominant_position=None  (no non-flat position at the trigger)"]

    lines += [
        "",
        "# how to read this:",
        "#   entry/expected apart  -> execution slippage at fill time",
        "#   mark/expected apart   -> mark-to-market drift after sizing",
        "#   all three agree       -> the position is simply larger than the",
        "#                            budget, i.e. a sizing-model gap",
        "#   unhedged breached but no large position -> residual carried in",
        "#                            from an earlier, partly-closed trade",
    ]
    return "\n".join(lines)


class TestWhatTripsTheDefaultRun:
    async def test_the_default_run_never_engages_the_emergency_switch(
        self, platform
    ):
        """Deliberately strict, and expected to fail until the remaining live
        breach is remediated.

        A normal, fully-gated platform trading its own shipped market should
        not reach an emergency state. That it does is the finding; the report
        below is the evidence needed to fix it correctly rather than quickly.
        """
        samples, entries = await run_diagnostic(platform)
        first = next(
            (s for s in samples if "RISK_LIMIT_BREACH" in s.kill_switch_triggers),
            None,
        )
        report = build_report(platform, samples, entries, first)
        print(report)
        await platform.stop()

        assert first is None, report
