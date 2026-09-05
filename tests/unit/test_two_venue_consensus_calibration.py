"""Diagnostic: why does the default two-venue market detect and never fill?

**This test changes nothing.** It drives the real platform — the real
synthetic market, the real TIDAL, NORO and ZEPHR, the real
``ConsensusEngine``, the real RUNE and the real paper executor — captures
every event on the decision path, and prints a calibration report.

The question it exists to answer, with measurements rather than argument:

    After NORO's abstention stopped suppressing the consensus denominator,
    the default topology still produces opportunities and no orders. WHAT
    is now blocking entry?

Nothing here asserts a calibration. The single assertion at the end fires
when no consensus result reaches the entry threshold, and carries the whole
report as its message so the external validator can read the numbers
straight out of CI. If the platform does trade, it passes silently and the
report is simply not needed.

Deliberately absent: any mock, any patched agent, any adjusted weight,
threshold, venue count or market parameter. A probe that changes the system
it measures answers a different question.
"""

from __future__ import annotations

import statistics

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings, simulated_venues
from core.events import EventType
from core.models.common import AgentId
from simulation.market import default_market
from storage import InMemoryEventStore
from tests.conftest import START_MS

#: Long enough for the seeded market to generate a meaningful number of
#: dislocations; short enough to stay a unit-suite run.
TICKS = 500

CAPTURED_TYPES = [
    EventType.OPPORTUNITY_DETECTED,
    EventType.AGENT_OPINION,
    EventType.CONSENSUS_UPDATED,
    EventType.TRADE_INTENT,
    EventType.RISK_PASS,
    EventType.RISK_FAIL,
    EventType.PAPER_ORDER_CREATED,
    EventType.PAPER_FILL,
]


