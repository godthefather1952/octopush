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
Public REST recent-trades state, sampled either side of the listening window,
is independent evidence of whether the market traded at all. It needs no
credential — it is the same public data the stream carries, which is exactly
why it can corroborate the stream without being the stream.

These tests pin the classification, because it is the part that decides
whether an endpoint gets adopted.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO / "scripts" / "lib" / "probe_venue_a.py"


def load_probe():
    """Load the probe as a module without needing it on sys.path."""
    spec = importlib.util.spec_from_file_location("probe_venue_a", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
