"""Phase 5 — H7: what horizon does MAX_ERROR_RATE actually measure?

``Orchestrator._error_rate`` documents itself as:

    "Rolling share of bus deliveries that raised."

and computes ``errors / (delivered + errors)`` over the bus subscriptions'
**lifetime** counters. Nothing decays, nothing windows.

The distinction matters because of what the gate is for. A rolling rate says
"the platform is failing right now, stop trading". A lifetime rate says "the
platform has failed this often since it started", which converges toward zero
in any long-running healthy process and stops responding to a live incident.

This file measures the horizon rather than asserting a preferred design, then
states the safety property: a concentrated recent failure burst must be able
to reach the configured threshold.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.config import RiskLimits
from core.models.risk import GateResult
from tests.audit.rune_fixtures import blocking_names, context, core, gate_named, intent
from tests.conftest import START_MS

LIMITS = RiskLimits()  # max_error_rate = 0.25


@dataclass
class FakeSubscription:
    """Only what ``_error_rate`` reads."""

    delivered: int
    errors: int


class FakeBus:
    def __init__(self, subscriptions):
        self.subscriptions = subscriptions


def error_rate_of(delivered: int, errors: int) -> float:
    """Run production's own formula against a synthetic bus.

    ``_error_rate`` touches nothing but ``bus.subscriptions``, so it can be
    called unbound against a stand-in rather than by driving a platform.
    """
    fake = Orchestrator.__new__(Orchestrator)
    fake.bus = FakeBus([FakeSubscription(delivered=delivered, errors=errors)])
    return Orchestrator._error_rate(fake)


class TestTheGateItself:
    @pytest.mark.parametrize("rate", [0.0, 0.1, 0.2499, 0.25])
    def test_at_or_below_the_limit_passes(self, rate):
        decision = core(LIMITS).evaluate(intent(), context(error_rate=rate), START_MS)
        assert gate_named(decision, "MAX_ERROR_RATE").result is GateResult.PASS

    @pytest.mark.parametrize("rate", [0.2501, 0.5, 1.0])
    def test_above_the_limit_blocks(self, rate):
        decision = core(LIMITS).evaluate(intent(), context(error_rate=rate), START_MS)
        assert "MAX_ERROR_RATE" in blocking_names(decision)

    def test_the_boundary_is_inclusive(self):
        """``error_rate <= max_error_rate``."""
        assert not gate_named(
            core(LIMITS).evaluate(intent(), context(error_rate=0.25), START_MS),
            "MAX_ERROR_RATE",
        ).blocking


class TestTheHorizonIsLifetime:
    """Establishing what the number means, before judging it."""

    def test_the_formula_uses_lifetime_counters(self):
        source = inspect.getsource(Orchestrator._error_rate)
        assert "s.delivered for s in subs" in source
        assert "s.errors for s in subs" in source
        assert "errors / total" in source
        for windowing in ("window", "recent", "deque", "decay", "since"):
            assert windowing not in source.lower(), (
                f"{windowing!r} appears in _error_rate; the horizon may have "
                "changed and this audit's premise needs rechecking"
            )

    def test_bus_subscription_counters_only_ever_increase(self):
        """A lifetime counter is only a lifetime measure if nothing resets it."""
        from core.bus import base, memory

        assert "sub.delivered += 1" in inspect.getsource(memory)
        assert "delivered: int = 0" in inspect.getsource(base)
        # No reset path anywhere in the bus.
        for module in (base, memory):
            source = inspect.getsource(module)
            assert "sub.delivered = 0" not in source
            assert "self.delivered = 0" not in source

    @pytest.mark.parametrize(
        ("delivered", "errors", "expected"),
        [
            (0, 0, 0.0),
            (100, 0, 0.0),
            (75, 25, 0.25),
            (50, 50, 0.5),
            (0, 10, 1.0),
        ],
    )
    def test_the_formula_computes_what_it_claims(self, delivered, errors, expected):
        assert error_rate_of(delivered, errors) == pytest.approx(expected)


class TestAConcentratedBurstMustBeVisible:
    """H7's safety property.

    A platform that has been healthy for a long time and then starts failing
    hard right now is exactly the case MAX_ERROR_RATE exists to catch. Whether
    it can be caught depends entirely on the horizon.
    """

    #: A long healthy history, then a burst. Every one of the recent
    #: deliveries failed, so any rolling measure over a recent window would
    #: report a rate of 1.0.
    HEALTHY_HISTORY = 10_000
    BURST_ERRORS = 200

    def test_the_burst_is_severe_by_any_recent_measure(self):
        """The premise: over the last 200 deliveries, everything failed."""
        recent_rate = self.BURST_ERRORS / self.BURST_ERRORS
        assert recent_rate == pytest.approx(1.0)
        assert recent_rate > LIMITS.max_error_rate

    def test_a_current_failure_burst_reaches_the_threshold(self):
        observed = error_rate_of(self.HEALTHY_HISTORY, self.BURST_ERRORS)
        decision = core(LIMITS).evaluate(
            intent(), context(error_rate=observed), START_MS
        )
        assert "MAX_ERROR_RATE" in blocking_names(decision), (
            "every one of the last "
            f"{self.BURST_ERRORS} bus deliveries raised, and the platform "
            f"still authorised a trade: reported_rate={observed:.6f} "
            f"limit={LIMITS.max_error_rate} "
            f"(the rate is diluted by {self.HEALTHY_HISTORY} historical "
            "successful deliveries because the measure is a lifetime ratio, "
            "not the rolling one it is documented as)"
        )

    @pytest.mark.parametrize("history", [1_000, 10_000, 100_000])
    def test_the_dilution_grows_with_uptime(self, history):
        """The longer the platform has been healthy, the less a live incident
        moves the number. Diagnostic, not a verdict."""
        observed = error_rate_of(history, self.BURST_ERRORS)
        assert observed == pytest.approx(
            self.BURST_ERRORS / (history + self.BURST_ERRORS)
        )
        assert observed < LIMITS.max_error_rate

    def test_how_many_errors_a_long_uptime_needs_before_it_reacts(self):
        """Quantifies the gap for the audit report."""
        history = 100_000
        needed = 1
        while error_rate_of(history, needed) <= LIMITS.max_error_rate:
            needed *= 2
            if needed > 10_000_000:
                break
        assert needed > 30_000, (
            "after 100,000 healthy deliveries the gate needs "
            f"{needed} accumulated errors before it fires"
        )


class TestTheDocumentedContract:
    def test_the_docstring_says_rolling(self):
        """Recorded so the finding can be classified as implementation-vs-doc
        rather than argued about."""
        doc = inspect.getdoc(Orchestrator._error_rate) or ""
        assert "Rolling" in doc

    def test_no_configuration_selects_a_window(self):
        """If the lifetime horizon were deliberate there would be somewhere to
        say so."""
        fields = set(RiskLimits.model_fields)
        assert "max_error_rate" in fields
        assert not [f for f in fields if "error" in f and "window" in f]
