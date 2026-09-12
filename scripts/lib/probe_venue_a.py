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
import json
import sys

RESULTS: list[tuple[str, bool, str]] = []


def record(requirement: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((requirement, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {requirement}" + (f" — {detail}" if detail else ""))


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


async def probe_ws(ws_base: str, symbols: list[str], *, frames: int = 40) -> None:
    """The combined stream, its event shape, and its sequence contiguity."""
    import websockets

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
    depth_seen = 0
    trade_seen = 0
    last_u: dict[str, int] = {}
    contiguous = True
    gap_detail = ""

    try:
        for _ in range(frames):
            raw = await asyncio.wait_for(connection.recv(), timeout=20)
            message = json.loads(raw)
            # Combined-stream envelope: {"stream": ..., "data": {...}}
            if "stream" not in message or "data" not in message:
                record("Combined-stream envelope present", False, str(message)[:80])
                break
            data = message["data"]
            event = data.get("e")
            if event == "depthUpdate":
                depth_seen += 1
                stream = message["stream"]
                if "U" not in data or "u" not in data:
                    record("Depth events carry U/u sequencing", False, str(sorted(data))[:80])
                    contiguous = False
                    break
                if stream in last_u and int(data["U"]) != last_u[stream] + 1:
                    contiguous = False
                    gap_detail = f"{stream}: {last_u[stream]} -> {data['U']}"
                last_u[stream] = int(data["u"])
            elif event == "trade":
                trade_seen += 1
    except Exception as exc:
        record("Stream delivered frames", False, f"{type(exc).__name__}: {exc}")
    finally:
        await connection.close()

    total = depth_seen + trade_seen
    record("Combined-stream envelope present", total > 0, f"{total} frames")
    record("Depth events received", depth_seen > 0, f"{depth_seen} depthUpdate")
    record("Trade events received", trade_seen > 0, f"{trade_seen} trade")
    record("Depth events carry U/u sequencing", depth_seen > 0 and bool(last_u))
    record(
        "Depth update ids are contiguous",
        contiguous and depth_seen > 1,
        gap_detail or ("too few depth events to judge" if depth_seen <= 1 else ""),
    )


async def main() -> int:
    ws_default, rest_default, symbols = configured_endpoints()
    ws_base = sys.argv[1] if len(sys.argv) > 1 else ws_default
    rest_base = sys.argv[2] if len(sys.argv) > 2 else rest_default

    print("\nVENUE_A public-endpoint probe (P12-F2)")
    print(f"  WebSocket ... {ws_base}")
    print(f"  REST ........ {rest_base}")
    print(f"  Symbols ..... {', '.join(symbols)}")
    print("  Credentials . none sent, and none accepted\n")

    print("REST checkpoint semantics\n")
    await probe_rest(rest_base, symbols)
    print("\nWebSocket stream semantics")
    await probe_ws(ws_base, symbols)

    failures = [name for name, ok, _ in RESULTS if not ok]
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
    print("ALL REQUIREMENTS MET for this endpoint, from this environment.")
    print(
        "Connectivity and semantics are necessary, not sufficient: confirm the\n"
        "endpoint is officially supported and public before configuring it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
