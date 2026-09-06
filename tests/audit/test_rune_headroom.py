"""Phase 5 — H1: does ``_headroom`` include every reducible size constraint?

``RuneCore.evaluate`` documents its own contract:

    "Size the intent to fit every limit, then gate the sized intent. ... A
    size-based gate can therefore only fail if the reduction could not make it
    pass, which is exactly when rejection is the right answer."

That is a strong claim, and it is testable. For every gate whose outcome
depends on ``intent.notional``, there must be no configuration in which a
smaller notional would have passed and RUNE rejected outright instead.

``_headroom`` currently considers: the requested notional, MAX_ORDER_NOTIONAL,
MAX_GROSS_EXPOSURE, MAX_STRATEGY_EXPOSURE, MAX_VENUE_EXPOSURE,
MAX_POSITION_NOTIONAL and ZEPHR's economical ceiling. The size-sensitive gates
are those seven plus MAX_NET_EXPOSURE and MAX_LEVERAGE.

The tests below state the contract as the assertion. A rejection where a
smaller size would have been authorised is a violation of the documented
sizing contract, not merely a conservative choice: it is the difference
between "this trade is too big" and "this trade is impossible".
"""

from __future__ import annotations

import inspect

import pytest

from agents.rune.core import RuneCore
from core.config import RiskLimits
from core.models.common import Side
from core.models.risk import RiskVerdict
from tests.audit.rune_fixtures import (
    VENUE_A,
    VENUE_B,
    blocking_names,
    context,
    core,
    gate_named,
    intent,
    leg,
    portfolio,
    portfolio_with,
    position,
)
from tests.conftest import START_MS

#: Gates whose PASS/FAIL depends on ``intent.notional``. Every one of these is
#: reducible in principle: shrinking the trade shrinks its projected
#: contribution, so there is always some size small enough to pass.
SIZE_SENSITIVE_GATES = [
    "MAX_ORDER_NOTIONAL",
    "MAX_POSITION_NOTIONAL",
    "MAX_GROSS_EXPOSURE",
    "MAX_NET_EXPOSURE",
    "MAX_LEVERAGE",
    "MAX_VENUE_EXPOSURE",
    "MAX_STRATEGY_EXPOSURE",
    # Added by Remediation D. It was classified as a pure current-state
    # gate because the residual it reads is measured rather than
    # projected -- but an intent's worst INTERMEDIATE residual scales
    # with its notional, and production evidence refuted the old
    # classification (P5-18).
    "MAX_UNHEDGED_EXPOSURE",
    "LIQUIDITY_SUFFICIENT",
]

#: Generous everywhere, so the inventory test measures how each gate's
#: comparison responds to size rather than which limit happens to bind first.
OPEN_LIMITS = RiskLimits(
    max_order_notional=1_000_000.0,
    max_position_notional=1_000_000.0,
    max_gross_exposure=10_000_000.0,
    max_net_exposure=10_000_000.0,
    max_venue_exposure=1_000_000.0,
    max_strategy_exposure=10_000_000.0,
    max_leverage=1_000_000.0,
    max_unhedged_notional=10_000_000.0,
)
OPEN_CTX = {"max_economical_notional": 1_000_000.0}


def smallest_passing_notional(
    limits: RiskLimits, ctx_kwargs: dict, intent_kwargs: dict, *, gate: str
) -> float | None:
    """The largest notional at or below the request that clears ``gate``.

    A deterministic descending scan rather than a solver: it makes no
    assumption about the gate's formula, which is the point — the audit must
    not reimplement the thing it is auditing.
    """
    engine = core(limits)
    requested = intent(**intent_kwargs).notional
    for step in range(1, 201):
        candidate = requested * (1.0 - step / 200.0)
        if candidate <= 0:
            break
        probe = dict(intent_kwargs)
        probe["notional"] = candidate
        decision = engine.evaluate(intent(**probe), context(**ctx_kwargs), START_MS)
        if gate not in blocking_names(decision):
            return candidate
    return None