# ======================================================================
# stdlib statistics
# ======================================================================


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile, matching the Phase 3 audit scripts.

    Deterministic and dependency-free: the probe must not need numpy to
    report a number the validator will act on.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def spread(values: list[float]) -> dict[str, float]:
    if not values:
        return dict.fromkeys(
            ("count", "min", "p25", "p50", "p75", "p90", "p95", "p99", "max", "mean"),
            float("nan"),
        ) | {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def line(label: str, stats: dict[str, float], keys: tuple[str, ...]) -> str:
    body = "  ".join(f"{k}={stats[k]:+.4f}" for k in keys)
    return f"    {label:<28} n={stats['count']:<5} {body}"


# ======================================================================
# the run
# ======================================================================


async def _drive() -> dict:
    """One real platform run, with every decision-path event captured."""
    from apps.orchestrator.wiring import build_platform

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

    opportunities: dict[str, dict] = {}
    opinions: dict[str, list[dict]] = {AgentId.TIDAL.value: [], AgentId.NORO.value: [],
                                       AgentId.ZEPHR.value: [], AgentId.LUMEN.value: []}
    consensus: list[dict] = []
    intents: list[dict] = []
    risk_pass: list[dict] = []
    risk_fail: list[dict] = []
    orders: list[dict] = []
    fills: list[dict] = []

    async def capture(event) -> None:
        # Defensive throughout: the bus raises on handler errors, and a probe
        # that crashes the run it is measuring reports nothing at all.
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type is EventType.OPPORTUNITY_DETECTED:
            opportunities.setdefault(
                str(event.correlation_id),
                {
                    "opportunity_id": payload.get("opportunity_id"),
                    "symbol": payload.get("symbol"),
                    "gross_edge_bps": payload.get("gross_edge_bps"),
                },
            )
        elif event.type is EventType.AGENT_OPINION:
            agent = str(payload.get("agent_id"))
            if agent in opinions:
                opinions[agent].append(payload)
        elif event.type is EventType.CONSENSUS_UPDATED:
            consensus.append(payload)
        elif event.type is EventType.TRADE_INTENT:
            intents.append(payload)
        elif event.type is EventType.RISK_PASS:
            risk_pass.append(payload)
        elif event.type is EventType.RISK_FAIL:
            risk_fail.append(payload)
        elif event.type is EventType.PAPER_ORDER_CREATED:
            orders.append(payload)
        elif event.type is EventType.PAPER_FILL:
            fills.append(payload)

    bus.subscribe(capture, types=CAPTURED_TYPES, name="calibration-probe")

    await platform.start(record=False)
    for _ in range(TICKS):
        clock.advance(100)
        await platform.step_market(1)
        await platform.orchestrator.tick()
    await bus.drain()
    await platform.stop()

    return {
        "settings": settings,
        "opportunities": opportunities,
        "opinions": opinions,
        "consensus": consensus,
        "intents": intents,
        "risk_pass": risk_pass,
        "risk_fail": risk_fail,
        "orders": orders,
        "fills": fills,
    }


@pytest.fixture(scope="module")
def run():
    import asyncio

    return asyncio.run(_drive())


# ======================================================================
# analysis
# ======================================================================


def _detail(payload: dict, key: str, default=None):
    detail = payload.get("detail")
    if not isinstance(detail, dict):
        return default
    value = detail.get(key, default)
    return default if value is None else value


def _numbers(payloads: list[dict], key: str) -> list[float]:
    out = []
    for payload in payloads:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def _detail_numbers(payloads: list[dict], key: str) -> list[float]:
    out = []
    for payload in payloads:
        value = _detail(payload, key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def _rate(payloads: list[dict], code: str) -> float:
    if not payloads:
        return float("nan")
    hits = sum(1 for p in payloads if code in (p.get("reason_codes") or []))
    return hits / len(payloads)


def _contributions(result: dict) -> dict[str, dict]:
    rows = result.get("contributions") or []
    return {str(row.get("agent_id")): row for row in rows if isinstance(row, dict)}


def analyse(run: dict) -> dict:
    settings = run["settings"]
    threshold = settings.consensus.entry_threshold

    tidal = run["opinions"][AgentId.TIDAL.value]
    noro = run["opinions"][AgentId.NORO.value]
    zephr = run["opinions"][AgentId.ZEPHR.value]

    scores = _numbers(run["consensus"], "agreement")
    complete = [r for r in run["consensus"] if r.get("complete")]
    entries = [
        r
        for r in complete
        if isinstance(r.get("agreement"), (int, float))
        and r["agreement"] >= threshold
    ]

    # --- the binding-agent analysis ---------------------------------
    #
    # For every consensus result where NORO abstained and both remaining
    # agents voted, recompute the score from the contribution rows and solve
    # for the TIDAL signal that would have reached the threshold.
    recomputed_gap = []
    #: (observed TIDAL signal, TIDAL signal required to reach the threshold),
    #: appended as one pair so the two series can never drift out of step.
    tidal_pairs: list[tuple[float, float]] = []
    strong_zephr_blocked = 0
    for result in run["consensus"]:
        rows = _contributions(result)
        abstained = [str(a) for a in (result.get("abstained_agents") or [])]
        t = rows.get(AgentId.TIDAL.value)
        z = rows.get(AgentId.ZEPHR.value)
        if AgentId.NORO.value not in abstained or t is None or z is None:
            continue
        if set(rows) != {AgentId.TIDAL.value, AgentId.ZEPHR.value}:
            # A third agent contributed mass, so "the active set is TIDAL plus
            # ZEPHR" is not true of this result and the recomputation below
            # would be comparing two different sums. Skip rather than report a
            # spurious mismatch.
            continue
        # ``AgentContribution.weight`` is already the effective weight (after
        # any DEGRADED down-weighting), so weight x confidence is exactly the
        # mass the engine summed.
        wt = float(t.get("weight") or 0.0) * float(t.get("confidence") or 0.0)
        wz = float(z.get("weight") or 0.0) * float(z.get("confidence") or 0.0)
        st = float(t.get("signal") or 0.0)
        sz = float(z.get("signal") or 0.0)
        if wt + wz <= 0:
            continue

        active = (wt * st + wz * sz) / (wt + wz)
        actual = result.get("score")
        if isinstance(actual, (int, float)):
            recomputed_gap.append(abs(active - float(actual)))

        if wt > 0:
            tidal_pairs.append(
                (st, (threshold * (wt + wz) - wz * sz) / wt)
            )
        agreement = result.get("agreement")
        if sz >= threshold and isinstance(agreement, (int, float)):
            if agreement < threshold:
                strong_zephr_blocked += 1

    observed_tidal = [observed for observed, _ in tidal_pairs]
    required_tidal = [required for _, required in tidal_pairs]
    shortfalls = [required - observed for observed, required in tidal_pairs]

    # --- blocker classification -------------------------------------
    if not entries:
        blocker = "CONSENSUS"
    elif not run["intents"]:
        blocker = "ORCHESTRATOR"
    elif not run["risk_pass"]:
        blocker = "RUNE"
    elif not run["orders"]:
        blocker = "EXECUTION"
    else:
        blocker = "NONE"

    return {
        "threshold": threshold,
        "tidal": tidal,
        "noro": noro,
        "zephr": zephr,
        "scores": scores,
        "complete": complete,
        "entries": entries,
        "recomputed_gap": recomputed_gap,
        "observed_tidal": observed_tidal,
        "required_tidal": required_tidal,
        "shortfalls": shortfalls,
        "strong_zephr_blocked": strong_zephr_blocked,
        "blocker": blocker,
    }


def bucket(values: list[float], edges: list[tuple[str, float, float]]) -> list[str]:
    return [
        f"      {label:<18}: {sum(1 for v in values if low <= v < high):>5}"
        for label, low, high in edges
    ]


def report(run: dict, facts: dict) -> str:
    threshold = facts["threshold"]
    tidal, noro, zephr = facts["tidal"], facts["noro"], facts["zephr"]
    scores = facts["scores"]

    tidal_signal = _numbers(tidal, "signal")
    tidal_conf = _numbers(tidal, "confidence")
    tidal_vol = _detail_numbers(tidal, "vol_penalty")
    zephr_signal = _numbers(zephr, "signal")
    zephr_conf = _numbers(zephr, "confidence")
    zephr_edge = _detail_numbers(zephr, "expected_net_edge_bps")
    zephr_cost = _detail_numbers(zephr, "expected_cost_bps")
    zephr_size = _detail_numbers(zephr, "chosen_notional")

    abstained = sum(1 for p in noro if p.get("abstain") is True)
    abstain_rate = abstained / len(noro) if noro else float("nan")

    closest = max(scores, default=float("nan"))
    gap = threshold - closest if scores else float("nan")

    keys = ("min", "p25", "p50", "p75", "p95", "max")
    wide = ("min", "p25", "p50", "p75", "p90", "p95", "p99", "max")

    lines: list[str] = [
        "",
        "=" * 72,
        "TWO-VENUE CONSENSUS CALIBRATION",
        "=" * 72,
        "",
        f"  ticks:               {TICKS}",
        f"  opportunities:       {len(run['opportunities'])}",
        f"  complete_consensus:  {len(facts['complete'])}",
        f"  entries:             {len(facts['entries'])}",
        f"  trade_intents:       {len(run['intents'])}",
        f"  risk_pass:           {len(run['risk_pass'])}",
        f"  risk_fail:           {len(run['risk_fail'])}",
        f"  orders:              {len(run['orders'])}",
        f"  fills:               {len(run['fills'])}",
        "",
        "  NORO",
        f"    opinions:          {len(noro)}",
        f"    abstain_rate:      {abstain_rate:.4f}",
        f"    signals != 0:      {sum(1 for p in noro if p.get('signal'))}",
        "",
        "  TIDAL",
        line("signal", spread(tidal_signal), keys),
        line("confidence", spread(tidal_conf), ("min", "mean", "max")),
        line("vol_penalty", spread(tidal_vol), ("p50", "p95", "max")),
        "    buckets:",
        *bucket(
            tidal_signal,
            [
                ("< -0.25", -1e9, -0.25),
                ("-0.25 .. 0", -0.25, 0.0),
                ("== 0", 0.0, 1e-12),
                ("0 .. 0.10", 1e-12, 0.10),
                ("0.10 .. 0.20", 0.10, 0.20),
                ("0.20 .. 0.40", 0.20, 0.40),
                (">= 0.40", 0.40, 1e9),
            ],
        ),
        "",
        "  ZEPHR",
        line("signal", spread(zephr_signal), keys),
        line("confidence", spread(zephr_conf), ("min", "mean", "max")),
        line("net_edge_bps", spread(zephr_edge), ("p50", "p90", "p95", "max")),
        line("cost_bps", spread(zephr_cost), ("p50", "p90", "max")),
        line("chosen_notional", spread(zephr_size), ("p50", "p90", "max")),
        f"    refusal_rate (NO_ECONOMICAL_SIZE):   {_rate(zephr, 'NO_ECONOMICAL_SIZE'):.4f}",
        f"    survival_rate (EDGE_SURVIVES_EXEC):  "
        f"{_rate(zephr, 'EDGE_SURVIVES_EXECUTION'):.4f}",
        "    buckets:",
        *bucket(
            zephr_signal,
            [
                ("< 0", -1e9, 0.0),
                ("0 .. 0.50", 0.0, 0.50),
                ("0.50 .. 0.75", 0.50, 0.75),
                ("0.75 .. 1.0", 0.75, 1.0),
                ("== 1.0", 1.0, 1e9),
            ],
        ),
        "",
        "  CONSENSUS",
        line("score", spread(scores), wide),
        f"    entry_threshold:   {threshold:.4f}",
        f"    max observed:      {closest:.4f}",
        f"    closest_gap:       {gap:+.4f}",
        f"    count >= 0.45:     {sum(1 for s in scores if s >= 0.45)}",
        f"    count >= 0.50:     {sum(1 for s in scores if s >= 0.50)}",
        f"    count >= 0.55:     {sum(1 for s in scores if s >= 0.55)}",
        f"    count >= {threshold:.2f}:     {sum(1 for s in scores if s >= threshold)}",
        "",
        "  BINDING",
        f"    samples (NORO abstained, TIDAL+ZEPHR voted): "
        f"{len(facts['observed_tidal'])}",
        f"    active-score vs actual, max |delta|:         "
        f"{max(facts['recomputed_gap'], default=float('nan')):.9f}",
        f"    ZEPHR >= {threshold:.2f} but consensus < {threshold:.2f}: "
        f"{facts['strong_zephr_blocked']}",
        f"    median observed TIDAL:   "
        f"{statistics.median(facts['observed_tidal']) if facts['observed_tidal'] else float('nan'):+.4f}",
        f"    median required TIDAL:   "
        f"{statistics.median(facts['required_tidal']) if facts['required_tidal'] else float('nan'):+.4f}",
        f"    minimum required TIDAL:  "
        f"{min(facts['required_tidal'], default=float('nan')):+.4f}",
        f"    median TIDAL shortfall:  "
        f"{statistics.median(facts['shortfalls']) if facts['shortfalls'] else float('nan'):+.4f}",
        "",
        f"  PIPELINE BLOCKER: {facts['blocker']}",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


# ======================================================================
# the probe
# ======================================================================


class TestTwoVenueCalibration:
    def test_the_run_produced_something_to_measure(self, run):
        """Guard on the probe itself: an empty run would make every number
        below meaningless, and a silent NaN report is worse than a loud
        failure."""
        assert run["opportunities"], "the seeded market detected no opportunities"
        assert run["consensus"], "no consensus result was ever computed"

    def test_noro_abstains_on_every_two_venue_opportunity(self, run):
        """The default topology has exactly two venues per symbol, both of
        which are the opportunity's own legs, so NORO has no independent
        benchmark on any of them."""
        noro = run["opinions"][AgentId.NORO.value]
        assert noro, "NORO published no opinion at all"
        non_abstaining = [p for p in noro if p.get("abstain") is not True]
        assert non_abstaining == [], (
            f"{len(non_abstaining)}/{len(noro)} NORO opinions were not "
            "abstentions on a topology that offers no independent evidence"
        )
        assert {p.get("signal") for p in noro} == {0.0}
        for payload in noro:
            assert "INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH" in (
                payload.get("reason_codes") or []
            )

    def test_an_abstention_is_never_counted_as_missing(self, run):
        """If it were, completeness would fail and the blocker would be
        mislabelled: the probe has to distinguish 'suspended' from 'scored
        too low'."""
        for result in run["consensus"]:
            missing = [str(a) for a in (result.get("missing_agents") or [])]
            abstained = [str(a) for a in (result.get("abstained_agents") or [])]
            assert AgentId.NORO.value not in missing or (
                AgentId.NORO.value not in abstained
            ), f"NORO reported both missing and abstaining: {result}"

    def test_the_abstaining_agent_carries_no_scoring_mass(self, run):
        """The recomputation the binding analysis depends on. If NORO were
        still in the denominator, the active-agent score would not match the
        published one and every 'required TIDAL' number would be wrong."""
        facts = analyse(run)
        assert facts["recomputed_gap"], (
            "no consensus result had NORO abstaining alongside TIDAL and "
            "ZEPHR voting, so the binding analysis has no samples"
        )
        assert max(facts["recomputed_gap"]) < 1e-9, (
            "the published consensus score is not the weighted mean of TIDAL "
            "and ZEPHR alone, so something else is still contributing mass"
        )

    def test_the_two_venue_pipeline_reaches_an_entry(self, run):
        """The probe's headline.

        Deliberately allowed to fail: when the default topology cannot reach
        the entry threshold, the report below is the deliverable, and it is
        attached to the assertion so CI prints it without anyone needing to
        re-run anything.
        """
        facts = analyse(run)
        diagnostic = report(run, facts)
        entry_count = len(facts["entries"])
        assert entry_count > 0, diagnostic
