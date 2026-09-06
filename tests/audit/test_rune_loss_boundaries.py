"""Phase 5 — §22, §23, §39: loss and drawdown boundaries, and whether RUNE and
the kill switch agree about where a limit is breached.

Two components enforce the same two numbers:

* ``gate_daily_loss`` / ``gate_drawdown`` decide whether a NEW trade may open.
* ``_daily_loss`` / ``_drawdown`` in the kill switch decide whether the whole
  platform stops.

A difference in comparison operator is not automatically a defect — one may be
deliberately stricter. It becomes a finding when the two disagree about the
same boundary in a way that leaves a state where the entry gate permits a trade
that the kill switch simultaneously considers a breach, or vice versa.
"""

from __future__ import annotations

import pytest

from core.config import RiskLimits, Settings, load_settings, simulated_venues
from core.models.risk import GateResult, RiskVerdict
from risk.kill_switch import KillSwitchInputs
from risk.kill_switch import _daily_loss as kill_daily_loss
from risk.kill_switch import _drawdown as kill_drawdown
from tests.audit.rune_fixtures import (
    blocking_names,
    context,
    core,
    gate_named,
    intent,
    portfolio,
)
from tests.conftest import START_MS

LIMITS = RiskLimits()
MAX_LOSS = LIMITS.max_daily_loss   # 2,500
MAX_DD = LIMITS.max_drawdown       # 5,000
EPS = 0.01


def settings_for(limits: RiskLimits) -> Settings:
    return load_settings().model_copy(
        update={"venues": simulated_venues(), "risk": limits}
    )


class TestDailyLossGate:
    """``loss < max_daily_loss`` — strict, so exactly at the limit blocks."""

    @pytest.mark.parametrize(
        ("day_pnl", "expected_pass"),
        [
            (0.0, True),
            (1_000.0, True),           # a profitable day
            (-(MAX_LOSS - EPS), True),
            (-MAX_LOSS, False),        # exactly at the limit
            (-(MAX_LOSS + EPS), False),
            (-1_000_000.0, False),
        ],
    )
    def test_the_boundary(self, day_pnl, expected_pass):
        decision = core(LIMITS).evaluate(
            intent(), context(portfolio=portfolio(day_realized_pnl=day_pnl)), START_MS
        )
        check = gate_named(decision, "MAX_DAILY_LOSS")
        assert (check.result is GateResult.PASS) is expected_pass

    def test_a_profitable_day_reports_zero_loss(self):
        decision = core(LIMITS).evaluate(
            intent(), context(portfolio=portfolio(day_realized_pnl=5_000.0)), START_MS
        )
        assert gate_named(decision, "MAX_DAILY_LOSS").observed == pytest.approx(0.0)

    def test_the_daily_loss_gate_measures_pre_fee_realised_pnl(self):
        """``PaperAccount`` accumulates ``day_realized_pnl`` from
        ``PositionState.apply``'s return value, which is pre-fee, and fees are
        tracked separately. Recorded so the report can state the unit rather
        than assume it."""
        import inspect

        from core.models.portfolio import PositionState

        source = inspect.getsource(PositionState.apply)
        assert "self.fees_paid += fee" in source
        assert "realized = (price - self.average_entry_price) * closing * direction" in (
            source
        )
        book = portfolio(day_realized_pnl=-100.0, fees_paid=500.0)
        decision = core(LIMITS).evaluate(intent(), context(portfolio=book), START_MS)
        assert gate_named(decision, "MAX_DAILY_LOSS").observed == pytest.approx(100.0), (
            "the daily-loss gate measures pre-fee realised P&L; fees paid "
            "during the day do not count toward the loss limit"
        )