class TestTheSizeSensitiveInventory:
    """The list above is the audit's premise; check it against production."""

    def test_every_size_sensitive_gate_is_actually_run(self):
        decision = core().evaluate(intent(), context(), START_MS)
        names = {g.name for g in decision.gates}
        missing = [n for n in SIZE_SENSITIVE_GATES if n not in names]
        assert not missing, f"not run by RuneCore._gate: {missing}"

    @pytest.mark.parametrize("name", SIZE_SENSITIVE_GATES)
    def test_each_ones_comparison_moves_with_the_notional(self, name):
        """A gate is size-sensitive only if the comparison it makes actually
        changes when the notional does.

        Compared as an ``(observed, limit)`` pair because the two shapes
        differ: most gates project the trade and compare against a fixed
        limit, while ``LIQUIDITY_SUFFICIENT`` holds a fixed ceiling and
        compares it against the requested size.

        A single leg, so that a balanced two-leg trade's zero net delta does
        not hide MAX_NET_EXPOSURE's dependence on size.
        """

        def comparison(notional: float):
            decision = core(OPEN_LIMITS).evaluate(
                intent(legs=[leg(VENUE_A, Side.BUY)], notional=notional),
                context(**OPEN_CTX),
                START_MS,
            )
            check = gate_named(decision, name)
            return (check.observed, check.limit)

        assert comparison(1_000.0) != comparison(20_000.0), (
            f"{name} made the same comparison at 1,000 and at 20,000; it is "
            "not size-sensitive and does not belong in this inventory"
        )

    def test_every_reducible_limit_has_a_headroom_candidate(self):
        """Structural statement of the H1 property, independent of behaviour.

        A limit may be read inline (``self.limits.max_gross_exposure``) or
        through a dedicated solver in :mod:`risk.limits`; either shape counts,
        because what matters is that ``_headroom`` bounds the size by it at
        all. MAX_NET_EXPOSURE and MAX_LEVERAGE were bounded by neither, so a
        trade breaching either was rejected where a smaller one would have
        passed (P5-7). MAX_UNHEDGED_EXPOSURE joined them in Remediation D,
        for the same reason arrived at from the other direction: it was
        thought not to be size-sensitive at all (P5-18).
        """
        from risk import limits as gates

        source = inspect.getsource(RuneCore._headroom)
        solvers = {
            "MAX_NET_EXPOSURE": ("max_net_exposure", "net_exposure_headroom"),
            "MAX_LEVERAGE": ("max_leverage", "leverage_headroom"),
            "MAX_UNHEDGED_EXPOSURE": (
                "max_unhedged_notional",
                "unhedged_headroom",
            ),
            "MAX_GROSS_EXPOSURE": ("max_gross_exposure", None),
            "MAX_STRATEGY_EXPOSURE": ("max_strategy_exposure", None),
            "MAX_VENUE_EXPOSURE": ("max_venue_exposure", None),
            "MAX_POSITION_NOTIONAL": ("max_position_notional", None),
            "MAX_ORDER_NOTIONAL": ("max_order_notional", None),
        }
        absent = []
        for name, (attribute, solver) in solvers.items():
            inline = f"self.limits.{attribute}" in source
            delegated = solver is not None and f"gates.{solver}(" in source
            if not (inline or delegated):
                absent.append(name)
        assert not absent, (
            "size-sensitive limits with no entry in _headroom, so a breach of "
            f"them rejects instead of reducing: {absent}"
        )

        # A delegated solver must actually consult the limit it stands for,
        # or the delegation is a name with nothing behind it.
        assert "limits.max_net_exposure" in inspect.getsource(
            gates.net_exposure_headroom
        )
        assert "limits.max_leverage" in inspect.getsource(gates.leverage_headroom)
        assert "limits.max_unhedged_notional" in inspect.getsource(
            gates.unhedged_headroom
        )

    def test_the_open_order_gate_is_deliberately_not_reducible(self):
        """The one size-sensitive-looking gate with no headroom candidate.

        An intent creates one order per leg regardless of its notional, so no
        reduction can make MAX_OPEN_ORDERS pass and rejection is the only
        correct answer. Pinned so its absence stays a decision.
        """
        source = inspect.getsource(RuneCore._headroom)
        assert "max_open_orders" not in source

    def test_headroom_and_the_gates_group_legs_the_same_way(self):
        """P5-11's other half: a gate that aggregates duplicate legs and a
        sizing path that does not would describe two different models."""
        source = inspect.getsource(RuneCore._headroom)
        assert "gates.legs_per_venue(intent)" in source
        assert "gates.legs_per_position(intent)" in source


