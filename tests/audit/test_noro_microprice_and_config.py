"""P3-10 / P3-11 regression: the price blend, config, state and stability.

* **Microprice (P3-10)** -- ``venue_price`` blends mid and microprice. The
  microprice lies between bid and ask, so on a wide book its distance from mid
  is large: a 60 bps market gave touch imbalance 30 bps of control over the
  venue price, twice the whole saturation band, enough to reverse the vote on
  touch sizes alone. The displacement is now capped.
* **Config (P3-11)** -- what ``NoroConfig`` used to accept that was
  numerically valid and semantically meaningless. Chiefly ``+inf``, which
  ``Field(gt=0)`` waves through.
* **State** -- nothing accumulates across snapshots. Unchanged, still asserted.
* **TTL** -- how NORO's opinion lifetime interacts with consensus. Unchanged.
* **Stability** -- a smoothly changing market must produce a smoothly changing
  signal, and a 0.001 bps configuration change must no longer produce any
  change at all.
"""

from __future__ import annotations

import itertools
import math

import pytest

from agents.noro.fair_value import venue_price
from core.config import NoroConfig
from core.models.common import DataQuality
from tests.audit.helpers import (
    PUBLISHED_BUCKETS,
    START_MS,
    SYMBOL,
    market,
    venue,
)
from tests.audit.noro_fixtures import build_noro, opinion_for


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# Section 6 — microprice semantics
# ======================================================================


#: A cap so wide it can never bind on these books, used where a test is about
#: the BLEND rather than the bound.
UNCAPPED = 10_000.0


def blend(weight: float, cap: float = UNCAPPED) -> NoroConfig:
    return NoroConfig(
        microprice_weight=weight, microprice_max_displacement_bps=cap
    )


