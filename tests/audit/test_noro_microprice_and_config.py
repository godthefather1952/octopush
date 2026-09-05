"""Phase 3 audit, Sections 6 / 28 / 49-52: the price blend, config, and state.

* **Microprice (6, 28)** -- ``venue_price`` blends mid and microprice. How
  much can that blend move NORO, and which book feature controls it?
* **Config (50)** -- what ``NoroConfig`` accepts that is numerically valid and
  semantically meaningless.
* **State (49)** -- does anything accumulate across snapshots?
* **TTL (51)** -- how NORO's opinion lifetime interacts with consensus.
* **Stability (52)** -- does a smoothly changing market produce a smoothly
  changing signal?
"""

from __future__ import annotations

import itertools
import math

import pytest

from agents.noro.fair_value import venue_price
from core.config import NoroConfig
from core.models.common import DataQuality
from tests.audit.helpers import START_MS, SYMBOL, cliff_venue, market, venue
from tests.audit.noro_fixtures import build_noro, opinion_for


@pytest.fixture
def config() -> NoroConfig:
    return NoroConfig()


# ======================================================================
# Section 6 — microprice semantics
# ======================================================================


class TestVenuePriceBlend:
    def test_weight_zero_is_the_mid(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, 0.0) == pytest.approx(100.0)

    def test_weight_one_is_the_microprice(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, 1.0) == pytest.approx(100.5)

    def test_weight_half_is_the_arithmetic_midpoint(self):
        state = venue("A", 100.0, microprice=100.5)
        assert venue_price(state, 0.5) == pytest.approx(100.25)

    @pytest.mark.parametrize("weight", [-5.0, -0.1, 1.1, 99.0])
    def test_the_weight_is_clamped_to_zero_one(self, weight):
        """``min(1, max(0, w))`` -- so an out-of-range weight degrades to an
        endpoint rather than extrapolating outside the touch."""
        state = venue("A", 100.0, microprice=100.5)
        price = venue_price(state, weight)
        assert 100.0 <= price <= 100.5

    def test_monotone_upward_when_the_microprice_is_above_the_mid(self):
        state = venue("A", 100.0, microprice=100.5)
        previous = -math.inf
        for weight in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            price = venue_price(state, weight)
            assert price >= previous
            previous = price

    def test_monotone_downward_when_the_microprice_is_below_the_mid(self):
        state = venue("A", 100.0, microprice=99.5)
        previous = math.inf
        for weight in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
            price = venue_price(state, weight)
            assert price <= previous
            previous = price

    def test_a_missing_microprice_falls_back_to_the_mid(self):
        state = venue("A", 100.0)
        state.metrics.microprice = None
        assert venue_price(state, 1.0) == pytest.approx(100.0)

    def test_a_missing_mid_makes_the_venue_unpriceable(self):
        state = venue("A", 100.0, microprice=100.5)
        state.metrics.mid = None
        assert venue_price(state, 0.5) is None, (
            "no mid means no price, even though a microprice is present"
        )