class TestHeadroomNeverIncreasesSize:
    """H1's easy half, and the one that must never fail.

    ``approved_notional <= requested_notional`` unconditionally. Headroom is a
    ceiling, not a target.
    """

    @pytest.mark.parametrize(
        "requested",
        [
            250.0,          # exactly min_trade_notional
            250.000001,     # just above it
            1_000.0,
            5_000.0,
            24_999.99,
            25_000.0,       # exactly max_order_notional
            25_000.01,      # just above it
            100_000.0,
            1_000_000.0,
            1e12,
        ],
    )
    def test_approval_never_exceeds_the_request(self, requested):
        decision = core().evaluate(
            intent(notional=requested), context(), START_MS
        )
        assert decision.approved_notional <= requested + 1e-9, (
            f"requested {requested}, approved {decision.approved_notional}"
        )

    @pytest.mark.parametrize("requested", [250.0, 1_000.0, 9_999.0, 25_000.0])
    @pytest.mark.parametrize(
        "ctx_kwargs",
        [
            {},
            {"strategy_exposure": 90_000.0},
            {"max_economical_notional": 700.0},
            {"portfolio": portfolio_with(position(VENUE_A, quantity=400.0))},
            {
                "strategy_exposure": 95_000.0,
                "max_economical_notional": 1_200.0,
                "portfolio": portfolio_with(
                    position(VENUE_A, quantity=300.0),
                    position(VENUE_B, quantity=-200.0),
                ),
            },
        ],
    )
    def test_multiple_binding_limits_still_only_reduce(self, requested, ctx_kwargs):
        decision = core().evaluate(
            intent(notional=requested), context(**ctx_kwargs), START_MS
        )
        assert decision.approved_notional <= requested + 1e-9
        assert decision.approved_notional >= 0.0

    def test_a_tiny_request_is_never_grown_to_the_minimum(self):
        """``min_trade_notional`` is a floor on what may be authorised, not a
        size to round up to. Growing a request would authorise a trade nobody
        asked for."""
        decision = core().evaluate(intent(notional=300.0), context(), START_MS)
        assert decision.approved_notional <= 300.0 + 1e-9


