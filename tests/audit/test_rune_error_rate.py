"""Phase 5 — P5-8: what horizon does MAX_ERROR_RATE actually measure?

``Orchestrator._error_rate`` documented itself as "Rolling share of bus
deliveries that raised" and then computed ``errors / (delivered + errors)``
over the bus subscriptions' **lifetime** counters. Nothing decayed, nothing
windowed.

The distinction is the whole point of the gate. A rolling rate says "the
platform is failing right now, stop trading". A lifetime rate says "the
platform has failed this often since it started", which converges toward zero
in any long-running healthy process and stops responding to a live incident —
so the longer a process had been healthy, the harder it became to stop.

Remediation E2 made the measurement genuinely rolling: each bus records the
outcome of every delivery attempt in a bounded window
(``core.bus.base.DeliveryOutcomeWindow``, sized by
``RiskLimits.error_rate_window_deliveries``) and exposes
``recent_error_rate``; the orchestrator reads that number rather than deriving
one. The assertions below that used to *establish* the lifetime horizon are
now inverted: they assert it is gone, and that the safety property it defeated
holds.
"""

from __future__ import annotations

import inspect

import pytest

from apps.orchestrator.orchestrator import Orchestrator
from core.bus import InMemoryEventBus
from core.bus.base import DEFAULT_DELIVERY_WINDOW, DeliveryOutcomeWindow
from core.config import RiskLimits
from core.events import Event, EventType
from core.models.risk import GateResult
from tests.audit.rune_fixtures import blocking_names, context, core, gate_named, intent
from tests.conftest import START_MS

LIMITS = RiskLimits()  # max_error_rate = 0.25, window = 200

#: A long healthy history, then a burst in which every recent delivery failed.
#: The exact case the gate exists to catch, and the exact case a lifetime
#: ratio could not see.
HEALTHY_HISTORY = 10_000
BURST = 200


def window(successes: int, errors: int, *, maxlen: int | None = None) -> DeliveryOutcomeWindow:
    """A window fed ``successes`` good outcomes and then ``errors`` bad ones."""
    made = DeliveryOutcomeWindow(
        LIMITS.error_rate_window_deliveries if maxlen is None else maxlen
    )
    for _ in range(successes):
        made.record_success()
    for _ in range(errors):
        made.record_error()
    return made


class FakeBus:
    """Only what ``_error_rate`` reads — which is now one number."""

    def __init__(self, rate: float) -> None:
        self.recent_error_rate = rate


def reported(rate: float) -> float:
    """Run production's own accessor against a stand-in bus."""
    fake = Orchestrator.__new__(Orchestrator)
    fake.bus = FakeBus(rate)
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


class TestTheHorizonIsRolling:
    """The inverted premise: the lifetime formula must be gone, not merely
    supplemented by a rolling one somewhere else."""

    def test_the_orchestrator_no_longer_derives_the_rate(self):
        source = inspect.getsource(Orchestrator._error_rate)
        body = source.split('"""')[-1]
        assert "recent_error_rate" in body
        for lifetime in ("s.delivered for s in subs", "s.errors for s in subs"):
            assert lifetime not in body, (
                f"{lifetime!r} is back in _error_rate: the gate would again be "
                "measuring the whole uptime instead of the recent past"
            )

    def test_the_window_is_configurable_and_bounded(self):
        fields = set(RiskLimits.model_fields)
        assert "error_rate_window_deliveries" in fields
        assert LIMITS.error_rate_window_deliveries == DEFAULT_DELIVERY_WINDOW

    def test_both_buses_answer_the_contract(self):
        from core.bus import base, redis_bus

        assert "recent_error_rate" in inspect.getsource(base.EventBus)
        for module in (InMemoryEventBus, redis_bus.RedisStreamBus):
            assert "recent_error_rate" in inspect.getsource(module)

    def test_the_window_reads_no_clock(self):
        source = inspect.getsource(DeliveryOutcomeWindow)
        for timey in ("time.", "now_ms", "monotonic", "datetime", "perf_counter"):
            assert timey not in source, (
                f"{timey!r} appears in the delivery window; eviction must be by "
                "count so replay measures the same health as the live run"
            )

    def test_lifetime_counters_survive_as_diagnostics(self):
        """Removing the bad measurement must not remove the per-handler
        counters that say *which* handler is failing."""
        from core.bus import base, memory

        assert "sub.delivered += 1" in inspect.getsource(memory)
        assert "delivered: int = 0" in inspect.getsource(base)


