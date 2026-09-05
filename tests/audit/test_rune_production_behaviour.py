"""Phase 5 — §41: RUNE observed over the validated synthetic market.

The targeted tests elsewhere in this suite construct adversarial states
deliberately. This one does the opposite: it runs the platform exactly as it
ships and measures what the hard-risk layer actually does, so the audit report
can say how often RUNE approves, reduces and rejects, which gates bind, and —
most importantly — whether any LIVE state ever exceeds a configured hard limit.

A quiet result here is not evidence of correctness. The default simulation is
not calibrated to press any limit, so this probe complements the adversarial
tests rather than replacing them. Its job is to catch a breach that the
targeted tests did not think to construct.

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


class RiskObservations:
    """Everything the probe collects from one run."""

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.verdicts: Counter[str] = Counter()
        self.blocking_gates: Counter[str] = Counter()
        self.kill_switch_triggers: list[str] = []
        self.max_open_orders = 0
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

    def observe_portfolio(self, portfolio, unhedged: float) -> None:
        worst = self.worst
        worst["gross_exposure"] = max(worst["gross_exposure"], portfolio.gross_exposure)
        worst["net_exposure"] = max(worst["net_exposure"], abs(portfolio.net_exposure))
        by_venue = portfolio.exposure_by_venue()
        if by_venue:
            worst["venue_exposure"] = max(
                worst["venue_exposure"], max(by_venue.values())
            )
        if portfolio.positions:
            worst["position_notional"] = max(
                worst["position_notional"],
                max(p.notional for p in portfolio.positions.values()),
            )
        worst["unhedged_notional"] = max(worst["unhedged_notional"], abs(unhedged))
        worst["drawdown"] = max(worst["drawdown"], portfolio.drawdown)
        worst["day_loss"] = max(
            worst["day_loss"], max(0.0, -portfolio.day_realized_pnl)
        )
        if portfolio.equity > 0:
            worst["leverage"] = max(
                worst["leverage"], portfolio.gross_exposure / portfolio.equity
            )

    def tally(self) -> None:
        for payload in self.decisions:
            self.verdicts[payload["verdict"]] += 1
            for gate in payload.get("gates", []):
                if gate.get("mandatory") and gate.get("result") != "PASS":
                    self.blocking_gates[gate["name"]] += 1

    def summary(self) -> dict:
        return {
            "ticks": TICKS,
            "risk_evaluations": len(self.decisions),
            "verdicts": dict(self.verdicts),
            "blocking_gates": dict(sorted(self.blocking_gates.items())),
            "kill_switch_triggers": self.kill_switch_triggers,
            "max_open_orders": self.max_open_orders,
            "worst_observed": {k: round(v, 4) for k, v in self.worst.items()},
        }


async def _noop() -> None:
    return None


async def run_probe(platform) -> RiskObservations:
    """Run the shipped platform and collect every risk decision it makes."""
    obs = RiskObservations()

    def collect(event):
        if event.type in (EventType.RISK_PASS, EventType.RISK_FAIL):
            obs.decisions.append(event.payload)
        elif event.type is EventType.KILL_SWITCH_TRIGGERED:
            obs.kill_switch_triggers.append(event.payload.get("kind", "?"))
        return _noop()

    platform.bus.subscribe(
        collect,
        types=[
            EventType.RISK_PASS,
            EventType.RISK_FAIL,
            EventType.KILL_SWITCH_TRIGGERED,
        ],
        name="risk-observer",
    )

    clock = platform.clock
    await platform.start(record=False, feeds=False)
    for _ in range(TICKS):
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
        obs.max_open_orders = max(
            obs.max_open_orders, len(platform.veska.open_orders())
        )
        portfolio = platform.account.snapshot()
        obs.observe_portfolio(portfolio, platform.okapi.total_unhedged(portfolio))
    await platform.bus.drain()
    obs.tally()
    return obs


def live_state_violations(obs: RiskObservations, limits) -> list[str]:
    """Every hard limit a LIVE state exceeded during the run.

    Pre-trade gates exist to make this list empty. Anything in it is a state
    the platform reached despite a limit that was supposed to prevent it.
    """
    checks = [
        ("MAX_GROSS_EXPOSURE", obs.worst["gross_exposure"], limits.max_gross_exposure),
        ("MAX_NET_EXPOSURE", obs.worst["net_exposure"], limits.max_net_exposure),
        ("MAX_VENUE_EXPOSURE", obs.worst["venue_exposure"], limits.max_venue_exposure),
        (
            "MAX_POSITION_NOTIONAL",
            obs.worst["position_notional"],
            limits.max_position_notional,
        ),
        ("MAX_LEVERAGE", obs.worst["leverage"], limits.max_leverage),
        (
            "MAX_UNHEDGED_EXPOSURE",
            obs.worst["unhedged_notional"],
            limits.max_unhedged_notional,
        ),
        ("MAX_OPEN_ORDERS", float(obs.max_open_orders), float(limits.max_open_orders)),
    ]
    return [
        f"{name}: observed {observed} > limit {limit}"
        for name, observed, limit in checks
        if observed > limit + 1e-6
    ]


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

        # Printed unconditionally: the numbers are the deliverable, whether or
        # not anything failed.
        print(f"\nPHASE 5 RUNE PRODUCTION PROBE\n{summary}")

        assert obs.decisions, (
            f"no risk evaluation ran in {TICKS} ticks; the probe measured "
            f"nothing and proves nothing. {summary}"
        )

        breaches = live_state_violations(obs, limits)
        bad_decisions = decision_violations(obs, limits)

        assert not breaches, (
            f"live state exceeded configured hard limits: {breaches}. {summary}"
        )
        assert not bad_decisions, (
            f"risk decisions violated their own limits: {bad_decisions}. {summary}"
        )
        assert obs.kill_switch_triggers == [], (
            "the kill switch fired on the shipped simulation: "
            f"{obs.kill_switch_triggers}. {summary}"
        )

        # The utilization snapshot the dashboard shows must be populated and
        # bounded after a real run.
        utilization = platform.state.risk_utilization
        assert 0.0 <= utilization.worst_utilization() <= 1.0, summary
        assert utilization.max_gross_exposure == limits.max_gross_exposure, (
            "risk utilization was never refreshed against the live limits"
        )

        await platform.stop()
