"""Phase 2 Batch 1.4: one orchestrator tick decides at ONE logical time.

Batch 1.3 established the rule for paper execution. This pins it for the
whole economic fast loop: with a live feed advancing the clock *repeatedly*
while a single tick is still running, every economically meaningful
timestamp that tick produces must still equal the tick's canonical time --
which is the instant TIDAL assembled the market snapshot, and the instant
the ``ORCHESTRATOR_TICK`` marker records, and therefore the one instant
replay can restore.

Anything that reads the live clock mid-tick instead breaks that: the
original run stamps a decision at a time replay cannot reach, and the two
runs then disagree about data freshness, opinion quality, opportunity
validity, risk gating or health -- see ``P2-15`` in the Batch 1.4 report.

LUMEN is deliberately exempt (its opinions genuinely arrive asynchronously
and carry their own arrival time); every other agent is not.

WHERE THE DRIFT IS INJECTED (Phase 3+4 harness cleanup)
=======================================================
"Mid-tick" is load-bearing. The drift these tests inject models a concurrent
feed moving the clock *while one orchestrator tick executes*; it does NOT
model the market itself arriving at different times, and the two are not
interchangeable. ``SimulatedMarketDriver.step()`` stamps every synthetic
message from the platform clock, so drift injected during market-input
delivery changes the input stream rather than the scheduling of the tick that
consumes it -- and an outcome difference then says nothing about the platform.

See :class:`MidTickClockAdvancer` for the boundary and why it is a scoped
context rather than an event-type allowlist.
"""

from __future__ import annotations

import contextlib

import pytest

from core.clock import ManualClock
from core.events import MARKET_INPUT_TYPES, Event, EventType

#: (event type -> payload field carrying the decision's logical time).
#: Every one of these is economic: it decides validity, freshness, quality,
#: a gate outcome or an execution deadline at some later tick.
ECONOMIC_STAMPS: dict[EventType, str] = {
    EventType.OPPORTUNITY_DETECTED: "created_at",
    EventType.AGENT_OPINION: "created_at",
    EventType.CONSENSUS_UPDATED: "created_at",
    EventType.TRADE_INTENT: "created_at",
    EventType.RISK_PASS: "created_at",
    EventType.RISK_FAIL: "created_at",
    EventType.EXECUTION_PLAN: "created_at",
    EventType.PAPER_ORDER_CREATED: "submitted_at",
}


class MidTickClockAdvancer:
    """Stands in for a live feed: advances the clock during a tick only.

    A tick that only ever sees a stationary clock cannot demonstrate the
    invariant; one running while the clock jumps forward at every internal
    publish can. But *where* the drift is injected is not a detail.

    WHY THIS IS GATED (Phase 3+4 harness cleanup)
    =============================================
    An earlier version of this helper was attached as unconditional bus
    middleware, so it advanced the clock on every publication — including the
    ``BOOK_SNAPSHOT`` / ``BOOK_DELTA`` / ``TRADE_PRINT`` publications that ARE
    the market input stream. ``SimulatedMarketDriver.step()`` reads
    ``self.clock.now_ms()`` once and stamps every message of that step with
    it, so the clock position when the *next* step begins depended on how many
    events the previous iteration published — which differs with ``step_ms``.

    Two runs at different ``step_ms`` therefore no longer shared a market. Their
    ``exchange_ts``, ``received_ts``, rolling trade-flow windows,
    short-volatility windows and freshness ages all diverged. An outcome
    difference then proved nothing: it was not "same market, different mid-tick
    scheduling", it was "different timestamped market input stream". That is a
    corrupted premise, not a finding, and the simulator's stamping is a
    deliberate production contract that must not be bent to accommodate the
    harness.

    Drift is therefore injected only while :meth:`active` is held — around
    ``orchestrator.tick()`` and nothing else. This is an explicit boundary
    rather than an event-name allowlist, so it cannot drift out of date as new
    internal event types are added.

    BUDGET
    ======
    ``budget_ms`` caps how far one tick may be pushed. It exists because
    :class:`ManualClock` refuses to move backwards (by design — a clock that
    rewinds is not a clock), so drift injected inside a tick cannot be undone
    afterwards. The equivalence test pins each market step to an ABSOLUTE
    instant so every run feeds on identical inputs, and that only works while
    one tick's drift stays strictly inside the tick period. Unbounded drift
    would push the schedule past the next boundary and the clock would refuse
    the jump.

    The cap does not make the drift uniform: with a budget of 80ms, a 1ms step
    lands up to eighty small movements spread through the tick and a 40ms step
    lands two large ones. The invariant under test — that *when* the clock
    moves inside a tick does not change what the tick decides — is exercised by
    exactly that difference. ``None`` leaves it unbounded, for tests that do
    not compare two runs against each other.
    """

    def __init__(
        self, clock: ManualClock, step_ms: int = 37, budget_ms: int | None = None
    ) -> None:
        self.clock = clock
        self.step_ms = step_ms
        self.budget_ms = budget_ms
        self.advances = 0
        self.total_advanced_ms = 0
        self.enabled = False
        self._spent_this_tick = 0

    @contextlib.contextmanager
    def active(self):
        """Enable drift for the duration of one orchestrator tick."""
        self.enabled = True
        self._spent_this_tick = 0
        try:
            yield self
        finally:
            self.enabled = False

    async def __call__(self, event: Event) -> None:
        if not self.enabled or self.step_ms <= 0:
            return
        if (
            self.budget_ms is not None
            and self._spent_this_tick + self.step_ms > self.budget_ms
        ):
            return
        self.advances += 1
        self._spent_this_tick += self.step_ms
        self.total_advanced_ms += self.step_ms
        self.clock.advance(self.step_ms)


