"""Zero trade events is not automatically an incompatibility: P12-F2.

THE PROBLEM WITH THE FIRST PROBE
================================
The Binance.US probe passed every REST and depth-stream requirement from
Codespaces — 159 depth events, zero sequence gaps — and observed **no trade
events at all** in a 65-second window. The probe scored that as a failed
requirement.

That verdict was not supportable. Zero trade events is two completely
different situations wearing the same face:

* the market was quiet and there was nothing to deliver — not a defect;
* trades happened and the trade stream failed to deliver them — a real
  incompatibility that would silently starve the trade parser.

Waiting longer does not fix it. A longer window makes a quiet market less
likely, never impossible, and an arbitrary timeout that is "long enough"
is just a slower guess.

THE CORRECTION
==============
Public REST recent-trades state is independent evidence of whether the market
traded at all. It needs no credential — it is the same public data the stream
carries, which is exactly why it can corroborate the stream without being the
stream.

AND THE RACE THE FIRST CORRECTION STILL HAD
===========================================
Sampling REST "either side of the window" is not enough on its own. The first
corrected probe took its baseline *before* opening the socket and its final
sample *after* closing it, leaving two intervals in which a trade could occur
with nothing listening — and a trade landing in either one produced "REST
advanced, stream delivered nothing", the signature of a broken stream.

So the ordering is now part of the contract: the listener is already draining
the socket before the baseline is taken, and it is still draining when the
final sample is taken. Only then is the delivered count read, and only then
does the listener stop. Every REST-observed trade therefore falls inside a
window the stream was genuinely listening through.

These tests pin both the classification and that ordering, because together
they are what decides whether an endpoint gets adopted.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO / "scripts" / "lib" / "probe_venue_a.py"


def load_probe():
    """Load the probe as a module without needing it on sys.path.

    Registered in ``sys.modules`` before execution because ``@dataclass``
    resolves annotations through ``sys.modules[cls.__module__]``, which does
    not exist yet for a module loaded straight from a path.
    """
    spec = importlib.util.spec_from_file_location("probe_venue_a", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture(scope="module")
def probe():
    return load_probe()


class TestTheThreeClassifications:
    def test_case_a_quiet_market_is_inconclusive_not_a_failure(self, probe):
        """The literal situation the first Binance.US probe was in."""
        status, detail = probe.classify_trade_liveness(
            rest_trades_advanced=False, ws_trade_events=0
        )
        assert status == probe.INCONCLUSIVE
        assert status != probe.FAIL, (
            "a quiet market must never be reported as endpoint incompatibility"
        )
        assert "does not indicate incompatibility" in detail

    def test_case_b_trades_occurred_and_were_delivered_is_a_pass(self, probe):
        status, _ = probe.classify_trade_liveness(
            rest_trades_advanced=True, ws_trade_events=4
        )
        assert status == probe.PASS

    def test_case_c_trades_occurred_and_none_were_delivered_is_a_failure(self, probe):
        status, detail = probe.classify_trade_liveness(
            rest_trades_advanced=True, ws_trade_events=0
        )
        assert status == probe.FAIL
        assert "TRADE STREAM DELIVERY INCOMPATIBLE" in detail


class TestClassificationEdges:
    def test_delivered_events_settle_it_even_if_rest_did_not_advance(self, probe):
        """Delivery is the question. Events that arrived answer it."""
        status, _ = probe.classify_trade_liveness(
            rest_trades_advanced=False, ws_trade_events=2
        )
        assert status == probe.PASS

    def test_a_single_delivered_event_is_enough(self, probe):
        status, _ = probe.classify_trade_liveness(
            rest_trades_advanced=True, ws_trade_events=1
        )
        assert status == probe.PASS

    def test_the_three_statuses_are_distinct(self, probe):
        assert len({probe.PASS, probe.FAIL, probe.INCONCLUSIVE}) == 3


class TestInconclusiveDoesNotBlockAdoption:
    """An inconclusive check must not be counted as a failure when the probe
    decides whether the endpoint is usable."""

    def test_only_failures_are_counted_against_an_endpoint(self, probe):
        probe.RESULTS.clear()
        probe.record_status("depth", probe.PASS)
        probe.record_status("trade liveness", probe.INCONCLUSIVE)

        failures = [n for n, status, _ in probe.RESULTS if status == probe.FAIL]
        unresolved = [
            n for n, status, _ in probe.RESULTS if status == probe.INCONCLUSIVE
        ]
        assert failures == []
        assert unresolved == ["trade liveness"]
        probe.RESULTS.clear()


def probe_code_only() -> str:
    """The probe's executable code, with comments and docstrings removed.

    Scanning raw source for words like "signature" matches the docstring
    promising that no signature is sent — a guard that fires on its own
    documentation cannot catch a real credential. Rebuilding from the syntax
    tree drops comments and docstrings while keeping every genuine string
    literal, so a URL or header name in actual code is still caught.
    """
    tree = ast.parse(PROBE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class TestTheProbeStaysPublicAndReadOnly:
    def test_it_uses_the_public_recent_trades_interface(self):
        assert "/api/v3/trades" in probe_code_only()

    def test_no_credential_or_private_endpoint_appears(self):
        text = probe_code_only().lower()
        for token in (
            "api_key",
            "apikey",
            "secret",
            "signature",
            "hmac",
            "x-mbx-apikey",
            "/api/v3/order",
            "/api/v3/account",
            "listenkey",
        ):
            assert token not in text, f"probe must stay public: found {token!r}"

    def test_it_performs_no_write_requests(self):
        code = probe_code_only()
        for verb in ("client.post(", "client.put(", "client.delete("):
            assert verb not in code, "the probe is read-only"


# ======================================================================
# The observation ordering — the race this pass exists to remove
# ======================================================================


async def noop_sleep(_seconds: float) -> None:
    """Collapse the observation window so tests run instantly."""
    return None


def scripted_rest(samples: list[dict[str, int | None]]):
    """Return REST states in order, repeating the last one when exhausted."""
    calls = {"n": 0}

    async def sample() -> dict[str, int | None]:
        index = min(calls["n"], len(samples) - 1)
        calls["n"] += 1
        return dict(samples[index])

    return sample, calls


def recording_listener(order: list[str]):
    async def start() -> None:
        order.append("START")

    async def stop() -> None:
        order.append("STOP")

    return start, stop


class TestObservationOrderingHasNoUncoveredInterval:
    """The previous probe sampled REST before the socket opened and again
    after it closed, so a trade in either uncovered interval looked like a
    delivery failure. The ordering below is what makes "REST advanced but the
    stream delivered nothing" mean something."""

    async def test_the_listener_starts_before_the_rest_baseline(self, probe):
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": 1}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=0,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.order.index("listener-started") < result.order.index("rest-baseline")

    async def test_the_final_rest_sample_happens_before_the_listener_stops(self, probe):
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": 1}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=0,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.order.index("rest-final") < result.order.index("listener-stopped")

    async def test_the_full_ordering_is_the_documented_one(self, probe):
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": 1}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=0,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.order[0] == "listener-started"
        assert result.order[1] == "rest-baseline"
        assert result.order[-3] == "rest-final"
        assert result.order[-2] == "read-ws-trade-count"
        assert result.order[-1] == "listener-stopped"
        assert order == ["START", "STOP"]

    async def test_the_trade_count_is_read_while_the_listener_is_still_running(
        self, probe
    ):
        """Counting after the stop would reintroduce a gap at the other end."""
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": 1}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 7,
            window_s=0,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.trade_events == 7
        assert result.order.index("read-ws-trade-count") < result.order.index(
            "listener-stopped"
        )


