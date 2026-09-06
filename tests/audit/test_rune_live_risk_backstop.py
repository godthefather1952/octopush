"""Phase 5 Remediation C — the post-fill hard-limit backstop (P5-3, P5-6).

RUNE answers "can this trade be authorised given what we know now?". The kill
switch answers a different question every tick: "has the state that ACTUALLY
exists crossed a hard boundary?". A multi-leg trade can transiently create
exposure no pre-trade projection can guarantee away — one leg fills before the
other, fills land away from expected prices, a fill partials, a cancel loses a
race — so prevention and detection are both required.

These tests pin the detection layer at its exact boundary, one dimension at a
time. Every scenario sets ONE limit tight and leaves the rest generous, so a
breach in the dimension under test cannot be produced or masked by another.

``CommittedExposure`` is the input of choice for most of them: it dials a
single dimension to an exact value without having to construct a portfolio
whose marks happen to land there, and it is the same value the pre-trade gates
read since P5-1, so a test written against it exercises the real coupling
rather than a parallel one.
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import Settings, load_settings, simulated_venues
from core.models.opportunity import STRATEGY_TRANSITIONS, StrategyState
from core.models.risk import CommittedExposure
from risk.kill_switch import (
    TRIGGER_ACTIONS,
    TRIGGERS,
    KillSwitch,
    KillSwitchInputs,
    live_risk_breaches,
)
from tests.audit.rune_fixtures import SYMBOL, VENUE_A, portfolio, portfolio_with, position
from tests.conftest import START_MS

#: The smallest amount that must read as "over". Deliberately tiny: the point
#: is that the boundary is exact, not that there is a safety band.
EPS = 1e-3

POSITION_KEY = f"{VENUE_A}:{SYMBOL}"


def cfg(**tight) -> Settings:
    """Settings with every live limit generous except the ones named.

    The coherence validator requires
    ``max_order_notional <= max_position_notional <= max_gross_exposure``, so a
    test tightening gross or position passes both together rather than relying
    on a default that would no longer be coherent.
    """
    base = load_settings().model_copy(update={"venues": simulated_venues()})
    limits = {
        "max_gross_exposure": 1_000_000.0,
        "max_net_exposure": 1_000_000.0,
        "max_venue_exposure": 1_000_000.0,
        "max_strategy_exposure": 1_000_000.0,
        "max_position_notional": 1_000_000.0,
        "max_leverage": 1_000.0,
        "max_unhedged_notional": 1_000_000.0,
    }
    limits.update(tight)
    return base.model_copy(update={"risk": base.risk.model_copy(update=limits)})


def inputs(**overrides) -> KillSwitchInputs:
    """A flat, healthy platform — nothing but the dimension under test moves."""
    fields = {
        "portfolio": portfolio(),
        "health": None,
        "required_components": [],
    }
    fields.update(overrides)
    return KillSwitchInputs(**fields)


def breaches(settings: Settings, **overrides) -> list[str]:
    return live_risk_breaches(inputs(**overrides), settings)


def switch(settings: Settings) -> KillSwitch:
    return KillSwitch(
        bus=InMemoryEventBus(raise_on_handler_error=True),
        clock=ManualClock(START_MS),
        settings=settings,
    )


# ======================================================================
# P5-3 — the predicate exists and is evaluated
# ======================================================================


class TestTheBackstopIsAutomatic:
    def test_risk_limit_breach_has_a_predicate(self):
        """P5-3's headline: the action mapping is no longer unreachable."""
        assert "RISK_LIMIT_BREACH" in TRIGGERS

    def test_it_engages_on_the_first_observation(self):
        """No confirmation delay. A measurement can blip; a breached hard
        boundary is true the moment it is observed, and waiting two more ticks
        means two more ticks of trading past a limit."""
        from risk.kill_switch import CONFIRMATIONS

        assert CONFIRMATIONS.get("RISK_LIMIT_BREACH", 1) == 1

    async def test_one_evaluation_is_enough_to_engage(self):
        """Behaviour, not just configuration."""
        engine = switch(cfg(max_gross_exposure=50_000.0, max_position_notional=50_000.0))
        fired = await engine.evaluate(
            inputs(committed_exposure=CommittedExposure(gross_exposure=50_000.0 + EPS))
        )
        assert "RISK_LIMIT_BREACH" in fired
        assert not engine.state.trading_allowed

    def test_the_predicate_is_pure(self):
        """Same inputs, same answer — no clock, no randomness, no network."""
        settings = cfg(max_net_exposure=10_000.0)
        state = inputs(
            committed_exposure=CommittedExposure(
                gross_exposure=20_000.0, net_exposure=20_000.0
            )
        )
        first = live_risk_breaches(state, settings)
        second = live_risk_breaches(state, settings)
        assert first == second == ["MAX_NET_EXPOSURE"]

        source = inspect.getsource(live_risk_breaches)
        for forbidden in ("clock", "random", "time.time", "datetime", "await"):
            assert forbidden not in source


