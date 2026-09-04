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
"""

from __future__ import annotations

import pytest

from core.clock import ManualClock
from core.events import Event, EventType

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


class ClockAdvancingFeed:
    """Stands in for a live feed: advances the clock on every publication.

    This is the whole point of the test. A tick that only ever sees a
    stationary clock cannot demonstrate the invariant; one running while the
    clock jumps forward at every internal publish can.
    """

    def __init__(self, clock: ManualClock, step_ms: int = 37) -> None:
        self.clock = clock
        self.step_ms = step_ms
        self.advances = 0

    async def __call__(self, event: Event) -> None:
        self.advances += 1
        self.clock.advance(self.step_ms)


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
        # during, the collection of a single event.
        feed = ClockAdvancingFeed(clock)
        platform.bus.add_middleware(feed)

        for _ in range(50):
            clock.advance(100)
            await platform.step_market(1)
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
        platform.bus.add_middleware(ClockAdvancingFeed(clock, step_ms=13))

        for _ in range(5):
            clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()

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


class TestOutcomeIsIndependentOfMidTickClockDrift:
    """The invariant stated as an outcome, not a timestamp.

    How fast a concurrent feed advances the clock *during* a tick is a
    scheduling accident. If the economic result changes with it, the run is
    not reproducible -- replay never reproduces that accident, it re-executes
    the tick at one recorded instant.
    """

    async def test_same_trades_whatever_the_mid_tick_drift(self, settings):
        from tests.replay.test_replay import replay_equivalence_summary

        async def run(step_ms: int) -> dict:
            platform = _build(settings)
            clock = platform.clock
            await platform.start(record=False, feeds=False)
            if step_ms:
                platform.bus.add_middleware(ClockAdvancingFeed(clock, step_ms=step_ms))
            for _ in range(50):
                clock.advance(100)
                await platform.step_market(1)
                await platform.orchestrator.tick()
            await platform.bus.drain()
            summary = replay_equivalence_summary(platform)
            await platform.stop()
            return summary

        baseline = await run(0)
        assert baseline["fills"], "the scenario must trade for this to prove anything"

        for step_ms in (1, 7, 40):
            assert await run(step_ms) == baseline, (
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
        platform.bus.add_middleware(ClockAdvancingFeed(clock, step_ms=29))

        captured: list[tuple[int, int]] = []
        original_body = platform.orchestrator._tick_body

        async def spy(market):
            captured.append((market.created_at, platform.orchestrator.tick_time))
            return await original_body(market)

        platform.orchestrator._tick_body = spy

        for _ in range(6):
            clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        await platform.bus.drain()

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