class TestTheWindowArithmetic:
    def test_an_empty_window_reports_no_errors(self):
        assert DeliveryOutcomeWindow(200).error_rate == 0.0

    def test_a_full_healthy_window_reports_zero(self):
        assert window(200, 0).error_rate == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("successes", "errors", "expected"),
        [
            (150, 50, 0.25),
            (149, 51, 0.255),
            (100, 100, 0.5),
            (0, 200, 1.0),
        ],
    )
    def test_a_partly_failing_window(self, successes, errors, expected):
        assert window(successes, errors).error_rate == pytest.approx(expected)

    def test_only_the_newest_outcomes_are_retained(self):
        made = window(HEALTHY_HISTORY, BURST)
        assert len(made) == LIMITS.error_rate_window_deliveries
        assert made.maxlen == LIMITS.error_rate_window_deliveries

    def test_a_long_healthy_history_cannot_dilute_a_total_burst(self):
        """10,000 successes then 200 failures: the window holds only the
        failures, so the rate is 1.0 — not the 0.0196 a lifetime ratio gives."""
        assert window(HEALTHY_HISTORY, BURST).error_rate == pytest.approx(1.0)

    def test_a_long_failing_history_cannot_condemn_a_recovered_process(self):
        """The property in the other direction, which matters just as much: a
        platform that has recovered must be allowed to trade again."""
        made = DeliveryOutcomeWindow(LIMITS.error_rate_window_deliveries)
        for _ in range(HEALTHY_HISTORY):
            made.record_error()
        for _ in range(BURST):
            made.record_success()
        assert made.error_rate == pytest.approx(0.0)

    @pytest.mark.parametrize("maxlen", [1, 7, 200, 1_000])
    def test_the_window_never_exceeds_its_size(self, maxlen):
        made = window(maxlen * 2, maxlen * 2, maxlen=maxlen)
        assert len(made) == maxlen
        assert made.error_rate == pytest.approx(1.0)

    def test_a_zero_length_window_is_refused(self):
        """A window retaining nothing would always report 0.0 — a gate that
        can never fire, configured by accident."""
        with pytest.raises(ValueError):
            DeliveryOutcomeWindow(0)

    @pytest.mark.parametrize("size", [0, -1, 100_001])
    def test_the_configured_size_is_bounded(self, size):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RiskLimits(error_rate_window_deliveries=size)


