"""Three previously-fixed health defects, now guarded — P0-H6.

The audit found that no test anywhere referenced ``CONFIRMATIONS``,
``streaks``, ``marin.heartbeat``, ``tradeable_symbols`` or ``market_data_ok``.
All three fixes were made during the build and all three were unguarded, so a
revert would have passed the whole suite.

What each one fixed:

1. **Confirmation streaks.** A measurement — component health, observed
   latency — can blip for one evaluation without anything being wrong. Since
   clearing the kill switch is manual, a single bad sample halting the
   platform permanently would make it useless. A breached limit or a
   corrupted book is true the moment it is observed and still engages at once.

2. **MARIN's heartbeat.** "I am alive" and "I just reconciled everything" are
   different claims on different cadences. Tying them together made a
   component that works every N ticks look dead for N-1 of them.

3. **Tradeable symbols.** A cross-venue strategy needs two usable venues *on
   the same symbol*. Counting usable venue/symbol pairs called the platform
   healthy with one venue entirely dark.
"""

from __future__ import annotations

import pytest

from core.bus import InMemoryEventBus
from core.clock import ManualClock
from core.config import load_settings
from core.models.ops import HealthState, HealthStatus, SystemHealth
from risk.kill_switch import CONFIRMATIONS, KillSwitch, KillSwitchInputs

START_MS = 1_700_000_000_000


@pytest.fixture
def kill_switch():
    return KillSwitch(InMemoryEventBus(), ManualClock(start_ms=START_MS), load_settings())


def unhealthy_health() -> SystemHealth:
    return SystemHealth(
        created_at=START_MS,
        components={
            "TIDAL": HealthState(service="TIDAL", status=HealthStatus.OFFLINE),
        },
    )


def healthy_health() -> SystemHealth:
    return SystemHealth(
        created_at=START_MS,
        components={
            "TIDAL": HealthState(service="TIDAL", status=HealthStatus.HEALTHY),
        },
    )


def portfolio():
    from core.models.portfolio import PortfolioState

    return PortfolioState(
        created_at=START_MS, initial_balance=100_000.0, cash=100_000.0
    )


def inputs(**overrides) -> KillSwitchInputs:
    # required_components must be non-empty or _health checks nothing: it asks
    # `health.required_ok(required_components)`, and an empty list is trivially
    # satisfied however sick the platform is.
    fields = {
        "health": healthy_health(),
        "portfolio": portfolio(),
        "required_components": ["TIDAL"],
    }
    fields.update(overrides)
    return KillSwitchInputs(**fields)


class TestConfirmationStreaks:
    """A blip must not permanently halt a platform that only clears manually."""

    async def test_a_measurement_trigger_does_not_fire_on_one_observation(self, kill_switch):
        fired = await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert fired == []
        assert kill_switch.state.trading_allowed
        assert kill_switch.streaks["SYSTEM_HEALTH_FAILURE"] == 1

    async def test_it_fires_once_the_condition_is_confirmed(self, kill_switch):
        required = CONFIRMATIONS["SYSTEM_HEALTH_FAILURE"]
        for _ in range(required - 1):
            assert await kill_switch.evaluate(inputs(health=unhealthy_health())) == []

        fired = await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert "SYSTEM_HEALTH_FAILURE" in fired
        assert not kill_switch.state.trading_allowed

    async def test_a_condition_that_clears_resets_the_streak(self, kill_switch):
        """The property that makes this a confirmation rather than a counter.

        Two failures, a recovery, then a failure must not add up to three.
        """
        required = CONFIRMATIONS["SYSTEM_HEALTH_FAILURE"]
        for _ in range(required - 1):
            await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert kill_switch.streaks["SYSTEM_HEALTH_FAILURE"] == required - 1

        await kill_switch.evaluate(inputs(health=healthy_health()))
        assert "SYSTEM_HEALTH_FAILURE" not in kill_switch.streaks

        fired = await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert fired == [], "a recovered condition must start counting again"
        assert kill_switch.state.trading_allowed

    async def test_an_unconfirmed_trigger_leaves_trading_allowed(self, kill_switch):
        for _ in range(CONFIRMATIONS["SYSTEM_HEALTH_FAILURE"] - 1):
            await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert kill_switch.state.trading_allowed

    async def test_book_corruption_engages_immediately(self, kill_switch):
        """A corrupted book is true the moment it is seen, not a measurement."""
        assert "BOOK_CORRUPTION" not in CONFIRMATIONS
        fired = await kill_switch.evaluate(inputs(book_corruption=True))
        assert "BOOK_CORRUPTION" in fired
        assert not kill_switch.state.trading_allowed

    async def test_a_reconciliation_mismatch_engages_immediately(self, kill_switch):
        fired = await kill_switch.evaluate(inputs(reconciliation_ok=False))
        assert fired, "a reconciliation mismatch is a fact, not a sample"
        assert not kill_switch.state.trading_allowed

    @pytest.mark.parametrize("trigger,required", sorted(CONFIRMATIONS.items()))
    def test_every_confirmed_trigger_needs_more_than_one_sample(self, trigger, required):
        assert required > 1, f"{trigger} is in CONFIRMATIONS but confirms on one sample"

    async def test_clearing_resets_every_streak(self, kill_switch):
        await kill_switch.evaluate(inputs(health=unhealthy_health()))
        assert kill_switch.streaks
        await kill_switch.clear()
        assert kill_switch.streaks == {}

    async def test_a_trigger_already_engaged_is_not_re_evaluated(self, kill_switch):
        await kill_switch.evaluate(inputs(book_corruption=True))
        before = dict(kill_switch.streaks)
        await kill_switch.evaluate(inputs(book_corruption=True))
        assert kill_switch.streaks == before


