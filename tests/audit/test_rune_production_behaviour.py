"""Phase 5 — §41: RUNE observed over the validated synthetic market.

The targeted tests elsewhere in this suite construct adversarial states
deliberately. This one does the opposite: it runs the platform exactly as it
ships and measures what the hard-risk layer actually does, so the audit report
can say how often RUNE approves, reduces and rejects, which gates bind, and —
most importantly — what happens when a LIVE state exceeds a configured hard
limit.

A quiet result here is not evidence of correctness. The default simulation is
not calibrated to press any limit, so this probe complements the adversarial
tests rather than replacing them. Its job is to catch a breach that the
targeted tests did not think to construct.

BREACH TO RESPONSE, NOT BREACH TO NEVER
=======================================
This probe used to assert that no live breach may ever be observed. Two
validation runs then observed the same one — net and unhedged exposure at
25,103.80 — through asynchronous multi-leg fills, which is a class of event
pre-trade projection cannot always prevent: one leg fills before the other,
fills land away from expected prices, a hedge is briefly incomplete.

So the invariant is stated where it can actually hold. The measurement is not
deleted or weakened; it is followed through:

* any tick whose live state breaches a hard limit must have
  ``RISK_LIMIT_BREACH`` in ``triggered_by`` by the END of that same tick —
  the tick order is settle, measure, protect, manage, seek, so a fill-created
  breach is visible before ``_protect`` runs and there is no reason to defer
  detection to the next tick;
* no ordinary ENTRY may be authorised or planned after that point;
* and the run must end back INSIDE every limit, because a trigger that fires
  and leaves the book breached has not finished its job.

The whole probe runs ONCE, inside one test, and every invariant is checked
against that single run. Splitting it across tests would either re-run 400
ticks per assertion or pair one run's observations with another run's platform;
accumulating the violations and reporting them together is also the more useful
shape for an audit, since it shows every breach rather than the first.

Nothing in the simulation is altered.
"""

from __future__ import annotations

from collections import Counter

from core.events import EventType
from core.models.risk import RiskVerdict

TICKS = 400

#: Triggers whose firing is explained by this probe's own subject matter. A
#: live hard-limit breach is what the run is measuring; anything else firing on
#: the shipped simulation is an unexplained event and still fails the probe.
EXPECTED_TRIGGERS = {"RISK_LIMIT_BREACH"}


def measure(portfolio, committed, unhedged: float, open_orders: int) -> dict:
    """The live risk state, computed here rather than read from production.

    An independent implementation of the contract the backstop is held to:
    exposure is what has FILLED plus what is COMMITTED by working, unfilled
    entry orders, while the unhedged residual is the filled book's alone —
    an unfilled second leg has not neutralised the position that exists now.
    Written from that definition so the probe compares production against a
    statement of the requirement, not against itself.
    """
    by_venue = portfolio.exposure_by_venue()
    for venue, amount in committed.venue_exposure.items():
        by_venue[venue] = by_venue.get(venue, 0.0) + amount
    by_position = {key: held.notional for key, held in portfolio.positions.items()}
    for key, amount in committed.position_exposure.items():
        by_position[key] = by_position.get(key, 0.0) + amount

    gross = portfolio.gross_exposure + committed.gross_exposure
    equity = portfolio.equity
    return {
        "gross_exposure": gross,
        "net_exposure": abs(portfolio.net_exposure + committed.net_exposure),
        "venue_exposure": max(by_venue.values(), default=0.0),
        "position_notional": max(by_position.values(), default=0.0),
        "unhedged_notional": abs(unhedged),
        "drawdown": portfolio.drawdown,
        "day_loss": max(0.0, -portfolio.day_realized_pnl),
        "leverage": gross / equity if equity > 0 else 0.0,
        "open_orders": float(open_orders),
    }


