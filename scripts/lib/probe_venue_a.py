"""Probe a candidate public VENUE_A endpoint against the semantics it must have.

P12-F2. The Phase 12 field rehearsal could not reach the configured Binance
public endpoint from GitHub Codespaces::

    server rejected WebSocket connection: HTTP 451

451 is "unavailable for legal reasons" — an access decision made by the server
about the caller, not a parser bug. Deciding what to do about it needs two
things this repository could not supply on its own: whether a candidate
endpoint is reachable *from the environment that will run the rehearsal*, and
whether it preserves every semantic the adapter depends on.

This script answers both, for whatever endpoint it is pointed at, and answers
them separately — because "it connected" is not the question. An endpoint that
connects but does not carry contiguous ``U``/``u`` depth sequencing would
silently break ``DepthSynchronizer``, which is worse than the 451.

WHAT IT REFUSES TO DO
=====================
It sends no credential, no API key and no signature, and it touches no private
or order endpoint. If a candidate needs authentication to answer these
questions, it is not a replacement for a public market-data feed and this
script will not help you use it as one.

USAGE
    python scripts/lib/probe_venue_a.py [WS_BASE] [REST_BASE]

Defaults to the currently configured VENUE_A endpoints, so running it with no
arguments reproduces the field failure and shows exactly where it happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass

PASS = "PASS"
FAIL = "FAIL"
#: A requirement that could not be decided because the market did not supply
#: the conditions to decide it. Explicitly NOT a failure: see
#: :func:`classify_trade_liveness`.
INCONCLUSIVE = "INCONCLUSIVE"

#: (requirement, status, detail)
RESULTS: list[tuple[str, str, str]] = []


def record(requirement: str, ok: bool, detail: str = "") -> None:
    record_status(requirement, PASS if ok else FAIL, detail)


def record_status(requirement: str, status: str, detail: str = "") -> None:
    RESULTS.append((requirement, status, detail))
    print(f"  [{status:^12}] {requirement}" + (f" — {detail}" if detail else ""))


def classify_trade_liveness(*, rest_trades_advanced: bool, ws_trade_events: int) -> tuple[str, str]:
    """Decide what zero observed trade events actually means.

    The first Binance.US probe saw 159 depth events, no sequence gaps, and no
    trade events at all in a 65-second window. That is two completely
    different situations wearing the same face:

    * the market was quiet and there was nothing to deliver;
    * trades happened and the trade stream failed to deliver them.

    Only the second is an incompatibility, and the previous probe could not
    tell them apart -- so it reported a failure that might have been a quiet
    market, which is exactly the kind of result that gets waved away. Waiting
    longer is not the fix either: an arbitrary timeout only makes a quiet
    market less likely, never impossible.

    Public REST recent-trades state settles it. Sampled before and after the
    listening window, it is independent evidence of whether the market
    generated trades at all. No credential is involved; this is the same
    public data the stream carries.
    """
    if ws_trade_events > 0:
        # Checked first on purpose. Delivery is the question, and events that
        # arrived answer it whatever the REST sample happened to catch -- a
        # REST snapshot that did not advance while the stream delivered is a
        # sampling race, not a stream defect.
        return (
            PASS,
            f"the stream delivered {ws_trade_events} trade event(s)",
        )
    if not rest_trades_advanced:
        return (
            INCONCLUSIVE,
            "no public trade occurred during the window, so the trade stream "
            "had nothing to deliver; this does not indicate incompatibility",
        )
    return (
        FAIL,
        "public trades occurred during the window but the trade stream "
        "delivered none - TRADE STREAM DELIVERY INCOMPATIBLE",
    )


def configured_endpoints() -> tuple[str, str, list[str]]:
    from core.config.settings import default_venues

    venue = next(v for v in default_venues() if v.name == "VENUE_A")
    return (
        venue.ws_url or "",
        venue.rest_url or "",
        list(venue.symbols),
    )


async def probe_rest(rest_base: str, symbols: list[str]) -> None:
    """The checkpoint the DepthSynchronizer handshake cannot start without."""
    import httpx

    from venues.base.symbols import denormalize
    from venues.venue_a import parser

    for symbol in symbols:
        venue_symbol = denormalize(symbol, parser.SYMBOL_STYLE)
        url = f"{rest_base.rstrip('/')}/api/v3/depth"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params={"symbol": venue_symbol, "limit": 100})
        except Exception as exc:
            record(f"REST depth reachable ({venue_symbol})", False, f"{type(exc).__name__}: {exc}")
            continue

        if resp.status_code != 200:
            record(
                f"REST depth reachable ({venue_symbol})",
                False,
                f"HTTP {resp.status_code}"
                + (" — access refused for this caller/region" if resp.status_code == 451 else ""),
            )
            continue
        record(f"REST depth reachable ({venue_symbol})", True, "HTTP 200")

        try:
            body = resp.json()
        except ValueError:
            record(f"REST depth is JSON ({venue_symbol})", False, "unparseable")
            continue

        record(
            f"REST carries lastUpdateId ({venue_symbol})",
            isinstance(body.get("lastUpdateId"), int),
            str(body.get("lastUpdateId")),
        )
        record(
            f"REST carries both sides ({venue_symbol})",
            bool(body.get("bids")) and bool(body.get("asks")),
            f"{len(body.get('bids', []))} bids / {len(body.get('asks', []))} asks",
        )
        try:
            parser.parse_depth_snapshot(body, symbol, 0)
            record(f"Existing parser accepts checkpoint ({venue_symbol})", True)
        except Exception as exc:
            record(
                f"Existing parser accepts checkpoint ({venue_symbol})",
                False,
                f"{type(exc).__name__}: {exc}",
            )


async def latest_public_trade(rest_base: str, venue_symbol: str) -> tuple[int, int] | None:
    """The most recent public trade id and time, or ``None`` if unavailable.

    ``/api/v3/trades`` is the public recent-trades interface. It takes no key,
    no signature and no account context, and it is the same data the public
    trade stream carries -- which is the point: it can corroborate the stream
    without being the stream.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{rest_base.rstrip('/')}/api/v3/trades",
                params={"symbol": venue_symbol, "limit": 1},
            )
        if resp.status_code != 200:
            return None
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None
        return int(rows[0]["id"]), int(rows[0]["time"])
    except Exception:
        return None


