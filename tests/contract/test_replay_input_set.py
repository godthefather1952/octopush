"""Phase 2 finalization, §38: what replay must feed back, and what it must not.

Replay's correctness rests on one classification, and the classification lives
in two frozensets (``MARKET_INPUT_TYPES`` and
``EXTERNAL_INTELLIGENCE_SOURCES``) that nothing forced anyone to keep current.
Over Batch 1.x the platform grew event types, the orchestrator grew a tick
marker, and LUMEN's opinions turned out to be exogenous (P2-16) -- each an
opportunity for an input to be silently omitted, which does not fail: it
produces a replay that runs cleanly and answers a different question.

So the classification is written out here, deliberately by hand, and the suite
fails if a new ``EventType`` appears without a decision about it. Two mistakes
are possible and both are caught:

* **Omitting an exogenous input** -- replay cannot reconstruct it, so the
  replayed run diverges from the recorded one while reporting fidelity.
* **Replaying a derived event** -- the platform stops recomputing it, so a
  code change to whatever produced it no longer shows up as a different
  answer, which is the entire purpose of replay.
"""

from __future__ import annotations

import pytest

from core.events import MARKET_INPUT_TYPES, EventType
from replay.engine import (
    EXTERNAL_INTELLIGENCE_SOURCES,
    EXTERNAL_INTELLIGENCE_TYPE,
    TICK_MARKER_TYPE,
)

#: Arrives from OUTSIDE the platform: a venue feed. Replay must publish these
#: back, because nothing in the recording could recompute them.
EXOGENOUS_MARKET_INPUTS = {
    EventType.BOOK_SNAPSHOT,
    EventType.BOOK_DELTA,
    EventType.TRADE_PRINT,
    EventType.VENUE_CONNECTED,
    EventType.VENUE_DISCONNECTED,
    #: Declared and replayable, but nothing publishes it today: a feed
    #: sequence gap is detected inside ``LocalOrderBook`` and handled there
    #: (a counter plus a resync request), never as a bus event. Kept in the
    #: replay input set deliberately -- if an adapter ever does publish one,
    #: it is exogenous by construction, and having replay already treat it as
    #: an input is safer than discovering the omission from a divergence.
    EventType.VENUE_SEQUENCE_GAP,
}

#: Recorded for audit, consumed by nothing. ``storage.record_raw`` emits the
#: unparsed venue payload alongside the parsed events it was parsed into;
#: replaying it as well would double every market update.
RECORDED_BUT_NOT_CONSUMED = {EventType.MARKET_UPDATE}

#: Replay's own control channel: a tick boundary, not data. Never published
#: onto the bus -- the CLI and every caller turn it into ``orchestrator.tick()``.
CONTROL = {TICK_MARKER_TYPE}

#: Produced by the platform from market inputs, and therefore RECOMPUTED.
#: ``AGENT_OPINION`` is here because most opinions are derived (TIDAL, NORO,
#: ZEPHR); the exogenous LUMEN subset is separated by SOURCE, not by type --
#: see ``EXTERNAL_INTELLIGENCE_SOURCES``.
DERIVED = {
    EventType.MARKET_STATE,
    EventType.BOOK_RESYNC_REQUESTED,
    EventType.FEED_STALE,
    EventType.OPPORTUNITY_DETECTED,
    EventType.OPPORTUNITY_EXPIRED,
    EventType.AGENT_OPINION,
    EventType.CONSENSUS_UPDATED,
    EventType.RISK_EVALUATION_REQUEST,
    EventType.RISK_PASS,
    EventType.RISK_FAIL,
    EventType.TRADE_INTENT,
    EventType.EXECUTION_PLAN,
    EventType.PAPER_ORDER_CREATED,
    EventType.PAPER_ORDER_UPDATED,
    EventType.PAPER_FILL,
    EventType.EXECUTION_REPORT,
    EventType.POSITION_UPDATED,
    EventType.PORTFOLIO_STATE,
    EventType.DELTA_REPORT,
    EventType.HEDGE_INTENT,
    EventType.RECONCILIATION_COMPLETE,
    EventType.RECONCILIATION_MISMATCH,
    EventType.STRATEGY_STATE_CHANGED,
    EventType.TRADE_ATTRIBUTION,
    EventType.HEALTH_HEARTBEAT,
    EventType.KILL_SWITCH_TRIGGERED,
    EventType.KILL_SWITCH_CLEARED,
    EventType.SYSTEM_EVENT,
    EventType.ERROR,
}