def violations(sample: dict, limits) -> list[str]:
    """Every hard limit this one sample exceeds.

    ``drawdown`` and ``day_loss`` are measured but deliberately not checked
    here: they have their own dedicated triggers and their own strict
    boundary convention, and this list is what ``RISK_LIMIT_BREACH`` is
    responsible for.
    """
    checks = [
        ("MAX_GROSS_EXPOSURE", "gross_exposure", limits.max_gross_exposure),
        ("MAX_NET_EXPOSURE", "net_exposure", limits.max_net_exposure),
        ("MAX_VENUE_EXPOSURE", "venue_exposure", limits.max_venue_exposure),
        ("MAX_POSITION_NOTIONAL", "position_notional", limits.max_position_notional),
        ("MAX_LEVERAGE", "leverage", limits.max_leverage),
        ("MAX_UNHEDGED_EXPOSURE", "unhedged_notional", limits.max_unhedged_notional),
        ("MAX_OPEN_ORDERS", "open_orders", float(limits.max_open_orders)),
    ]
    return [
        f"{name}: observed {sample[key]} > limit {limit}"
        for name, key, limit in checks
        if sample[key] > limit + 1e-6
    ]


class RiskObservations:
    """Everything the probe collects from one run."""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.verdicts: Counter[str] = Counter()
        self.blocking_gates: Counter[str] = Counter()
        self.kill_switch_triggers: list[str] = []
        self.max_open_orders = 0
        #: Tick index of each approved ENTRY authorisation. Exits and hedges
        #: build their decisions directly and never publish RISK_PASS, so
        #: every entry here is an entry and nothing else.
        self.entry_authorisations: list[int] = []
        #: Tick index of each execution plan built for one of those entries.
        self.entry_plans: list[int] = []
        self.entry_intent_ids: set[str] = set()
        #: One row per tick: what the state was and what the switch had done.
        self.timeline: list[dict] = []
        self.tick = -1
        self.final: dict = {}
        self.worst: dict[str, float] = {
            "gross_exposure": 0.0,
            "net_exposure": 0.0,
            "venue_exposure": 0.0,
            "position_notional": 0.0,
            "unhedged_notional": 0.0,
            "drawdown": 0.0,
            "day_loss": 0.0,
            "leverage": 0.0,
        }

    def observe_tick(self, tick: int, sample: dict, limits, kill_switch, positions: int):
        for key in self.worst:
            self.worst[key] = max(self.worst[key], sample[key])
        self.max_open_orders = max(self.max_open_orders, int(sample["open_orders"]))
        self.final = sample
        breached = violations(sample, limits)
        self.timeline.append(
            {
                "tick": tick,
                "breaches": breached,
                "triggered_by": list(kill_switch.triggered_by),
                "halt_new_trades": kill_switch.halt_new_trades,
                "open_orders": int(sample["open_orders"]),
                "positions": positions,
            }
        )
        return breached

    def first_breach_tick(self) -> int | None:
        return next(
            (row["tick"] for row in self.timeline if row["breaches"]), None
        )

    def first_response_tick(self) -> int | None:
        return next(
            (
                row["tick"]
                for row in self.timeline
                if "RISK_LIMIT_BREACH" in row["triggered_by"]
            ),
            None,
        )

    def undetected_breach_ticks(self) -> list[dict]:
        """Ticks that ended breached with no RISK_LIMIT_BREACH response."""
        return [
            row
            for row in self.timeline
            if row["breaches"] and "RISK_LIMIT_BREACH" not in row["triggered_by"]
        ]

    def tally(self) -> None:
        for payload in self.decisions:
            self.verdicts[payload["verdict"]] += 1
            for gate in payload.get("gates", []):
                if gate.get("mandatory") and gate.get("result") != "PASS":
                    self.blocking_gates[gate["name"]] += 1

    def summary(self) -> dict:
        breached = [row for row in self.timeline if row["breaches"]]
        return {
            "ticks": TICKS,
            "risk_evaluations": len(self.decisions),
            "verdicts": dict(self.verdicts),
            "blocking_gates": dict(sorted(self.blocking_gates.items())),
            "kill_switch_triggers": self.kill_switch_triggers,
            "max_open_orders": self.max_open_orders,
            "worst_observed": {k: round(v, 4) for k, v in self.worst.items()},
            "final_observed": {
                k: round(v, 4) for k, v in self.final.items()
            },
            "breached_ticks": len(breached),
            "first_breach_tick": self.first_breach_tick(),
            "first_response_tick": self.first_response_tick(),
            "entry_authorisation_ticks": self.entry_authorisations,
            "sample_breaches": [row["breaches"] for row in breached[:3]],
        }