class StreamListener:
    """Consumes the combined stream continuously in the background.

    The listener exists because of a race in the previous probe. That version
    sampled REST *before* opening the socket and again *after* closing it, so
    a trade landing in either uncovered interval produced "REST advanced, no
    WebSocket trades" and was classified a delivery failure — when in fact the
    stream had simply not been listening yet, or had already stopped.

    Here the socket is open and this listener is already draining it before the
    first REST baseline is taken, and it keeps draining until after the last
    REST sample. Every REST-observed trade therefore falls inside a window the
    stream was actually listening through, which is the only condition under
    which "REST advanced but the stream delivered nothing" means anything.
    """

    def __init__(self) -> None:
        self.frames = 0
        self.depth_events = 0
        self.trade_events = 0
        self.trade_ids: list[int] = []
        self.envelope_ok = True
        self.sequencing_ok = True
        self.contiguous = True
        self.gap_detail = ""
        self.error = ""
        self._last_u: dict[str, int] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def consume(self, raw: str) -> None:
        """Fold one raw frame into the counters. Pure; no I/O."""
        message = json.loads(raw)
        if "stream" not in message or "data" not in message:
            self.envelope_ok = False
            return
        self.frames += 1
        data = message["data"]
        event = data.get("e")
        if event == "depthUpdate":
            self.depth_events += 1
            stream = message["stream"]
            if "U" not in data or "u" not in data:
                self.sequencing_ok = False
                return
            if stream in self._last_u and int(data["U"]) != self._last_u[stream] + 1:
                self.contiguous = False
                self.gap_detail = f"{stream}: {self._last_u[stream]} -> {data['U']}"
            self._last_u[stream] = int(data["u"])
        elif event == "trade":
            self.trade_events += 1
            if "t" in data:
                self.trade_ids.append(int(data["t"]))

    async def run(self, connection) -> None:
        """Drain frames until :meth:`stop` is called or the socket ends."""
        while not self._stop:
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=5)
            except TimeoutError:
                continue  # a quiet market is not an error
            except Exception as exc:
                if not self._stop:
                    self.error = f"{type(exc).__name__}: {exc}"
                return
            try:
                self.consume(raw)
            except (ValueError, KeyError, TypeError) as exc:
                self.error = f"unparseable frame: {exc}"
                return


@dataclass
class Observation:
    """What the corroborated observation window saw, and in what order."""

    trade_events: int
    rest_advanced: bool
    baseline: dict[str, int | None]
    final: dict[str, int | None]
    advanced_symbols: list[str]
    unavailable: list[str]
    #: Audit trail of the ordering, so the no-gap property is testable.
    order: list[str]