def market_input_fingerprint(events: list[Event]) -> list[tuple]:
    """The market input stream, reduced to what is economically meaningful.

    Deliberately excludes minted identifiers (``event_id``, correlation ids),
    which are nondeterministic by design and say nothing about what the market
    did. Everything retained is something a downstream metric window, freshness
    gate or book applier actually reads.

    This exists to pin the PREMISE of the outcome-invariance test. Comparing
    economic outcomes between two runs is only meaningful if the two runs were
    fed the same market, and the harness corrupting its own inputs is exactly
    the failure this guards against — it is not hypothetical, it is the defect
    this pass fixed.
    """
    fingerprint = []
    for event in events:
        if event.type not in MARKET_INPUT_TYPES:
            continue
        payload = event.payload
        fingerprint.append(
            (
                event.type.value,
                event.ts_ms,
                event.source,
                event.schema_name,
                payload.get("venue"),
                payload.get("symbol"),
                payload.get("exchange_ts"),
                payload.get("received_ts"),
                payload.get("sequence"),
            )
        )
    return fingerprint


def _build(settings):
    """The two-venue synthetic scenario that actually trades (the same one
    the Batch 1.3 equivalence tests use), so the invariant is checked over a
    real pipeline rather than a run of empty ticks."""
    from tests.replay.test_replay import fresh_platform

    return fresh_platform(settings)