class TestVenuePriceBlend:
    """The blend itself, below the cap, is unchanged from v0.1."""

    def test_weight_zero_is_the_mid(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, blend(0.0)) == pytest.approx(100.0)

    def test_weight_one_is_the_microprice(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, blend(1.0)) == pytest.approx(100.5)

    def test_weight_half_is_the_arithmetic_midpoint(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, blend(0.5)) == pytest.approx(100.25)

    @pytest.mark.parametrize("weight", [-5.0, -0.1, 1.1, 99.0])
    def test_an_out_of_range_weight_is_refused_by_configuration(self, weight):
        """The old code clamped silently at the call site. The bound is now a
        config constraint, so a nonsensical weight cannot reach the market."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            NoroConfig(microprice_weight=weight)

    def test_monotone_upward_when_the_microprice_is_above_the_mid(self):
        state = venue("A", 100.0, microprice=100.5)
        previous = -math.inf
        for weight in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            price = venue_price(state, blend(weight))
            assert price >= previous
            previous = price

    def test_monotone_downward_when_the_microprice_is_below_the_mid(self):
        state = venue("A", 100.0, microprice=99.5)
        previous = math.inf
        for weight in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            price = venue_price(state, blend(weight))
            assert price <= previous
            previous = price

    def test_a_missing_microprice_falls_back_to_the_mid(self):
        state = venue("A", 100.0)
        state.metrics.microprice = None
        assert venue_price(state, blend(1.0)) == pytest.approx(100.0)

    def test_a_missing_mid_makes_the_venue_unpriceable(self):
        state = venue("A", 100.0, microprice=100.5)
        state.metrics.mid = None
        assert venue_price(state, blend(0.5)) is None, (
            "no mid means no price, even though a microprice is present"
        )


class TestP3_10_TheMicropriceCannotDominate:
    """P3-10 regression: touch imbalance refines a price; it must not
    overpower the rest of the market.

    The microprice lies strictly between bid and ask, so its distance from mid
    grows with the spread. On a 60 bps book at the default weight that gave it
    30 bps of control over the venue price -- twice the whole saturation band,
    and enough to reverse NORO's vote on touch sizes alone. The displacement
    is now capped at ``microprice_max_displacement_bps``.
    """

    @staticmethod
    def _swing(spread_bps: float, config: NoroConfig) -> float:
        """Full peak-to-peak venue-price movement, in bps, as the microprice
        sweeps from bid to ask."""
        mid = 100.0
        half = mid * spread_bps / 20_000
        low = venue_price(
            venue("A", mid, best_bid=mid - half, best_ask=mid + half,
                  microprice=mid - half),
            config,
        )
        high = venue_price(
            venue("A", mid, best_bid=mid - half, best_ask=mid + half,
                  microprice=mid + half),
            config,
        )
        return (high - low) / mid * 10_000

    def test_a_wide_book_can_no_longer_hand_it_thirty_bps_of_control(self):
        config = NoroConfig()
        assert self._swing(60.0, config) == pytest.approx(
            2 * config.microprice_max_displacement_bps, abs=0.05
        )
        assert self._swing(60.0, config) < config.saturation_bps, (
            "the microprice can no longer move a venue price by more than the "
            "signal's whole saturation band"
        )

    def test_the_uncapped_behaviour_is_what_the_finding_measured(self):
        """Evidence preserved: with the cap lifted, the old 30 bps swing is
        exactly what comes back."""
        assert self._swing(60.0, blend(0.5)) == pytest.approx(30.0, abs=0.1)

    @pytest.mark.parametrize("spread_bps", [60.0, 200.0, 1_000.0])
    def test_the_cap_binds_however_wide_the_spread(self, spread_bps):
        config = NoroConfig()
        assert self._swing(spread_bps, config) == pytest.approx(
            2 * config.microprice_max_displacement_bps, abs=0.05
        )

    def test_a_tight_book_is_unaffected(self):
        """Below the cap nothing changed: on a 1 bps book the displacement is
        a quarter of a bps either way, exactly as before."""
        assert self._swing(1.0, NoroConfig()) == pytest.approx(0.5, abs=0.01)
        assert self._swing(1.0, NoroConfig()) == pytest.approx(
            self._swing(1.0, blend(0.5)), abs=1e-9
        )

    def test_both_directions_are_capped(self):
        config = NoroConfig()
        bid, ask, mid = 99.70, 100.30, 100.0
        upward = venue_price(
            venue("A", mid, best_bid=bid, best_ask=ask, microprice=ask), config
        )
        downward = venue_price(
            venue("A", mid, best_bid=bid, best_ask=ask, microprice=bid), config
        )
        cap = config.microprice_max_displacement_bps
        assert (upward - mid) / mid * 10_000 == pytest.approx(cap, abs=0.01)
        assert (downward - mid) / mid * 10_000 == pytest.approx(-cap, abs=0.01)

    @pytest.mark.parametrize("spread_bps", [0.1, 2.0, 60.0, 5_000.0])
    def test_the_result_stays_finite_and_positive(self, spread_bps):
        mid = 100.0
        half = mid * spread_bps / 20_000
        for micro in (mid - half, mid, mid + half):
            price = venue_price(
                venue("A", mid, best_bid=mid - half, best_ask=mid + half,
                      microprice=micro),
                NoroConfig(),
            )
            assert price is not None
            assert math.isfinite(price) and price > 0

    def test_the_cap_is_configurable(self):
        wide = self._swing(60.0, NoroConfig(microprice_max_displacement_bps=8.0))
        narrow = self._swing(60.0, NoroConfig(microprice_max_displacement_bps=0.5))
        assert wide == pytest.approx(16.0, abs=0.05)
        assert narrow == pytest.approx(1.0, abs=0.05)

    def test_a_zero_cap_pins_every_venue_to_its_mid(self):
        config = NoroConfig(microprice_max_displacement_bps=0.0)
        state = venue("A", 100.0, best_bid=99.7, best_ask=100.3, microprice=100.3)
        assert venue_price(state, config) == pytest.approx(100.0)

    def test_touch_imbalance_can_no_longer_reverse_the_vote_alone(self, config):
        """The audit's headline case, re-run against an independent anchor:
        same mids, same liquidity, same spreads, only the touch sizes differ.
        The capped displacement is far too small to cross the anchor."""
        bid_a, ask_a = 99.70, 100.30

        def states(micro_a):
            return [
                venue("A", 100.0, best_bid=bid_a, best_ask=ask_a,
                      microprice=micro_a, liquidity=100_000.0),
                venue("B", 100.30, liquidity=100_000.0),
                venue("C", 100.15, liquidity=100_000.0),
            ]

        neutral = opinion_for(states(100.0), config, "A", "B").signal
        adverse = opinion_for(states(ask_a), config, "A", "B").signal
        assert neutral > 0
        assert adverse > 0, (
            f"touch imbalance moved the vote from {neutral:+.3f} to "
            f"{adverse:+.3f} without reversing it"
        )


# ======================================================================
# P3-11 — configuration validation
# ======================================================================


class TestP3_11_ConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("liquidity_window_bps", 0.0),
            ("liquidity_window_bps", -1.0),
            ("microprice_weight", -0.01),
            ("microprice_weight", 1.01),
            ("microprice_max_displacement_bps", -0.1),
            ("ttl_ms", 0),
            ("ttl_ms", -1),
            ("saturation_bps", 0.0),
            ("saturation_bps", -5.0),
            ("reliability_saturation_notional", 0.0),
            ("reliability_saturation_notional", -1.0),
            ("dispersion_tolerance_bps", 0.0),
            ("dispersion_tolerance_bps", -1.0),
            ("insufficient_breadth_confidence", -0.01),
            ("insufficient_breadth_confidence", 1.01),
        ],
    )
    def test_out_of_range_values_are_refused(self, field, value):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            NoroConfig(**{field: value})

    @pytest.mark.parametrize(
        "field",
        [
            "saturation_bps",
            "microprice_weight",
            "microprice_max_displacement_bps",
            "reliability_saturation_notional",
            "dispersion_tolerance_bps",
            "insufficient_breadth_confidence",
        ],
    )
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_p3_11_no_non_finite_value_survives(self, field, value):
        """The finding, closed.

        ``Field(gt=0)`` rejects NaN and -inf but ACCEPTS +inf, because
        ``inf > 0`` is true -- so ``saturation_bps=inf`` used to construct
        happily and silence NORO on every opportunity while it went on
        reporting healthy and confident. Every float now carries
        ``allow_inf_nan=False``.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            NoroConfig(**{field: value})

    def test_an_off_bucket_window_is_refused(self):
        """P3-1's config half, asserted here too because it is a validation
        property. The full treatment is in ``test_noro_liquidity_window``."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            NoroConfig(liquidity_window_bps=9.999)

    @pytest.mark.parametrize(
        "weights",
        [
            (0.5, 0.3, 0.3),
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 1.0),
            (0.4, 0.3, 0.2),
            (0.4, 0.4, 0.4),
        ],
    )
    def test_confidence_weights_must_sum_to_one(self, weights):
        import pydantic

        breadth, agreement, quality = weights
        with pytest.raises(pydantic.ValidationError):
            NoroConfig(
                breadth_confidence_weight=breadth,
                agreement_confidence_weight=agreement,
                quality_confidence_weight=quality,
            )

    @pytest.mark.parametrize(
        "weights", [(0.4, 0.3, 0.3), (1.0, 0.0, 0.0), (0.0, 0.5, 0.5), (0.2, 0.4, 0.4)]
    )
    def test_confidence_weights_summing_to_one_are_accepted(self, weights):
        breadth, agreement, quality = weights
        config = NoroConfig(
            breadth_confidence_weight=breadth,
            agreement_confidence_weight=agreement,
            quality_confidence_weight=quality,
        )
        assert config.breadth_confidence_weight == breadth

    def test_the_defaults_are_valid_and_coherent(self):
        config = NoroConfig()
        assert config.liquidity_window_bps in PUBLISHED_BUCKETS
        assert (
            config.breadth_confidence_weight
            + config.agreement_confidence_weight
            + config.quality_confidence_weight
        ) == pytest.approx(1.0)
        assert config.ttl_ms > 0
        assert config.saturation_bps > 0

    def test_a_tiny_saturation_makes_everything_saturate(self):
        """Still reachable, and still worth recording: a near-zero saturation
        turns every non-zero confirmation into a maximal vote."""
        config = NoroConfig(saturation_bps=1e-12)
        for gap in (0.001, 0.01, 1.0, 100.0):
            states = [
                venue("A", 100.0 * (1 - gap / 10_000)),
                venue("B", 100.0 * (1 + gap / 10_000)),
                venue("C", 100.0),
            ]
            assert opinion_for(states, config, "A", "B").signal == pytest.approx(1.0)

    def test_a_huge_ttl_is_accepted(self):
        """An opinion that never expires would be permanently FRESH to
        consensus. Numerically valid; economically a stuck vote. Recorded as
        a remaining gap rather than a closed finding."""
        config = NoroConfig(ttl_ms=10**12)
        opinion = opinion_for(
            [venue("A", 100.0), venue("B", 100.2)], config, "A", "B"
        )
        assert opinion.expires_at - opinion.created_at == 10**12


# ======================================================================
# Section 49 — state growth
# ======================================================================


class TestStateGrowth:
    def test_fair_values_is_bounded_by_the_configured_symbols(self, config):
        symbols = [f"SYM{i}-USD" for i in range(5)]
        noro = build_noro(config, symbols=symbols)
        for tick in range(200):
            noro.on_market_state(
                market(
                    *[
                        venue(f"V{v}", 100.0 + tick * 0.001, symbol=symbol)
                        for symbol in symbols
                        for v in range(2)
                    ],
                    created_at=START_MS + tick,
                )
            )
        assert len(noro.fair_values) == len(symbols)

    def test_an_unconfigured_symbol_is_never_stored(self, config):
        noro = build_noro(config, symbols=[SYMBOL])
        noro.on_market_state(
            market(
                venue("A", 100.0, symbol=SYMBOL),
                venue("B", 100.2, symbol=SYMBOL),
                venue("A", 3_000.0, symbol="ETH-USD"),
                venue("B", 3_030.0, symbol="ETH-USD"),
            )
        )
        assert set(noro.fair_values) == {SYMBOL}, (
            "the loop is over settings.symbols, so unexpected symbols in the "
            "snapshot are ignored rather than accumulated"
        )

    def test_nothing_accumulates_per_evaluation(self, config):
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        from tests.audit.helpers import opportunity

        for i in range(500):
            noro.evaluate(opportunity("A", "B"), START_MS + i)
        assert noro.evaluations == 500, "a counter, not a collection"
        assert len(noro.fair_values) == 1
        attributes = {
            name: value
            for name, value in vars(noro).items()
            if isinstance(value, (list, dict, set))
        }
        assert set(attributes) == {"fair_values"}, (
            f"unexpected growable state: {sorted(attributes)}"
        )

    def test_the_market_reference_is_replaced_not_appended(self, config):
        noro = build_noro(config)
        for tick in range(50):
            noro.on_market_state(
                market(venue("A", 100.0), venue("B", 100.2),
                       created_at=START_MS + tick)
            )
        assert noro.market.created_at == START_MS + 49


# ======================================================================
# Section 51 — TTL and consensus freshness
# ======================================================================


class TestOpinionTtl:
    def test_the_default_ttl(self, config):
        assert config.ttl_ms == 2_000

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            (0, DataQuality.FRESH),
            (1_999, DataQuality.FRESH),
            (2_000, DataQuality.FRESH),
            (2_001, DataQuality.DEGRADED),
            (2_500, DataQuality.DEGRADED),
            (2_501, DataQuality.STALE),
        ],
    )
    def test_quality_at_each_boundary(self, config, offset, expected):
        """Combined with the consensus grace period (500ms), an opinion is
        usable for 2s, down-weighted for a further 0.5s, then excluded."""
        from core.clock import ManualClock
        from core.state import SystemState

        clock = ManualClock(START_MS)
        state = SystemState(clock=clock)
        noro = build_noro(config)
        noro.on_market_state(market(venue("A", 100.0), venue("B", 100.2)))
        from tests.audit.helpers import opportunity

        opp = opportunity("A", "B")
        opinion = noro.evaluate(opp, START_MS)
        state.put_opinion(opinion)
        # NORO's opinions are keyed by the OPPORTUNITY id, not the symbol:
        # ``AgentOpinion.subject`` is ``correlation_id or symbol``, and NORO
        # always sets the correlation id.
        assert opinion.subject == opp.opportunity_id
        slot = state.opinion(
            opp.opportunity_id,
            opinion.agent_id,
            degraded_grace_ms=500,
            now_ms=START_MS + offset,
        )
        assert slot.quality is expected

    def test_at_the_default_tick_cadence_the_opinion_covers_many_ticks(
        self, config
    ):
        """100ms ticks and a 2s TTL: an opinion stays FRESH for ~20 ticks, so
        NORO rarely goes stale under normal cadence."""
        assert config.ttl_ms / 100 == 20


# ======================================================================
# Section 52 — fair-value stability
# ======================================================================


class TestStability:
    """A smoothly changing market must produce a smoothly changing signal.

    Every sweep now carries an independent anchor, because a two-venue market
    produces the neutral verdict at every step and would look perfectly smooth
    while measuring nothing.
    """

    @staticmethod
    def _anchored(buy_bps: float, sell_bps: float, liquidity: float = 100_000.0):
        return [
            venue("A", 100.0 * (1 - buy_bps / 10_000), liquidity=liquidity),
            venue("B", 100.0 * (1 + sell_bps / 10_000), liquidity=liquidity),
            venue("C", 100.0, liquidity=liquidity),
        ]

    def test_a_slowly_widening_gap_produces_a_smooth_signal(self, config):
        signals = []
        for step in range(60):
            gap_bps = step * 0.1
            states = self._anchored(gap_bps, gap_bps)
            signals.append(opinion_for(states, config, "A", "B").signal)
        jumps = [abs(b - a) for a, b in itertools.pairwise(signals)]
        assert max(jumps) < 0.02, f"largest single-step jump {max(jumps):.4f}"

    def test_slowly_changing_liquidity_produces_a_smooth_confidence(self, config):
        """Depth no longer touches the signal at all -- it is a pure price
        comparison now -- so the property worth checking is that the
        confidence it does feed moves smoothly."""
        signals, confidences = [], []
        for step in range(60):
            liquidity = 1_000.0 * (1.08**step)
            opinion = opinion_for(
                self._anchored(10.0, 10.0, liquidity), config, "A", "B"
            )
            signals.append(opinion.signal)
            confidences.append(opinion.confidence)
        assert len(set(signals)) == 1, "prices did not move, so the signal must not"
        jumps = [abs(b - a) for a, b in itertools.pairwise(confidences)]
        assert max(jumps) < 0.02, f"largest single-step jump {max(jumps):.4f}"

    def test_a_slowly_shifting_microprice_produces_a_smooth_signal(self, config):
        signals = []
        bid, ask = 99.9, 100.1
        for step in range(41):
            micro = bid + (ask - bid) * step / 40
            states = [
                venue("A", 100.0, best_bid=bid, best_ask=ask, microprice=micro,
                      liquidity=100_000.0),
                venue("B", 100.15, liquidity=100_000.0),
                venue("C", 100.05, liquidity=100_000.0),
            ]
            signals.append(opinion_for(states, config, "A", "B").signal)
        jumps = [abs(b - a) for a, b in itertools.pairwise(signals)]
        assert max(jumps) < 0.05
        assert len(set(signals)) > 1, "the sweep did move the signal"

    def test_p3_1_the_configuration_discontinuity_is_unreachable(self):
        """The audit's sharpest stability finding: sweeping the WINDOW rather
        than the market produced a spike -- 9.999 and 10.001 both fell back to
        whole-book depth while 10.0 hit the bucket, so a 0.001 bps
        configuration step moved the signal roughly a hundred times more per
        bps than the market sweeps above did.

        There is no longer a window between the buckets to sweep. The
        configurable domain IS the measured domain.
        """
        import pydantic

        for window in (9.999, 10.001, 24.999, 25.001, 0.999):
            with pytest.raises(pydantic.ValidationError):
                NoroConfig(liquidity_window_bps=window)

        assert set(PUBLISHED_BUCKETS) == {
            NoroConfig(liquidity_window_bps=b).liquidity_window_bps
            for b in PUBLISHED_BUCKETS
        }

    def test_the_measured_windows_move_the_signal_only_through_the_market(
        self, config
    ):
        """Changing the window between two measured buckets on a venue whose
        depth is flat across them changes nothing at all."""
        states = self._anchored(10.0, 10.0)
        signals = {
            window: opinion_for(
                states, NoroConfig(liquidity_window_bps=window), "A", "B"
            ).signal
            for window in PUBLISHED_BUCKETS
        }
        assert len(set(signals.values())) == 1, signals
