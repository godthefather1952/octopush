"""Reconnect backoff resets only once a session proves itself: TIDAL-M5.

The old code called ``reconnect.reset()`` immediately after the WebSocket
handshake succeeded, before a single byte of application data had arrived.
A connection that is accepted and then immediately closed — a public
exchange rate-limiting or rejecting a client, for instance — reset the delay
to its minimum every time, producing a reconnect storm at roughly the
initial interval forever instead of the exponential backoff the policy
exists to provide.

These tests drive the real ``WebSocketAdapter._session()``/``run()`` loop
against a scripted fake socket (no network), so what's being proven is the
actual reconnect mechanism, not a description of it. A minimal adapter
subclass stands in for a real venue: the mechanism under test
(``mark_healthy`` / ``ReconnectPolicy``) lives in the shared base class, and
using a toy adapter keeps the test from being entangled with either venue's
own parsing or synchronization logic — that wiring is covered separately in
``tests/unit/test_malformed_message_isolation.py``.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from core.clock import ManualClock
from core.config import VenueConfig
from venues.base.ws import WebSocketAdapter


class ScriptedSocket:
    """Stands in for the object ``websockets.connect(...)`` yields.

    Hands out a fixed list of frames, then raises to simulate the far end
    closing the connection — immediately, if the list is empty.
    """

    def __init__(self, frames: list[str] = ()) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []

    async def __aenter__(self) -> ScriptedSocket:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def recv(self) -> str:
        if not self._frames:
            raise ConnectionResetError("scripted close")
        return self._frames.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(data)


class ToyAdapter(WebSocketAdapter):
    """The minimal concrete adapter needed to exercise ``_session()``.

    ``"HEALTHY"`` is the one frame that counts as real market data; anything
    else is either ignored (an unknown/heartbeat-shaped frame) or malformed
    (counted as an error) — mirroring the two real adapters' shape without
    depending on either one's parser.
    """

    def connect_url(self) -> str:
        return "wss://example.invalid/toy"

    async def handle_payload(self, payload: str) -> None:
        if payload == "HEALTHY":
            self.mark_healthy()
            await self.emit_raw(payload)
            return
        if payload == "MALFORMED":
            self.stats.errors += 1
            return
        # An ignored/unknown frame: received, but proves nothing about
        # whether the market-data stream itself is working.


def build_adapter(clock: ManualClock) -> ToyAdapter:
    config = VenueConfig(name="TOY", display_name="Toy", adapter="simulated")
    return ToyAdapter(config, clock, [])


async def run_scripted_sessions(adapter: ToyAdapter, sockets: list[ScriptedSocket]) -> list[float]:
    """Replay ``run()``'s essential cycle without sleeping between attempts.

    For each scripted socket: run one real ``_session()`` against it (via the
    same ``websockets.connect`` call site production uses, patched to hand
    out that socket), let it fail exactly as a real disconnect would, then
    call ``next_delay()`` exactly as ``run()`` does. The returned list is the
    sequence of delays a real reconnect loop would actually have slept for.
    """
    delays: list[float] = []
    for socket in sockets:
        with (
            patch("websockets.connect", lambda *_a, socket=socket, **_kw: socket),
            contextlib.suppress(Exception),
        ):
            await adapter._session()
        delays.append(adapter.reconnect.next_delay())
    return delays


class TestRepeatedImmediateCloseBacksOffExponentially:
    async def test_delays_grow_with_each_immediate_close(self):
        adapter = build_adapter(ManualClock(0))
        delays = await run_scripted_sessions(adapter, [ScriptedSocket([]) for _ in range(4)])
        assert delays == [1.0, 2.0, 4.0, 8.0]

    async def test_max_backoff_remains_bounded(self):
        adapter = build_adapter(ManualClock(0))
        delays = await run_scripted_sessions(adapter, [ScriptedSocket([]) for _ in range(10)])
        assert delays[-1] == pytest.approx(30.0)
        assert all(d <= 30.0 for d in delays)


class TestAHealthySessionResetsBackoff:
    async def test_a_valid_message_then_disconnect_resets_the_delay(self):
        adapter = build_adapter(ManualClock(0))
        # Two immediate closes first, so backoff has genuinely grown...
        delays = await run_scripted_sessions(
            adapter, [ScriptedSocket([]), ScriptedSocket([])]
        )
        assert delays == [1.0, 2.0]
        # ...then a session that delivers one real message before closing.
        delays += await run_scripted_sessions(adapter, [ScriptedSocket(["HEALTHY"])])
        assert delays[-1] == 1.0, "a session that proved itself must reset the delay"
        # And a subsequent immediate close grows from that reset baseline,
        # not from where the pre-reset streak left off.
        delays += await run_scripted_sessions(adapter, [ScriptedSocket([])])
        assert delays[-1] == 2.0

    async def test_connected_but_never_receiving_anything_does_not_reset(self):
        """The handshake succeeding is not the same as the session proving
        itself — this is the literal old bug: reset() used to fire here,
        right after ``websockets.connect`` returned, before any frame.
        """
        adapter = build_adapter(ManualClock(0))
        adapter.reconnect.attempt = 3  # simulate having already backed off
        with (
            patch("websockets.connect", lambda *_a, **_kw: ScriptedSocket([])),
            contextlib.suppress(Exception),
        ):
            await adapter._session()
        assert adapter.stats.connected is True, "the handshake did succeed"
        assert adapter.reconnect.attempt == 3, "but that alone must not have reset backoff"


class TestMalformedFramesAloneDoNotResetBackoff:
    async def test_malformed_only_session_does_not_reset(self):
        adapter = build_adapter(ManualClock(0))
        delays = await run_scripted_sessions(adapter, [ScriptedSocket([]) for _ in range(2)])
        assert delays == [1.0, 2.0]
        # A session that receives frames, but none of them real market data.
        delays += await run_scripted_sessions(
            adapter, [ScriptedSocket(["MALFORMED", "MALFORMED", "IGNORED"])]
        )
        assert delays[-1] == 4.0, "malformed/ignored frames must not look like health"
        assert adapter.stats.errors == 2


class TestMarkHealthyIsTheOnlyResetPath:
    def test_mark_healthy_resets_the_policy_directly(self):
        adapter = build_adapter(ManualClock(0))
        adapter.reconnect.attempt = 5
        adapter.mark_healthy()
        assert adapter.reconnect.attempt == 0

    def test_the_handshake_itself_no_longer_resets_anything(self):
        """Static guard: the reset call must not be sitting right after
        ``websockets.connect`` in ``_session`` — it was the whole defect.
        """
        import inspect

        from venues.base import ws as ws_module

        source = inspect.getsource(ws_module.WebSocketAdapter._session)
        # Everything between the connect line and the first recv() call is
        # "handshake-time" code; reset() must not appear there.
        before_loop = source.split("while not self._stopping")[0]
        assert "reconnect.reset()" not in before_loop