# ======================================================================
# §32 A-I — the exact boundary, one dimension at a time
# ======================================================================


class TestGrossBoundary:
    LIMIT = 100_000.0
    SETTINGS = cfg(max_gross_exposure=LIMIT, max_position_notional=LIMIT)

    def test_exactly_at_the_limit_is_safe(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(gross_exposure=self.LIMIT),
        ) == []

    def test_one_epsilon_over_is_a_breach(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(gross_exposure=self.LIMIT + EPS),
        ) == ["MAX_GROSS_EXPOSURE"]

    def test_a_filled_book_over_the_limit_is_a_breach(self):
        """The dimension is filled + committed, so filled alone also counts."""
        book = portfolio_with(position(VENUE_A, quantity=1_500.0))
        assert book.gross_exposure > self.LIMIT
        assert "MAX_GROSS_EXPOSURE" in breaches(self.SETTINGS, portfolio=book)


class TestNetBoundary:
    LIMIT = 30_000.0
    SETTINGS = cfg(max_net_exposure=LIMIT)

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_exactly_at_the_limit_is_safe(self, sign):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT, net_exposure=self.LIMIT * sign
            ),
        ) == []

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_one_epsilon_over_is_a_breach_in_either_direction(self, sign):
        """Net exposure is signed; the limit is on its magnitude."""
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT + EPS,
                net_exposure=(self.LIMIT + EPS) * sign,
            ),
        ) == ["MAX_NET_EXPOSURE"]

    def test_an_offsetting_commitment_is_not_a_breach(self):
        """A working BUY and a working SELL net to nothing, exactly as two
        filled positions would. Over-reserving here would halt a book that is
        genuinely neutral."""
        book = portfolio_with(position(VENUE_A, quantity=400.0))
        assert book.net_exposure == pytest.approx(40_000.0)
        assert breaches(
            self.SETTINGS,
            portfolio=book,
            committed_exposure=CommittedExposure(
                gross_exposure=40_000.0, net_exposure=-40_000.0
            ),
        ) == []


class TestVenueBoundary:
    LIMIT = 60_000.0
    SETTINGS = cfg(max_venue_exposure=LIMIT)

    def test_exactly_at_the_limit_is_safe(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT, venue_exposure={VENUE_A: self.LIMIT}
            ),
        ) == []

    def test_one_epsilon_over_is_a_breach(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT + EPS,
                venue_exposure={VENUE_A: self.LIMIT + EPS},
            ),
        ) == ["MAX_VENUE_EXPOSURE"]

    def test_filled_and_committed_on_one_venue_add_up(self):
        """§32-H. Neither side breaches alone; together they do."""
        book = portfolio_with(position(VENUE_A, quantity=400.0))
        assert book.exposure_by_venue()[VENUE_A] == pytest.approx(40_000.0)
        committed = CommittedExposure(
            gross_exposure=30_000.0, venue_exposure={VENUE_A: 30_000.0}
        )
        assert breaches(self.SETTINGS, portfolio=book) == []
        assert breaches(self.SETTINGS, committed_exposure=committed) == []
        assert breaches(
            self.SETTINGS, portfolio=book, committed_exposure=committed
        ) == ["MAX_VENUE_EXPOSURE"]