class TestTheBusRecordsWhatItDispatches:
    """The window is fed at dispatch time by the real bus, not reconstructed."""

    async def test_a_failing_handler_moves_the_rate(self):
        bus = InMemoryEventBus()

        async def boom(_event: Event) -> None:
            raise RuntimeError("handler failed")

        bus.subscribe(boom, name="boom")
        assert bus.recent_error_rate == 0.0
        for i in range(10):
            await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=i, source="audit"))
        await bus.drain()
        assert bus.recent_error_rate == pytest.approx(1.0)

    async def test_a_healthy_handler_keeps_it_at_zero(self):
        bus = InMemoryEventBus()

        async def fine(_event: Event) -> None:
            return None

        bus.subscribe(fine, name="fine")
        for i in range(10):
            await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=i, source="audit"))
        await bus.drain()
        assert bus.recent_error_rate == pytest.approx(0.0)

    async def test_a_strict_bus_still_records_the_failure_it_re_raises(self):
        """``raise_on_handler_error=True`` propagates the exception. The
        attempt still happened, and a window that forgot it would report the
        healthier of the two possible answers exactly when the bus is
        configured to be strict."""
        bus = InMemoryEventBus(raise_on_handler_error=True)

        async def boom(_event: Event) -> None:
            raise RuntimeError("handler failed")

        bus.subscribe(boom, name="boom")
        await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=1, source="audit"))
        with pytest.raises(RuntimeError):
            await bus.drain()
        assert bus.recent_error_rate == pytest.approx(1.0)

    async def test_one_outcome_per_handler_per_event(self):
        """Two subscriptions, one failing: half the attempts fail."""
        bus = InMemoryEventBus()

        async def boom(_event: Event) -> None:
            raise RuntimeError("handler failed")

        async def fine(_event: Event) -> None:
            return None

        bus.subscribe(boom, name="boom")
        bus.subscribe(fine, name="fine")
        for i in range(10):
            await bus.publish(Event(type=EventType.SYSTEM_EVENT, ts_ms=i, source="audit"))
        await bus.drain()
        assert bus.recent_error_rate == pytest.approx(0.5)


class TestAConcentratedBurstMustBeVisible:
    """P5-8's safety property, unchanged from the audit that found it.

    A platform healthy for a long time that starts failing hard right now is
    exactly what MAX_ERROR_RATE exists to catch. Whether it can be caught
    depends entirely on the horizon.
    """

    def test_the_burst_is_severe_by_any_recent_measure(self):
        """The premise: over the last 200 deliveries, everything failed."""
        recent_rate = window(HEALTHY_HISTORY, BURST).error_rate
        assert recent_rate == pytest.approx(1.0)
        assert recent_rate > LIMITS.max_error_rate

    def test_a_current_failure_burst_reaches_the_threshold(self):
        observed = reported(window(HEALTHY_HISTORY, BURST).error_rate)
        decision = core(LIMITS).evaluate(
            intent(), context(error_rate=observed), START_MS
        )
        assert "MAX_ERROR_RATE" in blocking_names(decision), (
            f"every one of the last {BURST} bus deliveries raised, and the "
            f"platform still authorised a trade: reported_rate={observed:.6f} "
            f"limit={LIMITS.max_error_rate}"
        )

    @pytest.mark.parametrize("history", [1_000, 10_000, 100_000])
    def test_uptime_no_longer_dilutes_the_measurement(self, history):
        """The control. Under the old lifetime ratio this was
        ``BURST / (history + BURST)`` and shrank toward zero as the platform
        stayed up; the rolling window is indifferent to how long the healthy
        history is."""
        observed = window(history, BURST).error_rate
        assert observed == pytest.approx(1.0)
        assert observed > LIMITS.max_error_rate

    def test_the_number_of_errors_needed_no_longer_grows_with_uptime(self):
        """Quantifies the closed gap. After 100,000 healthy deliveries a
        lifetime ratio needed tens of thousands of accumulated errors before it
        fired; a rolling window over 200 needs a bare majority of the window's
        share of the limit."""
        made = window(100_000, 0)
        needed = 0
        while made.error_rate <= LIMITS.max_error_rate:
            made.record_error()
            needed += 1
            if needed > LIMITS.error_rate_window_deliveries:
                break
        assert needed == 51, (
            "after 100,000 healthy deliveries the gate needs "
            f"{needed} recent errors before it fires"
        )


class TestTheDocumentedContract:
    def test_the_docstring_and_the_implementation_now_agree(self):
        doc = inspect.getdoc(Orchestrator._error_rate) or ""
        assert "most recent" in doc
        assert "lifetime" in doc, (
            "the docstring should still say what the old counters are, so a "
            "reader does not reach for them again"
        )

    def test_configuration_selects_the_window(self):
        """If the horizon is deliberate there is somewhere to say so."""
        fields = set(RiskLimits.model_fields)
        assert "max_error_rate" in fields
        assert [f for f in fields if "error" in f and "window" in f]
