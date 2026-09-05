"""Phase 3 audit, Sections 25 / 43 / 44: NORO on the real simulated market.

Everything else in this audit reasons about constructed states. This suite
drives the REAL platform -- TIDAL, the detector, NORO, ZEPHR, consensus, RUNE,
VESKA -- on its seeded synthetic market and measures what NORO actually does.

The headline measurement, over 5,000 ticks (reproduced at a shorter length
here so the suite stays fast; the full run is
``scripts/audit_noro_simulation.py``):

    distinct opportunities        82
    FIRST evaluations (entry)     82, of which 0 were negative
    RE-evaluations (monitoring) 1166, of which 48 were negative

So NORO's ENTRY vote was positive every single time, and every rejection it
produced came from continuous re-evaluation after entry -- where the market
has moved away from the opportunity's original legs and the two-venue
tautology no longer binds.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.wiring import build_platform
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings, simulated_venues
from core.events import EventType
from core.models.common import AgentId
from simulation.market import default_market
from storage import InMemoryEventStore
from tests.conftest import START_MS

TICKS = 1_500


@pytest.fixture(scope="module")
def run():
    """One real platform run, shared by every test here."""
    import asyncio

    async def drive():
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
        published: dict[str, int] = {}
        rows: list[tuple[str, int, dict]] = []

        async def capture(event):
            if event.type is EventType.OPPORTUNITY_DETECTED:
                published[event.correlation_id] = (
                    published.get(event.correlation_id, 0) + 1
                )
            elif (
                event.type is EventType.AGENT_OPINION
                and event.payload.get("agent_id") == AgentId.NORO.value
            ):
                rows.append(
                    (
                        event.correlation_id,
                        published.get(event.correlation_id, 0),
                        event.payload,
                    )
                )

        bus.subscribe(capture, name="phase3-capture")
        await platform.start(record=False)
        for _ in range(TICKS):
            clock.advance(100)
            await platform.step_market(1)
            await platform.orchestrator.tick()
        await bus.drain()
        await platform.stop()
        return published, rows

    return asyncio.run(drive())


@pytest.fixture
def rows(run):
    return run[1]


@pytest.fixture
def first_evaluations(rows):
    return [payload for _, occurrence, payload in rows if occurrence == 1]


@pytest.fixture
def re_evaluations(rows):
    return [payload for _, occurrence, payload in rows if occurrence > 1]


class TestNoroAlwaysAnswers:
    def test_every_opportunity_received_an_opinion(self, run):
        published, rows = run
        assert sum(published.values()) == len(rows), (
            "NORO answered every OPPORTUNITY_DETECTED event"
        )

    def test_a_meaningful_number_of_opportunities_occurred(self, run):
        published, rows = run
        assert len(published) >= 10, f"only {len(published)} distinct opportunities"
        assert len(rows) >= 100


class TestTheEntryVoteIsUnanimouslyPositive:
    """The central production finding of this audit."""

    def test_no_entry_evaluation_was_ever_negative(self, first_evaluations):
        negative = [p for p in first_evaluations if p["signal"] < 0]
        assert first_evaluations, "there were entry evaluations to check"
        assert negative == [], (
            f"{len(negative)}/{len(first_evaluations)} entry votes were "
            "negative -- the two-venue proof says zero"
        )

    def test_every_entry_evaluation_saw_exactly_two_venues(self, first_evaluations):
        counts = {p["detail"]["venues_priced"] for p in first_evaluations}
        assert counts == {2}, (
            f"the simulated market is two-venue, so the tautology binds: {counts}"
        )

    def test_the_entry_votes_are_overwhelmingly_saturated(self, first_evaluations):
        saturated = sum(1 for p in first_evaluations if p["signal"] >= 1.0)
        share = saturated / len(first_evaluations)
        assert share > 0.5, (
            f"only {share:.1%} of entry votes saturated at +1"
        )


class TestRejectionComesFromReevaluation:
    def test_every_negative_opinion_was_a_re_evaluation(self, rows):
        negatives = [
            (occurrence, payload["signal"])
            for _, occurrence, payload in rows
            if payload["signal"] < 0
        ]
        assert negatives, "the run produced some negative opinions"
        assert all(occurrence > 1 for occurrence, _ in negatives), (
            "a negative NORO opinion only ever arises after entry, when the "
            "market has moved away from the opportunity's original legs"
        )

    def test_re_evaluation_is_where_noro_discriminates(self, re_evaluations):
        assert re_evaluations
        negative = sum(1 for p in re_evaluations if p["signal"] < 0)
        assert negative > 0, (
            "continuous re-evaluation is the one path where NORO's rejection "
            "machinery actually fires in production"
        )


class TestConfidenceIsSaturatedInPractice:
    def test_confidence_is_essentially_always_one(self, rows):
        confidences = [payload["confidence"] for _, _, payload in rows]
        assert min(confidences) == pytest.approx(1.0), (
            f"lowest confidence observed across the whole run: "
            f"{min(confidences):.4f} -- the field carries no information here"
        )

    def test_because_simulated_liquidity_far_exceeds_the_hard_threshold(self, rows):
        liquidity = [payload["detail"]["total_liquidity"] for _, _, payload in rows]
        assert min(liquidity) > 250_000.0, (
            "every observation is above the hard-coded $250k saturation point, "
            "so the liquidity term is pinned at 1.0 throughout"
        )


class TestSignalDistribution:
    def test_the_signal_lives_at_the_top_of_its_range(self, rows):
        signals = [payload["signal"] for _, _, payload in rows]
        saturated = sum(1 for s in signals if s >= 1.0)
        assert saturated / len(signals) > 0.5, (
            f"{saturated}/{len(signals)} opinions are exactly +1 -- above the "
            "saturation point the signal stops distinguishing anything"
        )

    def test_the_edge_routinely_exceeds_saturation_by_a_wide_margin(self, rows):
        edges = [
            payload["detail"]["confirmed_edge_bps"] for _, _, payload in rows
        ]
        saturation = load_settings().noro.saturation_bps
        beyond = sum(1 for e in edges if e > 2 * saturation)
        assert beyond / len(edges) > 0.5, (
            f"{beyond}/{len(edges)} edges exceed twice saturation_bps "
            f"({saturation}), so most of the measured signal is discarded by "
            "the clamp"
        )