class TestPositionBoundary:
    LIMIT = 50_000.0
    SETTINGS = cfg(max_position_notional=LIMIT)

    def test_exactly_at_the_limit_is_safe(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT,
                position_exposure={POSITION_KEY: self.LIMIT},
            ),
        ) == []

    def test_one_epsilon_over_is_a_breach(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT + EPS,
                position_exposure={POSITION_KEY: self.LIMIT + EPS},
            ),
        ) == ["MAX_POSITION_NOTIONAL"]

    def test_filled_and_committed_on_one_position_add_up(self):
        book = portfolio_with(position(VENUE_A, quantity=400.0))
        assert book.positions[POSITION_KEY].notional == pytest.approx(40_000.0)
        committed = CommittedExposure(
            gross_exposure=20_000.0, position_exposure={POSITION_KEY: 20_000.0}
        )
        assert breaches(self.SETTINGS, portfolio=book) == []
        assert breaches(
            self.SETTINGS, portfolio=book, committed_exposure=committed
        ) == ["MAX_POSITION_NOTIONAL"]


class TestLeverageBoundary:
    LIMIT = 2.0
    SETTINGS = cfg(max_leverage=LIMIT)

    def test_exactly_at_the_limit_is_safe(self):
        """Equity is 100,000, so 200,000 of gross is exactly 2.0x."""
        assert portfolio().equity == pytest.approx(100_000.0)
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(gross_exposure=200_000.0),
        ) == []

    def test_one_epsilon_over_is_a_breach(self):
        assert breaches(
            self.SETTINGS,
            committed_exposure=CommittedExposure(gross_exposure=200_000.0 + EPS),
        ) == ["MAX_LEVERAGE"]

    def test_non_positive_equity_carrying_exposure_is_a_breach(self):
        """Unbounded leverage, reported rather than divided by."""
        book = portfolio(cash=-50_000.0)
        assert book.equity <= 0
        assert "MAX_LEVERAGE" in breaches(
            self.SETTINGS,
            portfolio=book,
            committed_exposure=CommittedExposure(gross_exposure=1_000.0),
        )

    def test_flat_and_insolvent_does_not_manufacture_a_leverage_breach(self):
        """§13. With no exposure there is no leverage to breach; insolvency is
        a different condition with different semantics, and inventing an
        infinity here would fire this trigger for the wrong reason."""
        book = portfolio(cash=-50_000.0)
        assert book.equity <= 0
        assert book.gross_exposure == pytest.approx(0.0)
        assert "MAX_LEVERAGE" not in breaches(self.SETTINGS, portfolio=book)


class TestStrategyBoundary:
    LIMIT = 80_000.0
    SETTINGS = cfg(max_strategy_exposure=LIMIT)

    def test_exactly_at_the_limit_is_safe(self):
        assert breaches(self.SETTINGS, strategy_exposure=self.LIMIT) == []

    def test_one_epsilon_over_is_a_breach(self):
        assert breaches(self.SETTINGS, strategy_exposure=self.LIMIT + EPS) == [
            "MAX_STRATEGY_EXPOSURE"
        ]

    def test_committed_exposure_is_not_added_to_the_strategy_budget(self):
        """§16 / P5-2. ``strategy_exposure`` is reserved at authorisation and
        held for the whole lifecycle, so it already covers working trades.
        Adding committed exposure on top would count the same trade twice and
        halve the effective budget."""
        assert breaches(
            self.SETTINGS,
            strategy_exposure=self.LIMIT,
            committed_exposure=CommittedExposure(gross_exposure=self.LIMIT),
        ) == []