class TestRestAdvancementDetection:
    async def test_a_trade_mid_window_is_detected_even_if_superseded(self, probe):
        """Polling during the window is why a transient advance is not lost."""
        order: list[str] = []
        start, stop = recording_listener(order)
        # baseline 10, mid-window 11, final back to 11 -- an advance occurred.
        sample, _ = scripted_rest(
            [{"BTCUSDT": 10}, {"BTCUSDT": 11}, {"BTCUSDT": 11}]
        )

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=2,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.rest_advanced is True
        assert result.advanced_symbols == ["BTCUSDT"]

    async def test_a_quiet_market_reports_no_advance(self, probe):
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": 10}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=2,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.rest_advanced is False
        assert result.advanced_symbols == []

    async def test_one_symbol_advancing_is_enough(self, probe):
        """ETHUSDT stayed quiet in the field run; BTCUSDT did not."""
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest(
            [
                {"BTCUSDT": 31760197, "ETHUSDT": 500},
                {"BTCUSDT": 31760198, "ETHUSDT": 500},
            ]
        )

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 1,
            window_s=1,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.rest_advanced is True
        assert result.advanced_symbols == ["BTCUSDT"]

    async def test_unavailable_rest_state_is_reported_not_guessed(self, probe):
        order: list[str] = []
        start, stop = recording_listener(order)
        sample, _ = scripted_rest([{"BTCUSDT": None}])

        result = await probe.observe_trade_liveness(
            start_listener=start,
            stop_listener=stop,
            sample_rest=sample,
            trade_events_so_far=lambda: 0,
            window_s=0,
            poll_s=1,
            sleep=noop_sleep,
        )

        assert result.unavailable == ["BTCUSDT"]
        assert result.rest_advanced is False


class TestStreamListenerFolding:
    """The listener's frame accounting, exercised without a socket."""

    def test_a_depth_event_is_counted_and_sequenced(self, probe):
        listener = probe.StreamListener()
        listener.consume(
            '{"stream":"btcusdt@depth@100ms","data":{"e":"depthUpdate","U":1,"u":5}}'
        )
        assert listener.depth_events == 1
        assert listener.sequencing_ok
        assert listener.contiguous

    def test_a_sequence_gap_is_detected(self, probe):
        listener = probe.StreamListener()
        for first, final in ((1, 5), (9, 12)):
            listener.consume(
                '{"stream":"btcusdt@depth@100ms","data":'
                f'{{"e":"depthUpdate","U":{first},"u":{final}}}}}'
            )
        assert listener.contiguous is False
        assert listener.gap_detail

    def test_contiguous_updates_stay_contiguous(self, probe):
        listener = probe.StreamListener()
        for first, final in ((1, 5), (6, 9), (10, 14)):
            listener.consume(
                '{"stream":"btcusdt@depth@100ms","data":'
                f'{{"e":"depthUpdate","U":{first},"u":{final}}}}}'
            )
        assert listener.contiguous is True
        assert listener.depth_events == 3

    def test_a_trade_event_is_counted_with_its_id(self, probe):
        listener = probe.StreamListener()
        listener.consume(
            '{"stream":"btcusdt@trade","data":{"e":"trade","t":31760198,'
            '"p":"77213.13000000","q":"0.00006000"}}'
        )
        assert listener.trade_events == 1
        assert listener.trade_ids == [31760198]

    def test_a_missing_envelope_is_flagged(self, probe):
        listener = probe.StreamListener()
        listener.consume('{"e":"depthUpdate","U":1,"u":2}')
        assert listener.envelope_ok is False

    def test_missing_u_fields_fail_sequencing(self, probe):
        listener = probe.StreamListener()
        listener.consume('{"stream":"s","data":{"e":"depthUpdate"}}')
        assert listener.sequencing_ok is False