class TestDrawdownGate:
    """``drawdown < max_drawdown`` — also strict."""

    @staticmethod
    def _book(drawdown: float):
        # peak 100,000; equity is cash because there are no positions.
        return portfolio(cash=100_000.0 - drawdown, peak_equity=100_000.0)

    @pytest.mark.parametrize(
        ("drawdown", "expected_pass"),
        [
            (0.0, True),
            (MAX_DD - EPS, True),
            (MAX_DD, False),
            (MAX_DD + EPS, False),
        ],
    )
    def test_the_boundary(self, drawdown, expected_pass):
        book = self._book(drawdown)
        assert book.drawdown == pytest.approx(drawdown)
        decision = core(LIMITS).evaluate(intent(), context(portfolio=book), START_MS)
        assert (
            gate_named(decision, "MAX_DRAWDOWN").result is GateResult.PASS
        ) is expected_pass

    def test_equity_above_peak_reports_no_drawdown(self):
        book = portfolio(cash=150_000.0, peak_equity=100_000.0)
        assert book.drawdown == pytest.approx(0.0)
        decision = core(LIMITS).evaluate(intent(), context(portfolio=book), START_MS)
        assert not gate_named(decision, "MAX_DRAWDOWN").blocking


class TestRuneAndKillSwitchAgree:
    """§22/§23 — the two enforcement points must not disagree."""

    @pytest.mark.parametrize(
        "day_pnl",
        [0.0, -(MAX_LOSS - EPS), -MAX_LOSS, -(MAX_LOSS + EPS), -100_000.0],
    )
    def test_daily_loss_breach_is_the_same_state_for_both(self, day_pnl):
        book = portfolio(day_realized_pnl=day_pnl)
        settings = settings_for(LIMITS)
        gate_blocks = "MAX_DAILY_LOSS" in blocking_names(
            core(LIMITS).evaluate(intent(), context(portfolio=book), START_MS)
        )
        switch_fires = kill_daily_loss(
            KillSwitchInputs(portfolio=book, health=None), settings
        )
        assert gate_blocks is switch_fires, (
            f"day_realized_pnl={day_pnl}: entry gate blocks={gate_blocks} but "
            f"kill switch fires={switch_fires} against a {MAX_LOSS} limit"
        )

    @pytest.mark.parametrize(
        "drawdown", [0.0, MAX_DD - EPS, MAX_DD, MAX_DD + EPS, 50_000.0]
    )
    def test_drawdown_breach_is_the_same_state_for_both(self, drawdown):
        book = portfolio(cash=100_000.0 - drawdown, peak_equity=100_000.0)
        settings = settings_for(LIMITS)
        gate_blocks = "MAX_DRAWDOWN" in blocking_names(
            core(LIMITS).evaluate(intent(), context(portfolio=book), START_MS)
        )
        switch_fires = kill_drawdown(
            KillSwitchInputs(portfolio=book, health=None), settings
        )
        assert gate_blocks is switch_fires, (
            f"drawdown={drawdown}: entry gate blocks={gate_blocks} but kill "
            f"switch fires={switch_fires} against a {MAX_DD} limit"
        )


class TestDrawdownDependsOnMarks:
    """§23 — an understated drawdown would authorise a trade it should not.

    ``PositionState.notional`` falls back to the average entry price when
    ``mark_price`` is ``None``, so an unmarked position is valued at cost.
    Equity therefore ignores an adverse move, and drawdown is understated.
    """

    def test_an_unmarked_position_is_valued_at_cost(self):
        from core.models.portfolio import PositionState

        unmarked = PositionState(
            venue="VENUE_A", symbol="BTC-USD", quantity=100.0,
            average_entry_price=100.0,
        )
        assert unmarked.mark_price is None
        assert unmarked.notional == pytest.approx(10_000.0)
        assert unmarked.signed_notional == pytest.approx(10_000.0)
        assert unmarked.unrealized_pnl == pytest.approx(0.0)

    def test_an_adverse_move_only_shows_once_marked(self):
        from core.models.portfolio import PositionState

        marked = PositionState(
            venue="VENUE_A", symbol="BTC-USD", quantity=100.0,
            average_entry_price=100.0, mark_price=50.0,
        )
        assert marked.unrealized_pnl == pytest.approx(-5_000.0)
        assert marked.signed_notional == pytest.approx(5_000.0)

    def test_the_freshness_gates_are_what_stand_between_stale_marks_and_a_trade(self):
        """Recorded scope: RUNE has no gate on mark freshness itself. What
        prevents trading on a stale valuation is the market-data and health
        gates, which fail closed when the feed those marks come from stops."""
        decision = core(LIMITS).evaluate(
            intent(source_data_timestamp=START_MS - 10_000), context(), START_MS
        )
        assert "MARKET_DATA_FRESH" in blocking_names(decision)

        names = {g.name for g in decision.gates}
        assert not [n for n in names if "MARK" in n and n != "MARKET_DATA_FRESH"], (
            "there is no dedicated mark-freshness gate; drawdown correctness "
            "rests on the marking loop and on the data-age gate"
        )