class TestUnhedgedBoundary:
    """§17 — the dimension the production probe actually breached.

    25,103.80 against a 10,000 limit, while the only emergency path watching
    it waited for three times that. This closes the band.
    """

    LIMIT = 10_000.0
    SETTINGS = cfg(max_unhedged_notional=LIMIT)

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_exactly_at_the_limit_is_safe(self, sign):
        assert breaches(self.SETTINGS, unhedged_notional=self.LIMIT * sign) == []

    @pytest.mark.parametrize("sign", [1.0, -1.0])
    def test_one_epsilon_over_is_a_breach(self, sign):
        assert breaches(
            self.SETTINGS, unhedged_notional=(self.LIMIT + EPS) * sign
        ) == ["MAX_UNHEDGED_EXPOSURE"]

    def test_the_observed_production_value_is_a_breach(self):
        """The concrete number, so the finding stays legible."""
        assert breaches(self.SETTINGS, unhedged_notional=25_103.800333426625) == [
            "MAX_UNHEDGED_EXPOSURE"
        ]

    def test_it_fires_far_below_the_unexpected_position_level(self):
        """Both layers are pinned. ``UNEXPECTED_POSITION`` remains the 3x
        catastrophic trigger and is not weakened; this one covers the band
        between the configured limit and that multiple."""
        assert "UNEXPECTED_POSITION" in TRIGGERS
        catastrophic = self.LIMIT * 3
        middle = (self.LIMIT + catastrophic) / 2
        assert middle < catastrophic
        assert breaches(self.SETTINGS, unhedged_notional=middle) == [
            "MAX_UNHEDGED_EXPOSURE"
        ]

    def test_committed_exposure_does_not_offset_the_residual(self):
        """An unfilled second leg is a commitment, but it has not neutralised
        the one-sided position that exists NOW. Letting it net here is exactly
        how a real residual would be reported as hedged."""
        assert breaches(
            self.SETTINGS,
            unhedged_notional=self.LIMIT + EPS,
            committed_exposure=CommittedExposure(
                gross_exposure=self.LIMIT, net_exposure=-(self.LIMIT + EPS)
            ),
        ) == ["MAX_UNHEDGED_EXPOSURE"]


class TestNothingIsNotABreach:
    """§32-I — the control. A quiet platform must stay quiet, or the backstop
    halts every run it is installed in."""

    def test_a_flat_healthy_platform_breaches_nothing(self):
        assert breaches(cfg()) == []

    def test_the_shipped_limits_are_not_breached_by_an_empty_book(self):
        settings = load_settings().model_copy(update={"venues": simulated_venues()})
        assert live_risk_breaches(inputs(), settings) == []

    async def test_and_the_switch_stays_clear(self):
        engine = switch(cfg())
        fired = await engine.evaluate(inputs())
        assert "RISK_LIMIT_BREACH" not in fired
        assert engine.state.trading_allowed


# ======================================================================
# §19 — what the trigger does about it
# ======================================================================


class TestTheResponse:
    def test_it_halts_cancels_and_flattens(self):
        from core.models.ops import KillAction

        actions = TRIGGER_ACTIONS["RISK_LIMIT_BREACH"]
        assert KillAction.HALT_NEW_TRADES in actions
        assert KillAction.CANCEL_ALL in actions, (
            "resting entry orders must be pulled, or one of them fills and "
            "re-opens the exposure the flatten just closed"
        )
        assert KillAction.FLATTEN in actions, (
            "halting new entries alone leaves the breaching position in place"
        )

    def test_it_does_not_disable_execution(self):
        """§24. Flattening and hedging are submissions; a trigger that must
        reduce exposure cannot also refuse to submit."""
        from core.models.ops import KillAction

        assert KillAction.DISABLE_EXECUTION not in TRIGGER_ACTIONS["RISK_LIMIT_BREACH"]

    async def test_the_state_reflects_all_three_after_engaging(self):
        engine = switch(cfg(max_net_exposure=1_000.0))
        await engine.evaluate(
            inputs(
                committed_exposure=CommittedExposure(
                    gross_exposure=5_000.0, net_exposure=5_000.0
                )
            )
        )
        assert not engine.state.trading_allowed
        assert engine.state.cancel_all_requested
        assert engine.state.flatten_requested
        assert not engine.state.execution_disabled

    async def test_engaging_publishes_the_ordinary_kill_switch_event(self):
        """§31 — one event type, through the normal engage path."""
        from core.events import EventType

        seen: list[str] = []

        async def collect(event):
            seen.append(event.payload.get("kind", "?"))

        engine = switch(cfg(max_gross_exposure=50_000.0, max_position_notional=50_000.0))
        engine.bus.subscribe(
            collect, types=[EventType.KILL_SWITCH_TRIGGERED], name="breach-observer"
        )
        await engine.evaluate(
            inputs(committed_exposure=CommittedExposure(gross_exposure=60_000.0))
        )
        await engine.bus.drain()
        assert "RISK_LIMIT_BREACH" in seen