class TestOneTickOneTime:
    async def test_every_economic_stamp_in_a_tick_equals_the_tick_time(
        self, settings
    ):
        platform = _build(settings)
        clock = platform.clock
        seen: list[Event] = []

        platform.bus.subscribe(
            lambda e: seen.append(e) or _noop(), types=None, name="collector"
        )
        await platform.start(record=False, feeds=False)

        # Registered AFTER the collector so the clock jumps between, not
        # during, the collection of a single event. Unbounded: this test
        # compares stamps within one run rather than two runs against each
        # other, so it needs no fixed schedule and no drift budget.
        feed = MidTickClockAdvancer(clock)
        platform.bus.add_middleware(feed)

        for _ in range(50):
            clock.advance(100)
            # Market-input delivery, with no drift injected: the input stream
            # is not the thing under test and must not be perturbed by it.
            await platform.step_market(1)
            with feed.active():
                await platform.orchestrator.tick()
        await platform.bus.drain()

        assert feed.advances > 100, (
            "the clock must actually have moved many times mid-tick for this "
            "test to prove anything"
        )

        # Walk the stream: a MARKET_STATE opens a tick, the ORCHESTRATOR_TICK
        # that follows names its canonical time, and everything up to the
        # next MARKET_STATE belongs to that tick.
        pending_market: Event | None = None
        tick_time: int | None = None
        checked = 0
        ticks_checked = 0
        opportunity_born: dict[str, int] = {}

        for event in seen:
            if event.type is EventType.MARKET_STATE:
                pending_market = event
                tick_time = None
                continue
            if event.type is EventType.ORCHESTRATOR_TICK:
                tick_time = event.ts_ms
                ticks_checked += 1
                if pending_market is not None:
                    assert pending_market.payload["created_at"] == tick_time, (
                        "the market snapshot a tick reasons about must BE the "
                        "tick's canonical time, not an earlier instant the "
                        "marker then overrides"
                    )
                    checked += 1
                continue
            if tick_time is None:
                continue
            field = ECONOMIC_STAMPS.get(event.type)
            if field is None:
                continue
            if (
                event.type is EventType.AGENT_OPINION
                and event.payload.get("agent_id") == "LUMEN"
            ):
                # Exempt by design: LUMEN's intelligence genuinely arrives
                # asynchronously and carries its own arrival time.
                continue

            # Every one of these is published by the tick, so the envelope's
            # own timestamp is the tick's decision time without exception.
            assert event.ts_ms == tick_time, (
                f"{event.type.value} was published at ts_ms={event.ts_ms} "
                f"but its tick decided at {tick_time}"
            )
            checked += 1

            if event.type is EventType.OPPORTUNITY_DETECTED:
                # An opportunity is re-published on every later tick that
                # re-evaluates it (_monitor), and its created_at then
                # correctly stays the tick it was BORN in -- so the invariant
                # here is "born at its birth tick's time, and never restamped
                # afterwards", not "equal to the current tick".
                oid = event.payload["opportunity_id"]
                born = event.payload["created_at"]
                if oid not in opportunity_born:
                    assert born == tick_time, (
                        f"opportunity {oid} was born at {born} but the tick "
                        f"that detected it decided at {tick_time}"
                    )
                    opportunity_born[oid] = born
                else:
                    assert born == opportunity_born[oid], (
                        f"opportunity {oid} was restamped from "
                        f"{opportunity_born[oid]} to {born}"
                    )
                continue

            assert event.payload[field] == tick_time, (
                f"{event.type.value}.{field} was stamped "
                f"{event.payload[field]} but its tick decided at {tick_time}"
            )

        assert ticks_checked >= 50
        assert checked > 40, f"only {checked} economic stamps were verified"

        # The scenario must be rich enough to have exercised the real
        # pipeline, not just empty ticks.
        kinds = {e.type for e in seen}
        assert EventType.OPPORTUNITY_DETECTED in kinds
        assert EventType.AGENT_OPINION in kinds

        await platform.stop()

    async def test_market_snapshot_uses_one_instant_for_every_venue(
        self, settings
    ):
        """A snapshot aged against two instants is not reproducible either."""
        platform = _build(settings)
        clock = platform.clock
        await platform.start(record=False, feeds=False)
        drift = MidTickClockAdvancer(clock, step_ms=13)
        platform.bus.add_middleware(drift)

        for _ in range(5):
            clock.advance(100)
            await platform.step_market(1)
            with drift.active():
                await platform.orchestrator.tick()

        assert drift.advances > 0, "the clock must have moved inside a tick"
        market = platform.state.market
        assert market.venues, "the scenario must produce venue state"
        for key, venue in market.venues.items():
            assert venue.as_of == market.created_at, (
                f"{key} was aged at {venue.as_of} but the snapshot was taken "
                f"at {market.created_at}"
            )
        for key, view in market.consolidated.items():
            assert view.as_of == market.created_at, (
                f"consolidated {key} was aged at {view.as_of}, snapshot "
                f"{market.created_at}"
            )
        await platform.stop()


    async def test_build_state_ages_every_venue_at_the_supplied_instant(
        self, settings
    ):
        """``build_state(now_ms)`` must age the WHOLE snapshot at that
        instant, not re-read the clock per venue.

        Today ``build_state`` is synchronous, so a per-venue clock read
        happens to return the same value -- but only by accident of there
        being no await inside it. Threading the instant makes the property
        structural instead of incidental, and this test is what holds it
        there.
        """
        platform = _build(settings)
        clock = platform.clock
        await platform.start(record=False, feeds=False)
        for _ in range(3):
            clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()

        # The clock is parked far away; the supplied instant must win.
        supplied = clock.now_ms() - 250
        clock.advance(9_000_000)

        market = platform.tidal.build_state(supplied)
        assert market.created_at == supplied
        assert market.venues, "the scenario must produce venue state"
        for key, venue in market.venues.items():
            assert venue.as_of == supplied, (
                f"{key} was aged at {venue.as_of}, not the supplied {supplied}"
            )
        for key, view in market.consolidated.items():
            assert view.as_of == supplied, (
                f"consolidated {key} was aged at {view.as_of}, not {supplied}"
            )
        await platform.stop()