class TestMarinHeartbeat:
    """Liveness and reconciliation are different claims on different cadences."""

    @pytest.fixture
    def marin(self):
        from agents.marin import Marin
        from core.health import HealthRegistry
        from execution.oms import OrderManager
        from execution.paper.account import PaperAccount

        clock = ManualClock(start_ms=START_MS)
        return Marin(
            bus=InMemoryEventBus(),
            clock=clock,
            health=HealthRegistry(clock=clock),
            oms=OrderManager(clock=clock),
            account=PaperAccount(clock=clock, initial_balance=100_000.0),
        )

    def test_before_any_reconciliation_it_reports_offline(self, marin):
        """Not healthy-by-default: nothing has been checked yet."""
        marin.heartbeat()
        assert marin.health.status_of("MARIN") is HealthStatus.OFFLINE

    async def test_after_a_clean_run_it_reports_healthy(self, marin):
        await marin.run()
        assert marin.health.status_of("MARIN") is HealthStatus.HEALTHY

    async def test_it_stays_healthy_between_runs(self, marin):
        """The defect: MARIN runs every N ticks and looked dead for N-1.

        A heartbeat on a tick with no reconciliation must still report the
        last known result, or the kill switch sees a required component go
        offline every tick that is not a multiple of the interval.
        """
        await marin.run()
        for _ in range(20):
            marin.clock.advance(250)
            marin.heartbeat()
            assert marin.health.status_of("MARIN") is HealthStatus.HEALTHY

    async def test_a_mismatch_is_still_reported_between_runs(self, marin):
        """Carrying the last result forward must carry bad news too."""
        await marin.run()
        marin.account.cash += 1_000.0
        await marin.run()
        assert marin.health.status_of("MARIN") is HealthStatus.OFFLINE

        marin.clock.advance(250)
        marin.heartbeat()
        assert marin.health.status_of("MARIN") is HealthStatus.OFFLINE


class TestTradeableSymbols:
    """Two usable venues on the *same* symbol, not two usable pairs."""

    def build(self, usable: dict[str, list[str]]):
        """A market where ``usable[symbol]`` lists the venues quoting it."""
        from core.models.common import DataQuality
        from core.models.market import BookMetrics, MarketState, VenueMarketState

        venues = {}
        for symbol, names in usable.items():
            for venue in names:
                venues[f"{venue}:{symbol}"] = VenueMarketState(
                    venue=venue,
                    symbol=symbol,
                    as_of=START_MS,
                    metrics=BookMetrics(),
                    quality=DataQuality.FRESH,
                )
        return MarketState(created_at=START_MS, venues=venues)

    def tradeable(self, market, symbols):
        return [
            symbol
            for symbol in symbols
            if sum(1 for s in market.states_for(symbol) if s.quality.is_usable) >= 2
        ]

    def test_two_venues_on_one_symbol_is_tradeable(self):
        market = self.build({"BTC-USD": ["VENUE_A", "VENUE_B"]})
        assert self.tradeable(market, ["BTC-USD"]) == ["BTC-USD"]

    def test_one_venue_on_one_symbol_is_not(self):
        market = self.build({"BTC-USD": ["VENUE_A"]})
        assert self.tradeable(market, ["BTC-USD"]) == []

    def test_one_venue_on_each_of_two_symbols_is_not_tradeable(self):
        """The defect. Two usable pairs, but no symbol has a counterparty.

        Counting pairs called this healthy while one venue was entirely dark
        and no cross-venue trade was possible on anything.
        """
        market = self.build({"BTC-USD": ["VENUE_A"], "ETH-USD": ["VENUE_B"]})
        assert self.tradeable(market, ["BTC-USD", "ETH-USD"]) == []

    def test_a_partially_dark_universe_reports_only_what_is_tradeable(self):
        market = self.build(
            {"BTC-USD": ["VENUE_A", "VENUE_B"], "ETH-USD": ["VENUE_A"]}
        )
        assert self.tradeable(market, ["BTC-USD", "ETH-USD"]) == ["BTC-USD"]

    async def test_no_tradeable_symbol_is_a_market_data_outage(self, kill_switch):
        required = CONFIRMATIONS["MARKET_DATA_OUTAGE"]
        for _ in range(required - 1):
            assert await kill_switch.evaluate(inputs(market_data_ok=False)) == []
        fired = await kill_switch.evaluate(inputs(market_data_ok=False))
        assert "MARKET_DATA_OUTAGE" in fired

    async def test_a_tradeable_universe_raises_no_outage(self, kill_switch):
        for _ in range(5):
            assert await kill_switch.evaluate(inputs(market_data_ok=True)) == []
        assert kill_switch.state.trading_allowed