class TestHowMuchTheMicropriceCanMoveNoro:
    """Section 28: which book feature actually controls NORO?"""

    def test_a_touch_imbalance_moves_the_venue_price_by_up_to_half_the_spread(
        self,
    ):
        """The bound: microprice lies in [bid, ask], so at weight 0.5 the
        blend lies in [mid - spread/4, mid + spread/4]."""
        bid, ask = 99.90, 100.10
        mid = (bid + ask) / 2
        extremes = []
        for micro in (bid, ask):
            state = venue("A", mid, best_bid=bid, best_ask=ask, microprice=micro)
            extremes.append(venue_price(state, 0.5))
        assert extremes[0] == pytest.approx(mid - 0.05)
        assert extremes[1] == pytest.approx(mid + 0.05)
        assert extremes[1] - extremes[0] == pytest.approx((ask - bid) / 2)

    def test_on_a_wide_spread_that_is_a_large_move_in_bps(self):
        """A 60 bps spread gives the microprice 30 bps of control over the
        venue price at the default weight -- twice the whole saturation
        band."""
        bid, ask = 99.70, 100.30
        mid = (bid + ask) / 2
        low = venue_price(
            venue("A", mid, best_bid=bid, best_ask=ask, microprice=bid), 0.5
        )
        high = venue_price(
            venue("A", mid, best_bid=bid, best_ask=ask, microprice=ask), 0.5
        )
        swing_bps = (high - low) / mid * 10_000
        assert swing_bps == pytest.approx(30.0, abs=0.1)
        assert swing_bps > 2 * NoroConfig().saturation_bps

    def test_and_it_can_reverse_the_signal_on_its_own(self, config):
        """Same mids, same liquidity, same spreads -- only the touch sizes
        differ, and the vote flips."""
        bid_a, ask_a = 99.70, 100.30
        bid_b, ask_b = 99.90, 100.50

        def pair(micro_a, micro_b):
            return [
                venue("A", (bid_a + ask_a) / 2, best_bid=bid_a, best_ask=ask_a,
                      microprice=micro_a, liquidity=100_000.0),
                venue("B", (bid_b + ask_b) / 2, best_bid=bid_b, best_ask=ask_b,
                      microprice=micro_b, liquidity=100_000.0),
            ]

        neutral = opinion_for(pair(100.00, 100.20), config, "A", "B").signal
        adverse = opinion_for(pair(ask_a, bid_b), config, "A", "B").signal
        assert neutral > 0
        assert adverse < 0, (
            f"touch imbalance alone flipped {neutral:+.3f} to {adverse:+.3f}"
        )

    def test_the_weight_controls_how_much_authority_it_has(self, config):
        bid_a, ask_a = 99.70, 100.30
        bid_b, ask_b = 99.90, 100.50
        states = [
            venue("A", (bid_a + ask_a) / 2, best_bid=bid_a, best_ask=ask_a,
                  microprice=ask_a, liquidity=100_000.0),
            venue("B", (bid_b + ask_b) / 2, best_bid=bid_b, best_ask=ask_b,
                  microprice=bid_b, liquidity=100_000.0),
        ]
        signals = {
            weight: opinion_for(
                states, NoroConfig(microprice_weight=weight), "A", "B"
            ).signal
            for weight in (0.0, 0.25, 0.5, 0.75, 1.0)
        }
        assert signals[0.0] > 0, "on mids alone the trade is confirmed"
        assert signals[1.0] < 0, "on micropricess alone it is rejected"
        assert list(signals.values()) == sorted(signals.values(), reverse=True)

    def test_which_book_feature_dominates_depends_on_the_spread(self):
        """Summary of the sensitivity: on a tight book the microprice can
        barely move the price; on a wide one it dominates."""
        for spread_bps, expected in ((1.0, 0.5), (60.0, 30.0)):
            mid = 100.0
            half = mid * spread_bps / 20_000
            low = venue_price(
                venue("A", mid, best_bid=mid - half, best_ask=mid + half,
                      microprice=mid - half), 0.5
            )
            high = venue_price(
                venue("A", mid, best_bid=mid - half, best_ask=mid + half,
                      microprice=mid + half), 0.5
            )
            assert (high - low) / mid * 10_000 == pytest.approx(expected, abs=0.05)