async def observe_trade_liveness(
    *,
    start_listener,
    stop_listener,
    sample_rest,
    trade_events_so_far,
    window_s: float,
    poll_s: float,
    sleep=asyncio.sleep,
) -> Observation:
    """Run the observation window with no uncovered REST interval.

    The ordering is the whole point, and it is enforced here rather than left
    to the caller to remember:

    1. the listener starts, and is already draining the socket;
    2. only then is the REST baseline taken;
    3. REST is polled while the listener keeps running, so a trade anywhere in
       the window is seen even if it is superseded before the end;
    4. the final REST sample is taken with the listener **still** running;
    5. the delivered-trade count is read;
    6. only now does the listener stop.

    Every dependency is injected so the ordering can be proven without a
    network: see ``tests/unit/test_venue_a_probe_trade_liveness.py``.
    """
    order: list[str] = []

    await start_listener()
    order.append("listener-started")

    baseline = await sample_rest()
    order.append("rest-baseline")

    highest: dict[str, int | None] = dict(baseline)
    elapsed = 0.0
    while elapsed < window_s:
        await sleep(min(poll_s, window_s - elapsed))
        elapsed += poll_s
        polled = await sample_rest()
        order.append("rest-poll")
        for symbol, value in polled.items():
            current = highest.get(symbol)
            if value is not None and (current is None or value > current):
                highest[symbol] = value

    final = await sample_rest()
    order.append("rest-final")
    for symbol, value in final.items():
        current = highest.get(symbol)
        if value is not None and (current is None or value > current):
            highest[symbol] = value

    trade_events = trade_events_so_far()
    order.append("read-ws-trade-count")

    await stop_listener()
    order.append("listener-stopped")

    advanced_symbols = [
        symbol
        for symbol, start in baseline.items()
        if start is not None
        and highest.get(symbol) is not None
        and highest[symbol] != start
    ]
    unavailable = [symbol for symbol, start in baseline.items() if start is None]

    return Observation(
        trade_events=trade_events,
        rest_advanced=bool(advanced_symbols),
        baseline=baseline,
        final=final,
        advanced_symbols=advanced_symbols,
        unavailable=unavailable,
        order=order,
    )


def record_stream_semantics(listener: StreamListener) -> None:
    """Score the depth-stream requirements the adapter depends on."""
    record("Combined-stream envelope present", listener.envelope_ok and listener.frames > 0,
           f"{listener.frames} frames")
    record("Depth events received", listener.depth_events > 0,
           f"{listener.depth_events} depthUpdate")
    record("Depth events carry U/u sequencing",
           listener.depth_events > 0 and listener.sequencing_ok)
    record(
        "Depth update ids are contiguous",
        listener.contiguous and listener.depth_events > 1,
        listener.gap_detail
        or ("too few depth events to judge" if listener.depth_events <= 1 else ""),
    )
    if listener.error:
        record("Stream ran without error", False, listener.error)
    # Trade events are deliberately NOT scored here. Zero of them may mean the
    # market was quiet, which is not an endpoint defect; the verdict comes from
    # the corroborated observation below.
    print(f"    (trade events delivered while listening: {listener.trade_events})")