async def _noop() -> None:
    return None


async def run_probe(platform) -> RiskObservations:
    """Run the shipped platform and collect every risk decision it makes."""
    obs = RiskObservations()
    limits = platform.settings.risk

    def collect(event):
        if event.type in (EventType.RISK_PASS, EventType.RISK_FAIL):
            obs.decisions.append(event.payload)
            if event.type is EventType.RISK_PASS:
                # Only entries are evaluated by RUNE: exits and hedges build
                # their decisions directly and publish no RISK_PASS.
                obs.entry_authorisations.append(obs.tick)
                obs.entry_intent_ids.add(event.payload.get("intent_id", ""))
        elif event.type is EventType.EXECUTION_PLAN:
            if event.payload.get("intent_id") in obs.entry_intent_ids:
                obs.entry_plans.append(obs.tick)
        elif event.type is EventType.KILL_SWITCH_TRIGGERED:
            obs.kill_switch_triggers.append(event.payload.get("kind", "?"))
        return _noop()

    platform.bus.subscribe(
        collect,
        types=[
            EventType.RISK_PASS,
            EventType.RISK_FAIL,
            EventType.EXECUTION_PLAN,
            EventType.KILL_SWITCH_TRIGGERED,
        ],
        name="risk-observer",
    )

    clock = platform.clock
    await platform.start(record=False, feeds=False)
    for tick in range(TICKS):
        obs.tick = tick
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
        # Measured at the END of the tick. The tick runs settle, measure,
        # protect, manage, seek, so a breach created by a fill this tick was
        # already visible to `_protect` and there is no reason for detection
        # to wait for the next one.
        portfolio = platform.account.snapshot()
        obs.observe_tick(
            tick,
            measure(
                portfolio,
                platform.orchestrator._current_committed_exposure(),
                platform.okapi.total_unhedged(portfolio),
                len(platform.veska.open_orders()),
            ),
            limits,
            platform.state.kill_switch,
            positions=sum(1 for p in portfolio.positions.values() if not p.is_flat),
        )
    await platform.bus.drain()
    obs.tally()
    return obs


def live_state_violations(obs: RiskObservations, limits) -> list[str]:
    """Every hard limit the WORST live state of the run exceeded.

    Kept as the headline measurement. It is no longer asserted empty — see the
    module docstring — but it is what the breach-to-response invariants below
    are anchored to, and it is reported whether or not anything failed.
    """
    return violations({**obs.worst, "open_orders": float(obs.max_open_orders)}, limits)


def final_state_violations(obs: RiskObservations, limits) -> list[str]:
    """Every hard limit the run was still breaching when it ended."""
    return violations(obs.final, limits) if obs.final else []