#: One iteration of the drift-equivalence loop, in logical milliseconds. Each
#: market step is pinned to an absolute multiple of this from the run's origin,
#: so every run feeds on an identical input stream no matter how much drift was
#: injected inside the preceding tick. Unchanged from the relative ``advance(100)``
#: the loop used before: with no drift the two are the same schedule, so the
#: baseline run is exactly the run this test has always taken as its reference.
TICK_PERIOD_MS = 100

#: How far one tick may be pushed by injected drift. Must be strictly less than
#: ``TICK_PERIOD_MS``: :class:`ManualClock` refuses to move backwards, so drift
#: injected inside a tick cannot be unwound afterwards, and the next absolute
#: boundary has to still be in the future. See :class:`MidTickClockAdvancer`.
TICK_DRIFT_BUDGET_MS = 80


class TestOutcomeIsIndependentOfMidTickClockDrift:
    """The invariant stated as an outcome, not a timestamp.

    How fast a concurrent feed advances the clock *during* a tick is a
    scheduling accident. If the economic result changes with it, the run is
    not reproducible -- replay never reproduces that accident, it re-executes
    the tick at one recorded instant.

    THE PREMISE IS ASSERTED, NOT ASSUMED (Phase 3+4 harness cleanup)
    ===============================================================
    "Same inputs + different mid-tick clock movement = same outcome" has two
    halves, and the harness used to get the first half wrong: drift injected on
    every bus publication moved the clock during market-input delivery too, and
    ``SimulatedMarketDriver.step()`` stamps its messages from that clock. The
    runs being compared were therefore fed different markets, and the outcome
    difference the test reported was the harness's own doing.

    Two changes close that. Drift is now scoped to ``orchestrator.tick()``, and
    each market step is pinned to an absolute instant so its stamps cannot move.
    Then the premise itself is asserted: the market input stream of every drift
    run must be byte-identical to the baseline's before any economic comparison
    is allowed to mean anything.
    """

    @staticmethod
    async def _run(settings, step_ms: int) -> tuple[dict, list[tuple], int]:
        from tests.replay.test_replay import replay_equivalence_summary

        platform = _build(settings)
        clock = platform.clock
        inputs: list[Event] = []
        platform.bus.subscribe(
            lambda e: inputs.append(e) or _noop(),
            types=sorted(MARKET_INPUT_TYPES, key=lambda t: t.value),
            name="input-collector",
        )
        await platform.start(record=False, feeds=False)

        drift = MidTickClockAdvancer(
            clock, step_ms=step_ms, budget_ms=TICK_DRIFT_BUDGET_MS
        )
        platform.bus.add_middleware(drift)

        origin = clock.now_ms()
        for i in range(50):
            # ABSOLUTE, not relative: whatever drift the previous tick
            # injected is absorbed here, so this step's stamps are the same
            # in every run. `set()` would raise if a tick had somehow
            # overshot the boundary, which is the budget's job to prevent --
            # a silent overshoot is impossible.
            clock.set(origin + (i + 1) * TICK_PERIOD_MS)
            await platform.step_market(1)

            # Enabled around the whole tick, which is still "after the
            # snapshot boundary": `tick()` publishes nothing before
            # `build_state()` has already read the clock, so `created_at` --
            # and therefore `tick_time` and the ORCHESTRATOR_TICK marker --
            # is the same instant in every run, and drift begins at the
            # MARKET_STATE publication that follows it.
            with drift.active():
                await platform.orchestrator.tick()
                await platform.bus.drain()

        await platform.bus.drain()
        summary = replay_equivalence_summary(platform)
        fingerprint = market_input_fingerprint(inputs)
        await platform.stop()
        return summary, fingerprint, drift.total_advanced_ms

    async def test_same_trades_whatever_the_mid_tick_drift(self, settings):
        baseline, baseline_inputs, baseline_drift = await self._run(settings, 0)

        assert baseline["fills"], "the scenario must trade for this to prove anything"
        assert baseline_inputs, "the scenario must produce market inputs"
        assert baseline_drift == 0, "the reference run must have no drift at all"

        for step_ms in (1, 7, 40):
            summary, inputs, injected = await self._run(settings, step_ms)

            # The premise, checked first. If this fails the economic
            # comparison below is meaningless and must not be reported as a
            # production finding.
            assert inputs == baseline_inputs, (
                f"a mid-tick clock advance of {step_ms}ms changed the MARKET "
                "INPUT STREAM; the runs are not comparable and the harness, "
                "not the platform, is at fault"
            )
            assert injected > 0, (
                f"no drift was actually injected at step_ms={step_ms}, so this "
                "iteration proves nothing"
            )

            assert summary == baseline, (
                f"a mid-tick clock advance of {step_ms}ms changed the "
                "economic outcome"
            )