# ======================================================================
# P5-6 — cancel before flatten, and flatten the state that still has orders
# ======================================================================


class TestFlattenReachesExecutingRecords:
    def test_executing_to_exiting_is_already_legal(self):
        """No state-machine change was needed or made."""
        assert StrategyState.EXITING in STRATEGY_TRANSITIONS[StrategyState.EXECUTING]

    def test_flatten_visits_executing(self):
        source = inspect.getsource(Orchestrator._flatten)
        for state in ("EXECUTING", "MONITORING", "RECONCILING", "HEDGING"):
            assert f"StrategyState.{state}" in source

    def test_cancel_is_applied_before_flatten(self):
        """Reversing these would leave the window open: the flatten closes the
        position, then a still-resting entry order fills and re-opens it."""
        source = inspect.getsource(Orchestrator._protect)
        assert source.index(
            "if self.kill_switch.state.cancel_all_requested:"
        ) < source.index("if self.kill_switch.state.flatten_requested:")

    async def test_an_executing_record_is_unwound_by_a_flatten(self, platform):
        """Behaviour, not a source scan.

        A record sitting in EXECUTING with a real position must be moved out of
        EXECUTING by the kill-switch flatten. Before the fix it was skipped
        entirely and stayed exactly where it was — with its entry orders live.
        """
        from core.models.common import Side
        from core.models.execution import FillEvent
        from core.state import OpportunityRecord

        await platform.start(record=False, feeds=False)
        await platform.step_market(1)
        await platform.orchestrator.tick()
        market = platform.orchestrator.state.market
        assert market is not None and market.venues

        state = next(iter(market.venues.values()))
        price = state.metrics.mid
        assert price
        platform.account.apply_fill(
            FillEvent(
                created_at=platform.clock.now_ms(),
                client_order_id="entry-partial",
                venue=state.venue,
                symbol=state.symbol,
                side=Side.BUY,
                quantity=1.0,
                price=price,
            )
        )
        held = platform.account.snapshot().positions[f"{state.venue}:{state.symbol}"]
        assert not held.is_flat, "premise: a partly-filled entry left a position"

        record = OpportunityRecord(
            opportunity=_executing_opportunity(state.venue, state.symbol, price)
        )
        record.state = StrategyState.EXECUTING
        platform.orchestrator.state.opportunities["flatten-probe"] = record

        await platform.orchestrator._flatten(market)

        assert record.state is not StrategyState.EXECUTING, (
            "a flatten that skips EXECUTING leaves the trade whose entry "
            "orders are still live exactly where it was"
        )
        assert record.state in (StrategyState.EXITING, StrategyState.CLOSED)
        await platform.stop()

    def test_the_exit_uses_the_position_actually_held(self):
        """A partly-filled entry must be closed at the quantity that exists,
        never at the notional RUNE authorised."""
        source = inspect.getsource(Orchestrator._submit_exit)
        assert "quantity = abs(position.quantity)" in source


def _executing_opportunity(venue: str, symbol: str, price: float):
    """An Opportunity shaped like the detector's, for one venue/symbol."""
    from core.models.common import Side
    from core.models.opportunity import Opportunity, OpportunityKind, OpportunityLeg

    return Opportunity(
        created_at=START_MS,
        opportunity_id="flatten-probe",
        kind=OpportunityKind.CROSS_VENUE_DISLOCATION,
        strategy="cross_venue",
        symbol=symbol,
        legs=[
            OpportunityLeg(
                venue=venue, symbol=symbol, side=Side.BUY, reference_price=price
            )
        ],
        gross_edge_bps=10.0,
        expires_at=START_MS + 10_000_000,
    )