class TestReducibleGatesAreActuallyReduced:
    """The documented contract, gate by gate.

    Each case configures exactly one limit so that the requested size fails it
    and a smaller size would pass. Under the sizing contract the answer is
    APPROVED_REDUCED.
    """

    def test_max_order_notional_is_reduced(self):
        decision = core(
            RiskLimits(
                max_order_notional=10_000.0,
                max_position_notional=50_000.0,
                # Opened with the rest. MAX_UNHEDGED_EXPOSURE became
                # size-sensitive in Remediation D (P5-18), so a fixture that
                # isolates one limit has to open this one too.
                max_unhedged_notional=1_000_000.0,
            )
        ).evaluate(intent(notional=24_000.0), context(), START_MS)
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(10_000.0)

    def test_gross_exposure_is_reduced(self):
        limits = RiskLimits(
            max_gross_exposure=60_000.0,
            max_position_notional=50_000.0,
            # Raised so the directional book below breaches gross, not net --
            # this case isolates one limit at a time.
            max_net_exposure=100_000.0,
            max_unhedged_notional=1_000_000.0,
        )
        book = portfolio_with(position(VENUE_A, quantity=400.0))  # 40,000 gross
        decision = core(limits).evaluate(
            intent(notional=20_000.0), context(portfolio=book), START_MS
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        # 20,000 of gross headroom across two legs.
        assert decision.approved_notional == pytest.approx(10_000.0)
        assert gate_named(decision, "MAX_GROSS_EXPOSURE").observed <= 60_000.0 + 1e-9

    def test_venue_exposure_is_reduced(self):
        limits = RiskLimits(
            max_venue_exposure=30_000.0,
            max_net_exposure=100_000.0,
            max_unhedged_notional=1_000_000.0,
        )
        book = portfolio_with(position(VENUE_A, quantity=250.0))  # 25,000 on A
        decision = core(limits).evaluate(
            intent(notional=20_000.0), context(portfolio=book), START_MS
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(5_000.0)

    def test_position_notional_is_reduced(self):
        limits = RiskLimits(
            max_position_notional=30_000.0,
            max_order_notional=25_000.0,
            max_net_exposure=100_000.0,
            max_unhedged_notional=1_000_000.0,
        )
        book = portfolio_with(position(VENUE_A, quantity=200.0))  # 20,000 on A:BTC
        decision = core(limits).evaluate(
            intent(notional=20_000.0), context(portfolio=book), START_MS
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(10_000.0)

    def test_strategy_exposure_is_reduced(self):
        limits = RiskLimits(
            max_strategy_exposure=60_000.0, max_unhedged_notional=1_000_000.0
        )
        decision = core(limits).evaluate(
            intent(notional=20_000.0), context(strategy_exposure=40_000.0), START_MS
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(10_000.0)

    def test_the_economical_ceiling_is_reduced(self):
        decision = core().evaluate(
            intent(notional=20_000.0),
            context(max_economical_notional=8_000.0),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(8_000.0)


class TestNetExposureIsReducible:
    """H1, first open question.

    A one-legged intent projects its full notional as net delta. Shrinking it
    shrinks the projection proportionally, so there is always a size that fits
    MAX_NET_EXPOSURE. The sizing contract says such a trade is reduced.
    """

    LIMITS = RiskLimits(
        max_net_exposure=10_000.0, max_unhedged_notional=1_000_000.0
    )
    ONE_LEG = {"legs": [leg(VENUE_A, Side.BUY)], "notional": 20_000.0}

    def test_a_smaller_size_would_have_passed_the_net_exposure_gate(self):
        """Establishes the premise before asserting the contract."""
        passing = smallest_passing_notional(
            self.LIMITS, {}, self.ONE_LEG, gate="MAX_NET_EXPOSURE"
        )
        assert passing is not None and passing < 20_000.0, (
            "no smaller size clears MAX_NET_EXPOSURE, so this scenario cannot "
            "test the sizing contract"
        )

    def test_an_oversized_net_delta_is_reduced_not_rejected(self):
        decision = core(self.LIMITS).evaluate(
            intent(**self.ONE_LEG), context(), START_MS
        )
        assert decision.verdict is not RiskVerdict.REJECTED, (
            "RUNE documents that it sizes an intent to fit every limit and only "
            "rejects when no reduction could pass. A one-leg intent breaching "
            "MAX_NET_EXPOSURE is reducible: "
            f"requested={decision.requested_notional} "
            f"approved={decision.approved_notional} "
            f"net_observed={gate_named(decision, 'MAX_NET_EXPOSURE').observed} "
            f"limit={gate_named(decision, 'MAX_NET_EXPOSURE').limit} "
            f"blocking={blocking_names(decision)}"
        )

    def test_an_existing_position_pushing_net_over_is_reduced(self):
        """Same question with the breach coming from current exposure rather
        than from the intent alone."""
        book = portfolio_with(position(VENUE_A, quantity=80.0))  # +8,000 net
        decision = core(self.LIMITS).evaluate(
            intent(legs=[leg(VENUE_B, Side.BUY)], notional=20_000.0),
            context(portfolio=book),
            START_MS,
        )
        assert decision.verdict is not RiskVerdict.REJECTED, (
            "a 2,000 trade would have fitted the 10,000 net limit against an "
            f"8,000 existing net position; blocking={blocking_names(decision)}"
        )


class TestLeverageIsReducible:
    """H1, second open question.

    Projected leverage is ``(gross + notional * legs) / equity``. Every term
    but ``notional`` is fixed at decision time, so leverage is strictly
    increasing in size and always has a passing size below any breach.
    """

    #: Every other size-sensitive limit is set generously so that leverage is
    #: the only gate the requested size breaches. ``max_order_notional`` and
    #: ``max_position_notional`` move together because ``RiskLimits`` refuses a
    #: configuration where one permitted order would breach the position limit.
    LIMITS = RiskLimits(
        max_leverage=1.0,
        max_gross_exposure=150_000.0,
        max_position_notional=100_000.0,
        max_order_notional=60_000.0,
        max_net_exposure=60_000.0,
        max_unhedged_notional=1_000_000.0,
    )
    REQUESTED = 60_000.0
    #: What leverage alone permits: ``(max_leverage * equity - gross) / legs``
    #: = ``(1.0 * 100,000 - 40,000) / 2``. Tighter than every other candidate
    #: -- the next-smallest is venue A's 35,000 -- so leverage is what binds
    #: the size, which is the point of the scenario.
    EXPECTED_SIZED = 30_000.0

    @staticmethod
    def _book():
        # Equity 100,000 (cash 60,000 + a 40,000 long); gross 40,000.
        return portfolio_with(
            position(VENUE_A, quantity=400.0), cash=60_000.0, peak_equity=100_000.0
        )

    @classmethod
    def _ctx(cls, book):
        # ZEPHR's ceiling must not bind before leverage does.
        return {"portfolio": book, "max_economical_notional": 200_000.0}

    def test_the_scenario_puts_leverage_over_the_limit(self):
        """The premise: as submitted, this trade breaches leverage."""
        book = self._book()
        assert book.equity == pytest.approx(100_000.0)
        assert book.gross_exposure == pytest.approx(40_000.0)
        projected = (book.gross_exposure + self.REQUESTED * 2) / book.equity
        assert projected > self.LIMITS.max_leverage, (
            "the requested size must actually breach leverage"
        )

    def test_a_smaller_size_would_have_passed_the_leverage_gate(self):
        book = self._book()
        passing = smallest_passing_notional(
            self.LIMITS,
            self._ctx(book),
            {"notional": self.REQUESTED},
            gate="MAX_LEVERAGE",
        )
        assert passing is not None and passing < self.REQUESTED, (
            "no smaller size clears MAX_LEVERAGE, so this scenario cannot test "
            "the sizing contract"
        )

    def test_leverage_is_what_binds_the_size(self):
        """Establishes that the outcome here is attributable to leverage alone:
        it is the tightest candidate, and the sized trade lands exactly on the
        limit rather than merely under it."""
        book = self._book()
        decision = core(self.LIMITS).evaluate(
            intent(notional=self.REQUESTED), context(**self._ctx(book)), START_MS
        )
        assert blocking_names(decision) == []
        assert decision.approved_notional == pytest.approx(self.EXPECTED_SIZED)
        check = gate_named(decision, "MAX_LEVERAGE")
        assert check.observed == pytest.approx(self.LIMITS.max_leverage)

    def test_an_oversized_leverage_request_is_reduced_not_rejected(self):
        book = self._book()
        decision = core(self.LIMITS).evaluate(
            intent(notional=self.REQUESTED), context(**self._ctx(book)), START_MS
        )
        check = gate_named(decision, "MAX_LEVERAGE")
        assert decision.verdict is not RiskVerdict.REJECTED, (
            "RUNE documents that it sizes an intent to fit every limit and only "
            "rejects when no reduction could pass. Projected leverage is "
            "strictly increasing in notional and therefore always reducible: "
            f"requested={decision.requested_notional} "
            f"approved={decision.approved_notional} "
            f"equity={book.equity} gross={book.gross_exposure} "
            f"leverage_observed={check.observed} limit={check.limit} "
            f"blocking={blocking_names(decision)}"
        )


class TestNonReducibleGatesRejectOutright:
    """The other half of the contract: gates that a smaller size cannot fix
    must reject, not shrink the trade to zero and call it approved."""

    @pytest.mark.parametrize(
        ("name", "ctx_kwargs", "intent_kwargs"),
        [
            ("MIN_EXPECTED_EDGE", {}, {"expected_net_edge_bps": 0.0}),
            ("MAX_DAILY_LOSS", {"portfolio": portfolio(day_realized_pnl=-9_000.0)}, {}),
            ("MAX_OPEN_ORDERS", {"open_orders": 20}, {}),
            ("MAX_ERROR_RATE", {"error_rate": 0.9}, {}),
            ("HEDGE_AVAILABLE", {"hedge_available": False}, {}),
        ],
    )
    def test_a_size_independent_breach_rejects(self, name, ctx_kwargs, intent_kwargs):
        decision = core().evaluate(
            intent(**intent_kwargs), context(**ctx_kwargs), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert name in blocking_names(decision)
        assert decision.approved_notional == 0.0


class TestMinimumTradeBoundary:
    """§10 — the exact semantics at ``min_trade_notional``.

    ``evaluate`` rejects when ``headroom < min_trade_notional``, so the
    boundary is inclusive: headroom exactly at the minimum is eligible for the
    ordinary gates.
    """

    LIMITS = RiskLimits(min_trade_notional=1_000.0, max_order_notional=25_000.0)

    def test_headroom_exactly_at_the_minimum_is_eligible(self):
        decision = core(self.LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=1_000.0),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.APPROVED_REDUCED
        assert decision.approved_notional == pytest.approx(1_000.0)

    def test_headroom_just_below_the_minimum_is_rejected(self):
        decision = core(self.LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=999.99),
            START_MS,
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_codes == ["MIN_TRADE_NOTIONAL"]
        assert decision.approved_notional == 0.0

    def test_a_below_minimum_rejection_reports_the_headroom_it_measured(self):
        decision = core(self.LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=400.0),
            START_MS,
        )
        check = gate_named(decision, "MIN_TRADE_NOTIONAL")
        assert check.observed == pytest.approx(400.0)
        assert check.limit == pytest.approx(1_000.0)

    def test_zero_headroom_rejects(self):
        book = portfolio_with(position(VENUE_A, quantity=500.0))  # at the limit
        decision = core(RiskLimits(max_position_notional=50_000.0)).evaluate(
            intent(notional=5_000.0), context(portfolio=book), START_MS
        )
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.approved_notional == 0.0

    def test_a_below_minimum_rejection_runs_no_other_gate(self):
        """Deliberate short-circuit, pinned so the behaviour is a decision
        rather than an accident: the decision carries one gate."""
        decision = core(self.LIMITS).evaluate(
            intent(notional=5_000.0),
            context(max_economical_notional=10.0),
            START_MS,
        )
        assert [g.name for g in decision.gates] == ["MIN_TRADE_NOTIONAL"]


class TestHeadroomNeverGoesNegative:
    def test_exposure_already_past_a_limit_yields_zero_not_a_negative_size(self):
        book = portfolio_with(position(VENUE_A, quantity=2_000.0))  # 200,000
        decision = core().evaluate(
            intent(notional=5_000.0), context(portfolio=book), START_MS
        )
        assert decision.approved_notional == 0.0
        assert decision.verdict is RiskVerdict.REJECTED

    def test_strategy_exposure_already_past_the_limit_yields_zero(self):
        decision = core().evaluate(
            intent(notional=5_000.0),
            context(strategy_exposure=500_000.0),
            START_MS,
        )
        assert decision.approved_notional == 0.0
        assert decision.verdict is RiskVerdict.REJECTED


class TestHeadroomDividesGrossAndStrategyByLegCount:
    """Pinning the arithmetic ``_headroom`` actually uses.

    Both MAX_GROSS_EXPOSURE and MAX_STRATEGY_EXPOSURE are projected as
    ``current + notional * legs``, so the reducible headroom per leg is
    ``(limit - current) / legs``. This is internally consistent; whether
    ``current`` arrives in the same units is a separate question, audited in
    ``test_rune_strategy_exposure.py``.
    """

    def test_gross_headroom_is_divided_by_the_leg_count(self):
        limits = RiskLimits(max_gross_exposure=20_000.0, max_position_notional=20_000.0,
                            max_order_notional=20_000.0,
                            max_unhedged_notional=1_000_000.0)
        two_leg = core(limits).evaluate(
            intent(notional=20_000.0), context(), START_MS
        )
        one_leg = core(limits).evaluate(
            intent(legs=[leg(VENUE_A, Side.BUY)], notional=20_000.0),
            context(),
            START_MS,
        )
        assert two_leg.approved_notional == pytest.approx(10_000.0)
        assert one_leg.approved_notional == pytest.approx(20_000.0)

    def test_the_resulting_projection_lands_exactly_on_the_limit(self):
        limits = RiskLimits(max_gross_exposure=20_000.0, max_position_notional=20_000.0,
                            max_order_notional=20_000.0,
                            max_unhedged_notional=1_000_000.0)
        decision = core(limits).evaluate(intent(notional=20_000.0), context(), START_MS)
        check = gate_named(decision, "MAX_GROSS_EXPOSURE")
        assert check.observed == pytest.approx(20_000.0)
        assert check.observed <= check.limit + 1e-9