class TestNumericSafety:
    """§38 — no gate may pass because a comparison with a strange value
    happened to be true."""

    #: Every ``gt=0`` float limit on ``RiskLimits``.
    NOTIONAL_LIMITS = [
        "max_position_notional",
        "max_gross_exposure",
        "max_net_exposure",
        "max_leverage",
        "max_daily_loss",
        "max_drawdown",
        "max_venue_exposure",
        "max_strategy_exposure",
        "max_order_notional",
        "min_trade_notional",
        "max_unhedged_notional",
    ]

    @pytest.mark.parametrize("field", NOTIONAL_LIMITS)
    def test_every_notional_limit_must_be_positive(self, field):
        import pydantic

        for bad in (0.0, -1.0, float("nan"), float("-inf")):
            with pytest.raises(pydantic.ValidationError):
                RiskLimits(**{field: bad})

    def test_an_infinite_limit_is_not_a_limit(self):
        """``gt=0`` admits ``+inf``, and every comparison against an infinite
        ceiling passes. A limit that can never be breached is configuration
        that silently disables a hard gate.

        ``allow_inf_nan=False`` is already used elsewhere in this codebase's
        config (``TidalConfig``, ``NoroConfig``), so the convention exists.
        """
        import math

        import pydantic

        offenders = []
        for field in self.NOTIONAL_LIMITS:
            try:
                limits = RiskLimits(**{field: float("inf")})
            except pydantic.ValidationError:
                continue
            if math.isinf(getattr(limits, field)):
                offenders.append(field)
        assert not offenders, (
            "RiskLimits accepts an infinite value for: "
            f"{offenders} — each one disables the gate that reads it"
        )

    #: Every float field on ``RiskLimits``, enumerated from the model rather
    #: than by hand so a field added later cannot quietly escape the rule.
    FLOAT_LIMITS = sorted(
        name
        for name, info in RiskLimits.model_fields.items()
        if info.annotation is float
    )

    def test_the_enumeration_covers_the_known_float_limits(self):
        """A guard on the guard: if the annotation scan ever returns nothing,
        every parametrised case below would vacuously pass."""
        assert set(self.FLOAT_LIMITS) == {
            "max_position_notional",
            "max_gross_exposure",
            "max_net_exposure",
            "max_leverage",
            "max_daily_loss",
            "max_drawdown",
            "max_venue_exposure",
            "max_strategy_exposure",
            "max_order_notional",
            "min_trade_notional",
            "max_unhedged_notional",
            "min_expected_edge_bps",
            "max_error_rate",
        }

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_no_float_limit_may_be_non_finite(self, bad):
        """P5-14. ``x <= inf`` is true for every finite x, so an infinite
        ceiling is a gate that can never fire while still reporting itself as
        checked; every comparison against NaN is false, so a NaN ceiling is a
        gate that can never be satisfied. Neither is a limit."""
        import pydantic

        offenders = []
        for field in self.FLOAT_LIMITS:
            try:
                RiskLimits(**{field: bad})
            except pydantic.ValidationError:
                continue
            offenders.append(field)
        assert not offenders, (
            f"RiskLimits accepts {bad} for: {offenders} — each one breaks the "
            "gate that reads it"
        )

    def test_the_defaults_are_unchanged_by_the_finiteness_rule(self):
        """Rejecting infinities must not have moved any number."""
        defaults = RiskLimits()
        assert defaults.max_position_notional == pytest.approx(50_000.0)
        assert defaults.max_gross_exposure == pytest.approx(150_000.0)
        assert defaults.max_net_exposure == pytest.approx(25_000.0)
        assert defaults.max_leverage == pytest.approx(2.0)
        assert defaults.max_daily_loss == pytest.approx(2_500.0)
        assert defaults.max_drawdown == pytest.approx(5_000.0)
        assert defaults.max_venue_exposure == pytest.approx(75_000.0)
        assert defaults.max_strategy_exposure == pytest.approx(100_000.0)
        assert defaults.max_order_notional == pytest.approx(25_000.0)
        assert defaults.min_trade_notional == pytest.approx(250.0)
        assert defaults.max_unhedged_notional == pytest.approx(10_000.0)
        assert defaults.min_expected_edge_bps == pytest.approx(2.0)
        assert defaults.max_error_rate == pytest.approx(0.25)
        assert defaults.max_data_age_ms == 2_000
        assert defaults.max_clock_skew_ms == 2_000
        assert defaults.max_open_orders == 20

    def test_a_nan_expected_edge_cannot_pass_the_edge_gate(self):
        """``nan >= floor`` is False, so NaN blocks. Pinned because the
        opposite convention would silently authorise an unpriceable trade."""
        decision = core(LIMITS).evaluate(
            intent(expected_net_edge_bps=float("nan")), context(), START_MS
        )
        assert "MIN_EXPECTED_EDGE" in blocking_names(decision)

    def test_a_nan_consensus_agreement_cannot_pass(self):
        decision = core(LIMITS).evaluate(
            intent(consensus_agreement=float("nan")), context(), START_MS
        )
        assert "CONSENSUS_THRESHOLD" in blocking_names(decision)

    def test_a_nan_error_rate_cannot_pass(self):
        decision = core(LIMITS).evaluate(
            intent(), context(error_rate=float("nan")), START_MS
        )
        assert "MAX_ERROR_RATE" in blocking_names(decision), (
            "a NaN error rate is an unknown error rate; it must not be treated "
            "as an acceptable one"
        )

    def test_a_nan_unhedged_notional_cannot_pass(self):
        """An unknown residual is not an acceptable one.

        Since MAX_UNHEDGED_EXPOSURE gained a headroom solver (P5-18) the
        rejection arrives one step earlier: every comparison against NaN is
        false and ``max(0.0, nan)`` is ``0.0``, so the headroom candidate is
        zero and ``evaluate`` short-circuits. Fail-closed either way — what
        must never happen is a NaN residual authorising a trade.
        """
        from risk import limits as gates

        decision = core(LIMITS).evaluate(
            intent(), context(unhedged_notional=float("nan")), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == pytest.approx(0.0)
        assert gates.unhedged_headroom(intent(), float("nan"), LIMITS) == 0.0
        assert gates.gate_unhedged(intent(), float("nan"), LIMITS).blocking

    def test_an_infinite_economical_ceiling_does_not_grow_the_trade(self):
        decision = core(LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=float("inf")),
            START_MS,
        )
        assert decision.approved_notional <= 5_000.0 + 1e-9

    def test_a_nan_economical_ceiling_does_not_authorise_a_trade(self):
        """``min()`` over a list containing NaN is order-dependent and can
        return NaN. A NaN headroom must not become an approved size.

        This currently holds only because the ceiling is appended LAST and
        ``min`` keeps its running value when every comparison is false. Append
        order is not a safety property, so this test pins the outcome rather
        than the mechanism.
        """
        decision = core(LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=float("nan")),
            START_MS,
        )
        import math

        assert not math.isnan(decision.approved_notional), (
            "approved notional is NaN; the sizing path admitted a "
            "non-comparable ceiling"
        )