# ======================================================================
# Section 50 — configuration validation
# ======================================================================


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("liquidity_window_bps", 0.0),
            ("liquidity_window_bps", -1.0),
            ("microprice_weight", -0.01),
            ("microprice_weight", 1.01),
            ("ttl_ms", 0),
            ("ttl_ms", -1),
            ("saturation_bps", 0.0),
            ("saturation_bps", -5.0),
        ],
    )
    def test_out_of_range_values_are_refused(self, field, value):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            NoroConfig(**{field: value})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values(self, value):
        """Pydantic's ``gt=0`` rejects NaN and -inf; +inf passes the bound.
        Recorded as measured, not assumed."""
        import pydantic

        try:
            config = NoroConfig(saturation_bps=value)
        except pydantic.ValidationError:
            return
        assert math.isinf(config.saturation_bps), (
            "only +inf survives the numeric bound"
        )

    def test_an_infinite_saturation_silences_noro_entirely(self):
        """Numerically valid, semantically catastrophic: every signal becomes
        zero, so NORO votes neutral on everything while still reporting
        healthy and confident."""
        config = NoroConfig(saturation_bps=float("inf"))
        opinion = opinion_for(
            [venue("A", 100.0), venue("B", 101.0)], config, "A", "B"
        )
        assert opinion.signal == pytest.approx(0.0)
        assert opinion.confidence > 0.5
        assert "FAIR_VALUE_CONTRADICTS_DISLOCATION" in opinion.reason_codes

    def test_a_tiny_saturation_makes_everything_saturate(self):
        """The mirror case: every non-zero dislocation becomes a maximal
        vote, so NORO stops discriminating at all."""
        config = NoroConfig(saturation_bps=1e-12)
        for gap in (0.001, 0.01, 1.0, 100.0):
            states = [
                venue("A", 100.0),
                venue("B", 100.0 * (1 + gap / 10_000)),
            ]
            assert opinion_for(states, config, "A", "B").signal == pytest.approx(1.0)

    def test_an_off_bucket_window_is_accepted_without_comment(self):
        """Ties to P3-1: 9.999 is numerically valid and can never hit a
        measured bucket."""
        assert NoroConfig(liquidity_window_bps=9.999).liquidity_window_bps == 9.999

    def test_a_huge_ttl_is_accepted(self):
        """An opinion that never expires would be permanently FRESH to
        consensus. Numerically valid; economically a stuck vote."""
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
    def test_a_slowly_widening_gap_produces_a_smooth_signal(self, config):
        signals = []
        for step in range(60):
            gap_bps = step * 0.1
            states = [
                venue("A", 100.0, liquidity=100_000.0),
                venue("B", 100.0 * (1 + gap_bps / 10_000), liquidity=100_000.0),
            ]
            signals.append(opinion_for(states, config, "A", "B").signal)
        jumps = [abs(b - a) for a, b in itertools.pairwise(signals)]
        assert max(jumps) < 0.02, f"largest single-step jump {max(jumps):.4f}"

    def test_slowly_changing_liquidity_produces_a_smooth_signal(self, config):
        signals = []
        for step in range(60):
            liquidity = 100_000.0 * (1.01**step)
            states = [
                venue("A", 100.0, liquidity=liquidity),
                venue("B", 100.1, liquidity=100_000.0),
            ]
            signals.append(opinion_for(states, config, "A", "B").signal)
        jumps = [abs(b - a) for a, b in itertools.pairwise(signals)]
        assert max(jumps) < 0.01

    def test_a_slowly_shifting_microprice_produces_a_smooth_signal(self, config):
        signals = []
        bid, ask = 99.9, 100.1
        for step in range(41):
            micro = bid + (ask - bid) * step / 40
            states = [
                venue("A", 100.0, best_bid=bid, best_ask=ask, microprice=micro,
                      liquidity=100_000.0),
                venue("B", 100.15, liquidity=100_000.0),
            ]
            signals.append(opinion_for(states, config, "A", "B").signal)
        jumps = [abs(b - a) for a, b in itertools.pairwise(signals)]
        assert max(jumps) < 0.05

    def test_the_bucket_fallback_is_the_discontinuity_this_suite_finds(
        self, config
    ):
        """Sweeping the WINDOW rather than the market.

        The signal jump itself is modest (~0.020), because ``min + mean``
        damps it -- but it is produced by a 0.001 bps step in CONFIGURATION
        while the market does not move at all. Per bps of input change that
        is ~100x the sensitivity of the market sweeps above, and the
        confidence jump alongside it is far larger.
        """
        states = [
            cliff_venue("A", 100.0, near_notional=2_000.0, far_notional=900_000.0),
            venue("B", 100.2, liquidity=50_000.0),
        ]
        opinions = [
            opinion_for(states, NoroConfig(liquidity_window_bps=window), "A", "B")
            for window in (9.999, 10.0, 10.001)
        ]
        signals = [o.signal for o in opinions]
        confidences = [o.confidence for o in opinions]

        # 9.999 and 10.001 both miss the bucket and use the full book; 10.0
        # hits it. So the middle point is the outlier -- a spike, not a step.
        assert signals[0] == pytest.approx(signals[2]), (
            "both off-bucket windows fall back to the same full-book depth"
        )
        assert abs(signals[1] - signals[0]) == pytest.approx(0.020, abs=0.002)
        assert abs(confidences[1] - confidences[0]) == pytest.approx(0.356, abs=0.01)

        # Sensitivity per bps of input change, against the market sweeps above.
        config_sensitivity = abs(signals[1] - signals[0]) / 0.001
        market_sensitivity = 0.02 / 0.1  # largest jump from the gap sweep
        assert config_sensitivity > 50 * market_sensitivity, (
            f"{config_sensitivity:.1f} signal/bps from configuration vs "
            f"{market_sensitivity:.1f} from the market"
        )