class TestEveryEventTypeIsClassified:
    def test_nothing_is_unclassified(self):
        """A new event type must be a deliberate replay decision.

        If this fails, add the new type to exactly one set above -- after
        deciding whether replay has to feed it back. Guessing is the failure
        mode: an omitted exogenous input produces a clean replay of a
        different session.
        """
        classified = (
            EXOGENOUS_MARKET_INPUTS | RECORDED_BUT_NOT_CONSUMED | CONTROL | DERIVED
        )
        unclassified = set(EventType) - classified
        assert not unclassified, (
            f"unclassified event types: {sorted(t.value for t in unclassified)}"
        )

    def test_the_classes_do_not_overlap(self):
        classes = [
            EXOGENOUS_MARKET_INPUTS,
            RECORDED_BUT_NOT_CONSUMED,
            CONTROL,
            DERIVED,
        ]
        for i, first in enumerate(classes):
            for second in classes[i + 1 :]:
                assert not (first & second), (
                    f"an event type cannot be both: {first & second}"
                )


class TestTheEngineAgreesWithTheClassification:
    def test_every_exogenous_input_is_a_replay_input(self):
        missing = EXOGENOUS_MARKET_INPUTS - MARKET_INPUT_TYPES
        assert not missing, (
            f"{sorted(t.value for t in missing)} arrive from outside the "
            "platform and cannot be recomputed, so replay must publish them "
            "back -- otherwise the replayed run silently diverges"
        )

    def test_no_derived_event_is_a_replay_input(self):
        wrong = DERIVED & MARKET_INPUT_TYPES
        assert not wrong, (
            f"{sorted(t.value for t in wrong)} are computed by the platform. "
            "Replaying them back means a code change to whatever produces "
            "them no longer changes the replayed answer"
        )

    def test_the_market_input_set_is_exactly_the_exogenous_set(self):
        assert MARKET_INPUT_TYPES == EXOGENOUS_MARKET_INPUTS

    def test_the_tick_marker_is_never_a_market_input(self):
        """It carries no market data. Feeding it to the pipeline would be
        replaying replay's own control channel as if it were the market."""
        assert TICK_MARKER_TYPE not in MARKET_INPUT_TYPES

    def test_the_raw_channel_is_not_replayed(self):
        assert not (RECORDED_BUT_NOT_CONSUMED & MARKET_INPUT_TYPES)


class TestExternalIntelligenceIsSeparatedBySource:
    """P2-16. The one case where the TYPE cannot decide the question."""

    def test_the_intelligence_type_is_a_derived_type(self):
        assert EXTERNAL_INTELLIGENCE_TYPE in DERIVED, (
            "most AGENT_OPINIONs are derived; only certain SOURCES are not"
        )

    def test_it_is_not_a_market_input(self):
        """Adding it wholesale would replay TIDAL/NORO/ZEPHR opinions back."""
        assert EXTERNAL_INTELLIGENCE_TYPE not in MARKET_INPUT_TYPES

    def test_only_lumen_is_external(self):
        assert frozenset({"LUMEN"}) == EXTERNAL_INTELLIGENCE_SOURCES

    @pytest.mark.parametrize("agent", ["TIDAL", "NORO", "ZEPHR", "RUNE", "OKAPI"])
    def test_deterministic_agents_are_recomputed(self, agent):
        assert agent not in EXTERNAL_INTELLIGENCE_SOURCES


class TestTheVenuePublisherEmitsOnlyClassifiedTypes:
    """The exogenous boundary, checked against the code that actually crosses it.

    ``VenueFeedPublisher`` is the single bridge from an adapter's normalised
    output onto the bus, so whatever it can emit IS the exogenous set.
    """

    def test_every_publishable_topic_is_a_replay_input_or_deliberately_not(self):
        from apps.orchestrator.wiring import _MESSAGE_TOPICS

        for topic in _MESSAGE_TOPICS.values():
            assert topic in MARKET_INPUT_TYPES, (
                f"{topic.value} is published by a venue feed and must be "
                "replayable"
            )

    def test_the_venue_status_topics_are_replay_inputs(self):
        for topic in (EventType.VENUE_CONNECTED, EventType.VENUE_DISCONNECTED):
            assert topic in MARKET_INPUT_TYPES

    def test_the_raw_topic_is_the_one_deliberate_exception(self):
        """``publish_raw`` emits MARKET_UPDATE carrying the unparsed payload.
        It is exogenous, and deliberately NOT replayed: the parsed events it
        was parsed into are replayed instead, and doing both would apply every
        market update twice."""
        assert EventType.MARKET_UPDATE not in MARKET_INPUT_TYPES
        assert EventType.MARKET_UPDATE in RECORDED_BUT_NOT_CONSUMED
