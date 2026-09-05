"""P3-4 regression on the real simulated market.

Everything else in this suite reasons about constructed states. This one drives
the REAL platform -- TIDAL, the detector, NORO, ZEPHR, consensus, RUNE, VESKA
-- on its seeded synthetic market and measures what NORO actually does.

**What the audit measured**, over 5,000 ticks:

    distinct opportunities        82
    FIRST evaluations (entry)     82, of which 0 were negative
    RE-evaluations (monitoring) 1166, of which 48 were negative

NORO's entry vote was positive every single time, at a confidence pinned at
1.0 -- and not because it agreed, but because it could not disagree. The
simulated market has exactly two venues per symbol, which are exactly the two
the detector picks, so the benchmark was built from the prices under judgement
and confirmed them by construction. Every rejection came from continuous
re-evaluation after entry, where the market had moved away from the
opportunity's original legs.

**The property this file now protects.** On a two-venue market NORO has no
independent valuation evidence and says so: signal exactly 0.0, confidence
``insufficient_breadth_confidence``, reason
``INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH``. It neither confirms nor
contradicts, and the guaranteed positive entry vote is gone.
"""

from __future__ import annotations

import pytest

from agents.noro.agent import (
    FAIR_VALUE_CONFIRMS_DISLOCATION,
    FAIR_VALUE_CONTRADICTS_DISLOCATION,
    INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH,
)
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
        return published, rows, settings

    return asyncio.run(drive())


@pytest.fixture
def rows(run):
    return run[1]


@pytest.fixture
def settings(run):
    return run[2]


@pytest.fixture
def first_evaluations(rows):
    return [payload for _, occurrence, payload in rows if occurrence == 1]


@pytest.fixture
def re_evaluations(rows):
    return [payload for _, occurrence, payload in rows if occurrence > 1]


class TestNoroAlwaysAnswers:
    """Unchanged, and load-bearing: the neutral verdict is an ANSWER.

    NORO is a required agent, so returning ``None`` on a two-venue market
    would suspend the strategy on the commonest market there is. Missing and
    neutral had to stay different things.
    """

    def test_every_opportunity_received_an_opinion(self, run):
        published, rows, _ = run
        assert sum(published.values()) == len(rows), (
            "NORO answered every OPPORTUNITY_DETECTED event"
        )

    def test_a_meaningful_number_of_opportunities_occurred(self, run):
        published, rows, _ = run
        assert len(published) >= 10, f"only {len(published)} distinct opportunities"
        assert len(rows) >= 100


class TestP3_4_TheEntryVoteIsNoLongerGuaranteedPositive:
    """The central production finding, inverted.

    The audit asserted "no entry evaluation was ever negative" and measured
    82/82 positive. The property now is that none of them is positive either:
    with only the detector's own two venues in the market there is nothing
    independent to confirm with.
    """

    def test_no_entry_evaluation_is_positive(self, first_evaluations):
        positive = [p for p in first_evaluations if p["signal"] > 0]
        assert first_evaluations, "there were entry evaluations to check"
        assert positive == [], (
            f"{len(positive)}/{len(first_evaluations)} entry votes were "
            "positive -- with two venues there is no independent evidence to "
            "be positive about"
        )

    def test_every_entry_evaluation_is_exactly_neutral(self, first_evaluations):
        signals = {p["signal"] for p in first_evaluations}
        assert signals == {0.0}, signals

    def test_every_entry_evaluation_saw_exactly_two_contributors(
        self, first_evaluations
    ):
        counts = {p["detail"]["contributors"] for p in first_evaluations}
        assert counts == {2}, (
            f"the simulated market is two-venue, so no opportunity has an "
            f"independent anchor: {counts}"
        )

    def test_no_entry_evaluation_had_an_independent_contributor(
        self, first_evaluations
    ):
        counts = {p["detail"]["independent_contributors"] for p in first_evaluations}
        assert counts == {0}


class TestEveryOpinionDeclinesToVote:
    def test_no_opinion_anywhere_in_the_run_is_directional(self, rows):
        directional = [
            (occurrence, payload["signal"])
            for _, occurrence, payload in rows
            if payload["signal"] != 0.0
        ]
        assert directional == [], (
            f"{len(directional)} directional votes on a two-venue market"
        )

    def test_the_reason_code_is_always_the_breadth_one(self, rows):
        codes = {tuple(payload["reason_codes"]) for _, _, payload in rows}
        assert codes == {(INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH,)}, codes

    def test_every_opinion_is_flagged_as_an_abstention(self, rows):
        """What actually keeps the two-venue market tradeable.

        A declined vote that still carried weight into the consensus
        denominator would drag every score toward zero, and the platform would
        detect opportunities and never fill one. ``abstain`` removes NORO from
        both sides of the weighted mean, leaving TIDAL and ZEPHR to decide on
        the evidence they do have.
        """
        assert {payload["abstain"] for _, _, payload in rows} == {True}

    def test_no_confirmation_or_contradiction_is_ever_claimed(self, rows):
        for _, _, payload in rows:
            assert FAIR_VALUE_CONFIRMS_DISLOCATION not in payload["reason_codes"]
            assert FAIR_VALUE_CONTRADICTS_DISLOCATION not in payload["reason_codes"]

    def test_re_evaluation_declines_just_as_entry_does(self, re_evaluations):
        """The audit found that all 48 rejections came from re-evaluation --
        the one path where the tautology did not bind. That path now declines
        for the same honest reason as entry.

        Note the subset rather than equality: re-evaluations arise only while
        an opportunity stays in flight, and a declined NORO vote can end that
        earlier than a confirmed one did, so the set may legitimately be
        empty. What must never appear in it is a directional vote.
        """
        assert {p["signal"] for p in re_evaluations} <= {0.0}


class TestP3_8_ConfidenceIsNoLongerPinnedAtOne:
    def test_confidence_is_the_configured_insufficient_breadth_value(
        self, rows, settings
    ):
        confidences = {payload["confidence"] for _, _, payload in rows}
        assert len(confidences) == 1, confidences
        assert confidences.pop() == pytest.approx(
            settings.noro.insufficient_breadth_confidence
        )

    def test_it_is_far_below_the_old_pinned_value(self, rows):
        """Measured across the whole audit run, the lowest confidence NORO
        ever reported was 1.0. Every one of these is a fraction of that."""
        assert max(payload["confidence"] for _, _, payload in rows) < 0.5


class TestTheDetailExplainsTheDeclinedVote:
    def test_it_names_the_contributors_it_did_have(self, first_evaluations):
        for payload in first_evaluations:
            assert payload["detail"]["contributor_venues"].count(",") == 1

    def test_it_reports_no_benchmark(self, first_evaluations):
        for payload in first_evaluations:
            assert payload["detail"]["valuation_benchmark"] is None
            assert payload["detail"]["weakest_confirmation_bps"] is None

    def test_the_model_version_marks_the_new_semantics(self, rows):
        versions = {payload["model_version"] for _, _, payload in rows}
        assert versions == {"noro-0.2"}


class TestFreshnessOnTheRealMarket:
    def test_every_opinion_carries_a_contributor_timestamp(self, rows):
        """P3-7: fail-closed means a published opinion always has one."""
        for _, _, payload in rows:
            assert payload["source_data_timestamp"] is not None

    def test_the_stamp_never_leads_the_opinions_own_creation(self, rows):
        for _, _, payload in rows:
            assert payload["source_data_timestamp"] <= payload["created_at"]