class TestTickTimeIdentity:
    """The three clocks Section 3 requires never to drift apart."""

    async def test_snapshot_tick_time_and_marker_are_one_value(
        self, settings
    ):
        platform = _build(settings)
        clock = platform.clock
        markers: list[int] = []
        platform.bus.subscribe(
            lambda e: markers.append(e.ts_ms) or _noop(),
            types=[EventType.ORCHESTRATOR_TICK],
            name="markers",
        )
        await platform.start(record=False, feeds=False)
        drift = MidTickClockAdvancer(clock, step_ms=29)
        platform.bus.add_middleware(drift)

        captured: list[tuple[int, int]] = []
        original_body = platform.orchestrator._tick_body

        async def spy(market):
            captured.append((market.created_at, platform.orchestrator.tick_time))
            return await original_body(market)

        platform.orchestrator._tick_body = spy

        for _ in range(6):
            clock.advance(100)
            await platform.step_market(1)
            with drift.active():
                await platform.orchestrator.tick()
        await platform.bus.drain()

        assert drift.advances > 0, "the clock must have moved inside a tick"
        assert len(captured) == 6
        assert len(markers) == 6
        for (created_at, tick_time), marker_ts in zip(captured, markers, strict=True):
            assert created_at == tick_time == marker_ts

        await platform.stop()


async def _noop() -> None:
    return None


@pytest.mark.parametrize("component", ["detector", "tidal", "noro", "zephr", "rune_core"])
def test_fast_loop_entry_points_take_explicit_time(component):
    """Structural guard: the economic entry points cannot silently go back
    to reading a clock, because the time is a required argument.
    """
    import inspect

    targets = {
        "detector": ("strategies.cross_venue.detector", "CrossVenueDetector", "detect"),
        "tidal": ("agents.tidal.agent", "Tidal", "evaluate"),
        "noro": ("agents.noro.agent", "Noro", "evaluate"),
        "zephr": ("agents.zephr.agent", "Zephr", "evaluate"),
        "rune_core": ("agents.rune.core", "RuneCore", "evaluate"),
    }
    module_name, cls_name, method = targets[component]
    module = __import__(module_name, fromlist=[cls_name])
    params = inspect.signature(getattr(getattr(module, cls_name), method)).parameters
    assert "now_ms" in params, f"{cls_name}.{method}() must take an explicit now_ms"