class TestConfigCoherence:
    """§37 — configurations that make the documented system impossible."""

    def test_min_trade_above_max_order_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(min_trade_notional=30_000.0, max_order_notional=25_000.0)

    def test_max_order_above_max_position_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_order_notional=60_000.0, max_position_notional=50_000.0)

    def test_max_position_above_max_gross_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_position_notional=200_000.0, max_gross_exposure=150_000.0)

    def test_an_order_larger_than_the_venue_limit_is_refused(self):
        """A single permitted order that cannot fit on any one venue means
        every trade is silently reduced or rejected, and nothing warned.

        The same shape the validator already rejected for position/gross, left
        unchecked for venue until P5-15. Every order executes on exactly one
        venue, so the two caps are directly comparable.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="max_venue_exposure"):
            RiskLimits(
                max_order_notional=25_000.0,
                max_venue_exposure=10_000.0,
                max_unhedged_notional=1_000_000.0,
            )

    def test_an_order_exactly_at_the_venue_limit_is_allowed(self):
        """Equality is coherent: one order may fill the venue's whole budget."""
        limits = RiskLimits(max_order_notional=25_000.0, max_venue_exposure=25_000.0)
        assert limits.max_order_notional == pytest.approx(limits.max_venue_exposure)

    def test_one_unit_above_the_venue_limit_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_order_notional=25_001.0, max_venue_exposure=25_000.0)

    def test_a_two_leg_strategy_cannot_use_its_whole_strategy_budget(self):
        """``max_strategy_exposure`` is consumed at ``notional * legs``, so the
        largest authorisable per-leg size is half the budget for a two-leg
        strategy. Recorded so the interaction is explicit rather than
        surprising."""
        limits = RiskLimits(
            max_strategy_exposure=20_000.0,
            max_order_notional=25_000.0,
            max_position_notional=50_000.0,
            # Opened so the strategy budget is what binds (P5-18).
            max_unhedged_notional=1_000_000.0,
        )
        decision = core(limits).evaluate(intent(notional=25_000.0), context(), START_MS)
        assert decision.approved_notional == pytest.approx(10_000.0)

    def test_a_strategy_budget_below_two_minimum_trades_is_refused_at_the_root(self):
        """Every trade would reject at MIN_TRADE_NOTIONAL and the platform
        would look merely quiet.

        The rule lives on ``Settings``, not ``RiskLimits``: it depends on the
        shipped strategy opening ``SHIPPED_ENTRY_LEGS`` legs at once, which is
        a fact about the strategy rather than about risk limits in general. A
        one-leg strategy would find a two-leg floor arbitrary, so ``RiskLimits``
        still accepts the same numbers on its own.
        """
        import pydantic

        from core.config.settings import SHIPPED_ENTRY_LEGS

        assert RiskLimits(max_strategy_exposure=300.0, min_trade_notional=250.0)
        with pytest.raises(pydantic.ValidationError, match="max_strategy_exposure"):
            load_settings(risk={"max_strategy_exposure": 300.0, "min_trade_notional": 250.0})
        assert SHIPPED_ENTRY_LEGS == 2

    def test_a_strategy_budget_of_exactly_two_minimum_trades_is_allowed(self):
        settings = load_settings(
            risk={"max_strategy_exposure": 500.0, "min_trade_notional": 250.0}
        )
        assert settings.risk.max_strategy_exposure == pytest.approx(500.0)

    def test_one_unit_below_two_minimum_trades_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            load_settings(risk={"max_strategy_exposure": 499.0, "min_trade_notional": 250.0})

    def test_a_recovery_reserve_that_consumes_the_unhedged_budget_is_refused(self):
        """``max_unhedged_notional - hedge_tolerance_notional`` is the budget an
        entry may actually use (P5-18). Below ``min_trade_notional`` no entry
        could ever be sized, and nothing said so."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="hedge_tolerance_notional"):
            load_settings(
                risk={"max_unhedged_notional": 700.0, "min_trade_notional": 250.0},
                hedge_tolerance_notional=500.0,
            )

    def test_a_recovery_reserve_leaving_exactly_the_minimum_is_allowed(self):
        settings = load_settings(
            risk={"max_unhedged_notional": 750.0, "min_trade_notional": 250.0},
            hedge_tolerance_notional=500.0,
        )
        assert (
            settings.risk.max_unhedged_notional - settings.hedge_tolerance_notional
        ) == pytest.approx(settings.risk.min_trade_notional)

    def test_one_unit_below_the_minimum_recovery_budget_is_refused(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            load_settings(
                risk={"max_unhedged_notional": 749.0, "min_trade_notional": 250.0},
                hedge_tolerance_notional=500.0,
            )

    @pytest.mark.parametrize("rate", [-0.01, 1.01])
    def test_error_rate_outside_zero_to_one_is_refused(self, rate):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_error_rate=rate)

    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_time_windows_are_refused(self, value):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_data_age_ms=value)
        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_clock_skew_ms=value)

    def test_max_open_orders_must_be_positive(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            RiskLimits(max_open_orders=0)