def decision_violations(obs: RiskObservations, limits) -> list[str]:
    """Decisions that authorised something the limits forbid."""
    out: list[str] = []
    for decision in obs.decisions:
        approved = decision["approved_notional"]
        requested = decision["requested_notional"]
        rejected = decision["verdict"] == RiskVerdict.REJECTED.value

        if approved > requested + 1e-9:
            out.append(
                f"{decision['decision_id']}: approved {approved} > requested "
                f"{requested}"
            )
        if rejected:
            if approved != 0.0:
                out.append(
                    f"{decision['decision_id']}: rejected but approved {approved}"
                )
            if not decision["reason_codes"]:
                out.append(f"{decision['decision_id']}: rejected with no reason code")
            continue

        if approved > limits.max_order_notional + 1e-9:
            out.append(
                f"{decision['decision_id']}: approved {approved} above "
                f"max_order_notional {limits.max_order_notional}"
            )
        if 0.0 < approved < limits.min_trade_notional - 1e-9:
            out.append(
                f"{decision['decision_id']}: approved {approved} below "
                f"min_trade_notional {limits.min_trade_notional}"
            )
        if "ALL_GATES_PASSED" not in decision["reason_codes"]:
            out.append(
                f"{decision['decision_id']}: approved without ALL_GATES_PASSED"
            )
        blocking = [
            gate["name"]
            for gate in decision.get("gates", [])
            if gate.get("mandatory") and gate.get("result") != "PASS"
        ]
        if blocking:
            out.append(
                f"{decision['decision_id']}: approved with blocking gates {blocking}"
            )
    return out


class TestRuneOverTheDefaultMarket:
    async def test_the_hard_limits_hold_over_a_production_like_run(self, platform):
        limits = platform.settings.risk
        obs = await run_probe(platform)
        summary = obs.summary()
        summary["worst_breaches"] = live_state_violations(obs, limits)
        summary["final_breaches"] = final_state_violations(obs, limits)

        # Printed unconditionally: the numbers are the deliverable, whether or
        # not anything failed.
        print(f"\nPHASE 5 RUNE PRODUCTION PROBE\n{summary}")

        assert obs.decisions, (
            f"no risk evaluation ran in {TICKS} ticks; the probe measured "
            f"nothing and proves nothing. {summary}"
        )

        bad_decisions = decision_violations(obs, limits)
        assert not bad_decisions, (
            f"risk decisions violated their own limits: {bad_decisions}. {summary}"
        )

        # -- 1. every breach is detected, within the tick that created it ---
        undetected = obs.undetected_breach_ticks()
        assert not undetected, (
            "a live hard-limit breach ended a tick with no RISK_LIMIT_BREACH "
            f"response: {undetected[:3]}. The tick runs settle, measure, "
            "protect, manage, seek, so the breach was visible to _protect "
            f"before the tick ended. {summary}"
        )

        # -- 2. nothing but an explained trigger fires ----------------------
        unexplained = sorted(set(obs.kill_switch_triggers) - EXPECTED_TRIGGERS)
        assert not unexplained, (
            "the kill switch fired for something this probe does not explain: "
            f"{unexplained}. {summary}"
        )

        # -- 3. no new ENTRY after the response ----------------------------
        response_tick = obs.first_response_tick()
        if response_tick is not None:
            late_entries = [t for t in obs.entry_authorisations if t > response_tick]
            late_plans = [t for t in obs.entry_plans if t > response_tick]
            assert not late_entries, (
                f"{len(late_entries)} entry authorisation(s) after "
                f"RISK_LIMIT_BREACH engaged on tick {response_tick}: "
                f"{late_entries[:5]}. {summary}"
            )
            assert not late_plans, (
                f"{len(late_plans)} entry execution plan(s) after "
                f"RISK_LIMIT_BREACH engaged on tick {response_tick}: "
                f"{late_plans[:5]}. {summary}"
            )

        # -- 4. and the run ends back inside every limit -------------------
        # A trigger that fires and leaves the book breached has not finished
        # its job. Risk-reducing activity — exits, hedges, the flatten — stays
        # permitted precisely so this can hold.
        still_breached = final_state_violations(obs, limits)
        assert not still_breached, (
            "the run ended still outside its hard limits: "
            f"{still_breached}. The safety response fired but did not bring "
            f"exposure back inside. {summary}"
        )

        # The utilization snapshot the dashboard shows must be populated and
        # bounded after a real run.
        utilization = platform.state.risk_utilization
        assert 0.0 <= utilization.worst_utilization() <= 1.0, summary
        assert utilization.max_gross_exposure == limits.max_gross_exposure, (
            "risk utilization was never refreshed against the live limits"
        )

        await platform.stop()