async def probe_stream_and_trades(
    ws_base: str, rest_base: str, symbols: list[str], *, window_s: float, poll_s: float
) -> None:
    """Open the stream, then corroborate its trade delivery against REST."""
    import websockets

    from venues.base.symbols import denormalize
    from venues.venue_a import parser

    streams = "/".join(parser.stream_names(symbols))
    url = f"{ws_base.rstrip('/')}?streams={streams}"
    print(f"\n  connecting: {url[:110]}{'...' if len(url) > 110 else ''}\n")

    try:
        connection = await asyncio.wait_for(
            websockets.connect(url, ping_interval=15, close_timeout=5).__aenter__(),
            timeout=20,
        )
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        record("WebSocket combined stream reachable", False, text)
        if "451" in text:
            print(
                "\n  HTTP 451 is an access decision about the caller, not a\n"
                "  malformed request. Do not route around it. Either use an\n"
                "  officially supported public endpoint that serves this\n"
                "  environment, or record VENUE_A as an environment blocker.\n"
            )
        return

    record("WebSocket combined stream reachable", True)
    listener = StreamListener()
    venue_symbols = [denormalize(s, parser.SYMBOL_STYLE) for s in symbols]
    task: asyncio.Task | None = None

    async def start_listener() -> None:
        nonlocal task
        task = asyncio.create_task(listener.run(connection))
        # Yield once so the listener is genuinely draining before any REST
        # sample is taken, rather than merely scheduled.
        await asyncio.sleep(0)

    async def stop_listener() -> None:
        listener.stop()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def sample_rest() -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for venue_symbol in venue_symbols:
            latest = await latest_public_trade(rest_base, venue_symbol)
            out[venue_symbol] = latest[0] if latest else None
        return out

    try:
        print(f"  observing for {window_s:.0f}s with the listener active throughout\n")
        observation = await observe_trade_liveness(
            start_listener=start_listener,
            stop_listener=stop_listener,
            sample_rest=sample_rest,
            trade_events_so_far=lambda: listener.trade_events,
            window_s=window_s,
            poll_s=poll_s,
        )
    finally:
        listener.stop()
        with contextlib.suppress(Exception):
            await connection.close()

    record_stream_semantics(listener)

    print("\nTrade-stream liveness (corroborated against public REST)\n")
    for venue_symbol in venue_symbols:
        start = observation.baseline.get(venue_symbol)
        end = observation.final.get(venue_symbol)
        if start is None:
            print(f"    {venue_symbol}: public trade state unavailable")
        elif venue_symbol in observation.advanced_symbols:
            print(f"    {venue_symbol}: public trade id {start} -> {end} while listening")
        else:
            print(f"    {venue_symbol}: no new public trade (id still {start})")

    if observation.unavailable and not observation.trade_events:
        record_status(
            "Trade-stream liveness",
            INCONCLUSIVE,
            f"public trade state unavailable for {', '.join(observation.unavailable)}; "
            "cannot tell a quiet market from a broken stream",
        )
        return

    status, detail = classify_trade_liveness(
        rest_trades_advanced=observation.rest_advanced,
        ws_trade_events=observation.trade_events,
    )
    if status == PASS and listener.trade_ids:
        detail += f" (e.g. trade id {listener.trade_ids[-1]})"
    record_status("Trade-stream liveness", status, detail)


async def main() -> int:
    ws_default, rest_default, symbols = configured_endpoints()
    ws_base = sys.argv[1] if len(sys.argv) > 1 else ws_default
    rest_base = sys.argv[2] if len(sys.argv) > 2 else rest_default
    window_s = float(os.environ.get("TF_PROBE_WINDOW_S", "90"))
    poll_s = float(os.environ.get("TF_PROBE_POLL_S", "10"))

    print("\nVENUE_A public-endpoint probe (P12-F2)")
    print(f"  WebSocket ... {ws_base}")
    print(f"  REST ........ {rest_base}")
    print(f"  Symbols ..... {', '.join(symbols)}")
    print("  Credentials . none sent, and none accepted\n")

    print("REST checkpoint semantics\n")
    await probe_rest(rest_base, symbols)

    print("\nWebSocket stream semantics")
    await probe_stream_and_trades(
        ws_base, rest_base, symbols, window_s=window_s, poll_s=poll_s
    )

    failures = [name for name, status, _ in RESULTS if status == FAIL]
    unresolved = [name for name, status, _ in RESULTS if status == INCONCLUSIVE]

    print("\n" + "=" * 66)
    if failures:
        print(f"NOT USABLE — {len(failures)} requirement(s) unmet:")
        for name in failures:
            print(f"  - {name}")
        print(
            "\nAn endpoint missing any of these is not a drop-in replacement.\n"
            "Do not adopt one merely because it connects, and do not change\n"
            "VENUE_A's symbols to make a different listing fit: BTC-USDT and\n"
            "BTC-USD are different instruments and must stay that way."
        )
        return 1

    if unresolved:
        print("REQUIRED SEMANTICS MET; some checks were inconclusive:")
        for name in unresolved:
            print(f"  - {name}")
        print(
            "\nAn inconclusive check is not a failure and is not a pass. The\n"
            "common case is a quiet market during the window — re-run when the\n"
            "market is active to convert it into a verdict."
        )
        return 2

    print("ALL REQUIREMENTS MET for this endpoint, from this environment.")
    print(
        "Connectivity and semantics are necessary, not sufficient: confirm the\n"
        "endpoint is officially supported and public before configuring it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
