# Phase 1 (TIDAL) Validation

This document records why Phase 1 is, or is not, being declared validated. It
is engineering evidence for a reviewer who was not in the room — not a status
update.

**Bottom line, stated up front:** every gate that can be executed inside this
validation session passed, with zero unexplained failures. One mandatory
gate — the live public-market-data soak (sections 9-15 of the validation
mandate) — **could not be executed**, because this session's sandboxed network
policy denies all outbound connections to Binance and Coinbase, at both the
REST and WebSocket layers, as a deliberate proxy-level policy (not a
transient failure). Because that soak is a required condition for the
`phase1-validated` tag, **the tag was not created** and Phase 1 is not
declared complete by this session. See section 17 and the final report for
the exact reasoning and what is needed to finish.

## 1. Phase 1 scope

Phase 1 hardens TIDAL — the market-data ingestion, book-synchronisation, and
consolidation layer — against a structured audit that found 3 critical, 4
high, 8 medium and 6 low severity findings (`TIDAL-C1..C3`, `TIDAL-H1..H4`,
`TIDAL-M1..M8`, `TIDAL-L1..L6`). Five remediation batches
(`170eea5`, `55baf60`, `d0f0454`, `b28fcdb`, `ffca80f`) closed the C/H/M
findings and most of the L findings; this validation pass closes the
remainder and independently re-verifies all of it against the exact
committed SHA, not against prior reports.

Out of scope for Phase 1: Phase 2 strategy/agent work, any live order
submission path (none exists in this codebase), and anything requiring
exchange credentials (none are ever read).

## 2. Architecture

```
exchange (public, unauthenticated)
   |  WebSocket / REST checkpoint
   v
venue adapter (venues/venue_a, venues/venue_b)
   |  normalised BookDelta / OrderBookSnapshot / TradeEvent / VenueStatus
   v
event bus (core/bus — InMemoryEventBus or RedisStreamBus)
   |  BOOK_SNAPSHOT, BOOK_DELTA, TRADE_PRINT, VENUE_CONNECTED/DISCONNECTED
   v
TIDAL (agents/tidal) — owns every LocalOrderBook, computes DataQuality,
   latency, clock skew; consolidates per-venue state into ConsolidatedView
   |  MARKET_STATE, AGENT_OPINION, BOOK_RESYNC_REQUESTED, SYSTEM_EVENT
   v
MarketState (core/models/market.py) — the one published view everything
   downstream (NORO, ZEPHR, LUMEN, the orchestrator, VenueRouter) reads
```

Recovery is a bus round-trip, not a direct call: TIDAL never holds an
adapter reference. It publishes `BOOK_RESYNC_REQUESTED`; `ResyncBridge`
(the only place that knows both TIDAL and the adapters) forwards it to
whichever adapter owns that venue.

## 3. Audit findings table

| ID | Severity | Description | Resolution | Commit | Validation evidence |
|---|---|---|---|---|---|
| C1 | Critical | Binance depth stream has no base state; a book was never actually built, only diffed against nothing. | REST checkpoint bootstrap joined onto buffered stream updates via the documented `U <= lastUpdateId+1 <= u` handshake. | `170eea5` | `tests/unit/test_binance_sync.py` |
| C2 | Critical | A detected sequence gap produced no working recovery — the request never reached the feed and the book never recovered. | `BOOK_RESYNC_REQUESTED` bus round-trip + `ResyncBridge` + per-symbol `DepthSynchronizer.request_resync`. | `170eea5` | `tests/unit/test_resync_flow.py` |
| C3 | Critical | The Binance symbol formatter silently substituted USDT for USD, so BTC-USD priced as BTC-USDT — a stablecoin basis mistaken for a bitcoin edge. | Removed `_QUOTE_ALIASES`; distinct settlement assets (USD/USDT/USDC/BUSD) are never aliased; symbol-equality guard in `find_dislocation`. | `55baf60` | `tests/unit/test_instrument_identity.py` |
| H1 | High | Binance `U`/`u` treated as a point, not the range they are, making the post-snapshot handshake unsatisfiable for the majority of legitimate first messages. | Range-sequenced (`first_sequence..sequence`) handling with correct straddle tolerance. | `170eea5` | `tests/unit/test_binance_sync.py` |
| H2 | High | Coinbase `level2_batch` had no effective gap detection, and a docstring claiming a "timestamp monotonicity fallback" was false — no such check existed. | Honest `_check_unordered`: drops only provably-stale (older-timestamp) updates; documents, rather than fabricates, what the public feed can and cannot prove. | `d0f0454` | `tests/unit/test_coinbase_continuity.py` |
| H3 | High | `DataQuality` was computed from local receipt time alone; a feed delivering packets on schedule describing stale exchange data looked FRESH. Clock skew was clamped into "latency," hiding a broken clock. | FRESH requires both local-silence age and exchange-observation age within bound; skew handled by a separate, signed `clock_skew_ms` with its own tolerance and health/system-event surfacing. | `d0f0454` | `tests/unit/test_market_freshness.py` |
| H4 | High | A multi-leg opportunity's "data age" used the newest timestamp across all market data, so one stale leg could hide behind one fresh, unrelated leg. | `MarketState.source_data_timestamp_for(legs)` — oldest observation among only the legs actually involved. | `d0f0454` | `tests/unit/test_data_age.py` |
| M1 | Medium | `LocalOrderBook` destructively trimmed storage to `max_depth` on every write; a level just outside the top-N was discarded even though the venue never deleted it. | Storage keeps the full book; only `levels()` (a read) trims to `max_depth`. | `b28fcdb` | `tests/unit/test_depth_storage.py` |
| M2 | Medium | Coinbase side parsing defaulted anything not literally `"buy"` to a sell, silently misclassifying malformed sides. | Explicit side validation; unrecognised values raise `MalformedVenueMessage`. | `b28fcdb` | `tests/unit/test_coinbase_side_validation.py` |
| M3 | Medium | `PriceLevel`/`TradeEvent` accepted NaN/±Infinity, which could propagate into every downstream calculation. | `allow_inf_nan=False` on all market-data numeric fields; malformed levels reject the whole message rather than being silently skipped. | `b28fcdb` | `tests/unit/test_numeric_validation.py` |
| M4 | Medium | No structured containment for a malformed venue message — one bad frame could either crash the session or be silently ignored with no visibility. | `MalformedVenueMessage`/`ContinuityUncertain` + per-venue containment policy (Binance: per-symbol resync; Coinbase: full reconnect) + structured `MalformedContext` diagnostics. | `b28fcdb` | `tests/unit/test_malformed_message_isolation.py` |
| M5 | Medium | Reconnect backoff reset immediately after the WebSocket handshake succeeded, before any real data — a socket that connects and instantly closes reconnected at ~1Hz forever. | `mark_healthy()` resets backoff only once a real market-data message (not a heartbeat) is parsed. | `b28fcdb` | `tests/unit/test_reconnect_backoff.py` |
| M6 | Medium | Cross-venue consolidation reported a single usable venue's own bid/ask as `best_bid`/`best_ask`/`cross_venue_spread_bps` — a fabricated "cross-venue" fact from one venue. | Those fields require `len(usable) >= 2`; `reference_price` remains valid (and documented as such) for one contributor. | `ffca80f` | `tests/unit/test_consolidated_view.py` |
| M7 | Medium | `InMemoryEventBus`'s pending queue was unbounded; a slow consumer could grow it without limit. A later review found the fix's own admission check racy under concurrent publishers through async middleware. | Hard ceiling (`max_pending` + `cascade_reserve`) enforced by a synchronous check-and-reserve under one lock; middleware runs only after a slot is reserved, so recording and enqueueing can never disagree; reservations released on middleware failure/cancellation. | `ffca80f` | `tests/unit/test_bus_backpressure.py`, `tests/unit/test_bus_hard_bound.py` |
| M8 | Medium | Coinbase timestamp parsing guessed numeric formats for non-string input, an undocumented behaviour. | `parse_iso_ms` requires a string; anything else falls back to `received_ts` with no guessing. | `b28fcdb` | `tests/unit/test_coinbase_timestamp_parser.py` |
| L1 | Low | `_quality()` defaulted an unknown venue's connection state to *connected*; the published `VenueMarketState.connected` field defaulted the same lookup to *disconnected* — two answers from one dict. | Both default to `False`: unknown connection state is never treated as connected. | `32e67af` | `tests/unit/test_connection_state_fail_closed.py` |
| L2 | Low | `LocalOrderBook.invalidate(reason)` accepted a diagnostic reason and discarded it — every invalidation looked identical from the outside. | Bounded `invalid_reason` field, set by every invalidation path (disconnect, sequence gap, storage overflow), cleared on the next successful checkpoint. | `32e67af` | `tests/unit/test_invalidation_reason.py` |
| L3 | Low | `updates_since_checkpoint` was written on every snapshot/delta but had no consumer anywhere in the codebase. | Removed entirely (field, increments, no test depended on it). | `32e67af` | grep-verified absence; full suite green |
| L4 | Low | Reported concern: raw negative latency (exchange timestamp after receipt) might be silently hidden. | Verified already correct as of Batch 3 (`d0f0454`): `clock_skew_ms` can be negative and is exposed as-is; the economic `latency_ms` estimate is floored at 0 for downstream cost modelling; large future skew fails freshness. No code change — closed by inspection and existing tests. | `d0f0454` (pre-existing) | `tests/unit/test_market_freshness.py::TestLatencyDoesNotHideSkew` |
| L5 | Low | `VenueRouter._levels()` returned book levels from any state with a populated book, regardless of `DataQuality` — a STALE or UNAVAILABLE book could still be routed against. | Defers to canonical `DataQuality.is_usable` before returning any levels; no second freshness model invented. | `32e67af` | `tests/unit/test_venue_router_quality_gate.py` |
| L6 | Low | Three developer-tooling tests were stale against the current devcontainer (build-based, not image-based, since the Codespaces Docker-in-Docker fix `e117edb`) and against Docker daemon availability. | Tests rewritten to check the real relationship (devcontainer's `build.dockerfile` resolves to the `Dockerfile` that pins Python 3.12) and to distinguish "Docker unavailable" from "script logic is wrong" via an explicit skip. | `32e67af` | `tests/contract/test_developer_tooling.py` — 0 failures |

## 4. Binance synchronisation algorithm

1. On subscribe, the adapter buffers streamed `depthUpdate` messages per
   symbol (`DepthSynchronizer`, bounded by `depth_sync_max_buffer`) while a
   REST checkpoint (`GET /api/v3/depth`) is fetched concurrently.
2. The checkpoint's `lastUpdateId` (`L`) is the book's initial `sequence`.
3. The buffered stream is replayed: the first applied update must satisfy
   `U <= L + 1 <= u` (range semantics — `U`/`u` are the inclusive span of
   update IDs one message covers, not a single point). A message whose whole
   span is behind `L` is dropped as already-covered; one that starts past
   `L + 1` is a real gap.
4. After the handshake, ordinary range continuity applies: each message's
   `U` must not exceed `held + 1`.
5. A gap raises `BookDesyncError`, marks the book unsynchronised, and (rate
   limited per book) publishes `BOOK_RESYNC_REQUESTED`, which
   `VenueAAdapter.request_resync` answers by resetting that one symbol's
   `DepthSynchronizer` and re-fetching a checkpoint — never touching any
   other symbol on the same connection.
6. Backoff (`ReconnectPolicy`) only resets once a real market-data message
   (not merely a successful handshake or a heartbeat) is parsed
   (`mark_healthy`), so a socket that connects and immediately closes backs
   off exponentially instead of reconnecting at ~1 Hz forever.

## 5. Coinbase continuity model

Coinbase's public `level2_batch` channel carries **no sequence number** on
either the initial `snapshot` or subsequent `l2update` messages. This is
stated plainly rather than worked around: **no sequence number is invented**,
and the parser's prior docstring claiming a "timestamp monotonicity
fallback" that did not exist was corrected (`TIDAL-H2`).

What the client can actually prove, and no more: an incoming update whose
`exchange_ts` is older than the newest one already applied is *known* to be
information already superseded — applying it would regress the book to a
state older than what is already held, which is strictly worse than doing
nothing. Such an update is dropped (`out_of_order_dropped`), silently and
without escalation, because an old timestamp is not evidence of a **gap** —
treating it as one would fabricate a confidence the signal cannot support.

Because no gap-proof exists on this channel at all, the only verified
recovery for any book-affecting malformed message, or for a disconnect, is a
**full reconnect** — which re-subscribes and re-snapshots every symbol on
that connection, not just the affected one. This blast radius is documented,
not hidden, at every call site (`VenueBAdapter._contain`,
`VenueBAdapter.request_resync`).

## 6. Timestamp / freshness semantics

Three timestamps, one meaning each (milliseconds): `exchange_ts` (when the
exchange observed it), `received_ts`/`last_update_ts` (when this process
received it), `as_of` (when TIDAL assembled the state being read).

`DataQuality.FRESH` requires **all** of:
- `book.synced`, not crossed, both sides non-empty;
- the venue's connection state known and `True` (TIDAL-L1);
- `as_of - last_update_ts <= max_data_age_ms` (local silence);
- `as_of - exchange_ts <= max_data_age_ms` (exchange-observation age);
- `received_ts - exchange_ts >= -max_clock_skew_ms` (the exchange timestamp
  is not implausibly ahead of receipt).

Between `max_data_age_ms` and `3x` it: `DEGRADED`. Beyond `3x`: `STALE`.
Any of the above outright failing (unsynced, crossed, disconnected, or a
clock-skew violation): `UNAVAILABLE`. `DataQuality.is_usable` is `True` only
for `FRESH` — this is the single canonical freshness gate; `VenueRouter`
(TIDAL-L5) defers to it rather than inventing a second one.

## 7. Instrument identity rules

Settlement assets (`USD`, `USDT`, `USDC`, `BUSD`) are distinct and never
aliased by any symbol formatter (`TIDAL-C3`). `BTC-USD != BTC-USDT`,
`BTC-USD != BTC-USDC`, `BTC-USD != BTC-BUSD` — enforced structurally (no
`_QUOTE_ALIASES`-style rewrite exists anywhere in `venues/base/symbols.py`)
and by `find_dislocation`'s explicit symbol-equality guard.

## 8. Event-bus backpressure / hard-bound model

`InMemoryEventBus` has a hard, finite ceiling on admitted-but-not-yet-
dispatched events: `max_pending` (default 10,000, external publishers) +
`cascade_reserve` (default 1,000, reentrant/cascade publishers) =
`hard_ceiling`. Both are enforced against `queue_depth + reserved_pending`,
not `queue_depth` alone, closing a race where concurrent publishers could
jointly exceed the bound while each was independently past the check but
still inside async middleware.

An external publisher blocks (real backpressure) at `max_pending`. A
reentrant publish (from inside a handler the bus is currently dispatching)
cannot block without deadlocking the one task capable of freeing capacity,
so instead it fails closed with `CascadeCapacityExceeded` once
`cascade_reserve` is exhausted — counted, never silent.

Recording (the event-store `Recorder`, attached as bus middleware) runs only
after a capacity slot is reserved, and the reservation is released if
middleware raises or the publishing task is cancelled — so an event can
never be durably recorded without also being accepted for dispatch, closing
a live/replay divergence.

`RedisStreamBus` bounds its stream by `MAXLEN`/`approximate` trimming at the
transport layer instead; this clause does not mandate one mechanism, only
that neither implementation grows without limit or loses an accepted event
silently.

## 9. Book-storage safety model

`LocalOrderBook.max_levels_per_side` (from `VenueConfig.max_book_levels_per_side`,
validated `>= book_depth_levels` and `>= book_depth_levels * 2` to cover
Binance's checkpoint-depth doubling) is a hard ceiling on **stored** levels
per side — distinct from `max_depth`, the **read-time** trim `TIDAL-M1`
made the only form of trimming. Exactly at the bound is a normal book; one
level past it fails the whole book closed (`BookOverflowError`), never
evicts individual levels to fit.

A delta's possible overshoot is checked *after* applying it (cheap and safe,
since one venue message carries at most a few thousand levels). A snapshot
is checked *before* anything is copied into `self.bids`/`self.asks` — a
checkpoint can legitimately be enormous, and a 100x-oversized one must never
become resident in authoritative storage even momentarily, or the bound
would be defeated by the exact case it exists to catch. On snapshot
overflow, the book's prior state (if any) is left completely untouched.

Binance overflow recovers via the existing per-symbol resync path; Coinbase
overflow via the existing full-reconnect path (documented blast radius, per
section 5). A persistent (>=3 consecutive) snapshot-overflow streak on one
book escalates to a distinct `BOOK_OVERFLOW_PERSISTENT` health event without
changing retry frequency (already rate-limited), surfacing a likely
configuration mismatch rather than looking like an ordinary one-off gap.

## 10. Malformed-message containment

`MalformedVenueMessage` (raised by a parser, carrying venue/symbol/message
type) and `ContinuityUncertain` (forces a full reconnect) are caught at the
adapter boundary, never left to crash the session. Binance: a malformed
`depthUpdate` triggers that one symbol's resync; a malformed trade does not.
Coinbase: a malformed `snapshot`/`l2update` forces a full reconnect; a
malformed `match` does not. Every containment decision is recorded in a
structured, bounded `MalformedContext` (venue, symbol, message type, detail,
whether it invalidated a book, whether it requested recovery) rather than
only a log line.

## 11. Live venue configuration

| Venue | Adapter | Symbols |
|---|---|---|
| VENUE_A | Binance (`binance_public`) | `BTC-USDT`, `ETH-USDT` |
| VENUE_B | Coinbase (`venue_b`) | `BTC-USD`, `ETH-USD` |

## 12. Why the current live configuration creates no same-instrument cross-venue pair

Binance lists BTC/ETH only against USDT on this platform's configured
symbols; Coinbase lists them only against USD. Since `BTC-USDT != BTC-USD`
(section 7) is a structural, enforced invariant, `consolidate()` never
receives two states for the same canonical symbol across these two venues —
there is no legitimate same-instrument pair to consolidate. **This is
correct, expected behaviour, not a defect to "fix" by aliasing stablecoins.**
A live soak against this configuration should therefore observe **zero**
same-instrument cross-venue opportunities for BTC or ETH — that is what
success looks like here, not an absence of activity to explain away.

## 13. Simulated-mode purpose

`simulated_venues()` deliberately gives both venues the *same* instruments
(`BTC-USD`, `ETH-USD` on both) so the full strategy pipeline — including
genuine cross-venue consolidation and opportunity detection — can be
exercised end-to-end offline, deterministically, without any network
dependency. It is a test configuration, not a claim about what the real
exchanges list.

## 14. Paper-only security boundary

`PaperExecutor` is the only execution implementation in this codebase; no
other execution path exists to enable. No adapter, anywhere, holds
credentials, signs a request, or calls an authenticated endpoint — the
`VenueAdapter` interface declares no method capable of submitting, amending,
or cancelling a real order, and `VenueCapabilities.order_submission` /
`.authenticated` are structurally `False`. `TF_MODE` other than `paper` is
rejected at settings load. `TF_FEED=live` widens only which public
market-data source is read; it has no effect on execution. This validation
session swept every diff for credentials, authenticated calls, and
order-submission verbs: none found (matches every prior batch's sweep).

## 15. Deterministic test results

All results below were generated in this validation session against the
exact commit under test, not carried over from prior reports.

**Against `ffca80f`** (baseline check, section 3 of the mandate, before any
cleanup in this session):
- `tests/contract/test_event_bus_contract.py`: 27 passed, 27 skipped (Redis, no server reachable at that point)
- Full mandated Phase 1 focused list (19 files): 426 passed
- `ruff check .`: all checks passed
- `mypy --ignore-missing-imports core`: no issues found in 24 source files
- `pytest -q tests`: 1115 passed, 2 failed (the two devcontainer TIDAL-L6 tests, since not yet fixed at this point), 69 skipped

**After the TIDAL-L1/L2/L3/L5/L6 cleanup, commit `32e67af`:**
- `tests/contract/test_event_bus_contract.py`: 27 passed, 27 skipped (Redis, in-memory-only run)
- `tests/contract/test_developer_tooling.py`: **69 passed, 0 failures** (run 3x for stability against the real Docker daemon)
- Full mandated Phase 1 focused list, now including the 3 new L1/L2/L5 test files (22 files): 450 passed
- `ruff check .`: all checks passed
- `mypy --ignore-missing-imports core`: no issues found in 24 source files
- `mypy --ignore-missing-imports agents/tidal execution/router` (supplementary, "if practical"): 5 pre-existing errors, confirmed present identically on `ffca80f` before this session's changes (see section 21) — not introduced by Phase 1 remediation
- `pytest -q tests`: **1141 passed, 0 failed, 69 skipped, 1 warning** (333.63s)

Skips are all Redis-variant contract tests in runs where no `TF_TEST_REDIS_URL`
was set (expected — see section 16 for the separate real-Redis run) plus a
small number of environment-gated tests (e.g. requiring a live Docker
daemon for specific volume-preservation assertions) that ran and passed
once the daemon was started in this session (section 16), not skipped in
the final counts above where they could run.

## 16. Redis / Postgres / Docker verification results

The containerised path (`docker compose up --build`, `./verify-phase0.sh`)
remains blocked in this sandboxed session for the **same, already-documented
reason Phase 0 recorded** (`docs/docker-verification.md`): the egress proxy
denies Docker Hub's blob CDN (`production.cloudfront.docker.com` → 403),
so no base image layer (`python:3.12-slim`, `redis:7-alpine`,
`postgres:16-alpine`) can be pulled. This is an environment policy limit,
not a defect, and was not worked around.

To still obtain **real** (non-mocked) infrastructure verification, this
session used the native `redis-server` (7.0.15) and `postgresql-16` (16.13)
packages already installed in the sandbox — the same actual server
software, not a substitute deployment system, just not containerised:

- **Redis**: started on port 6399. `tests/contract/test_event_bus_contract.py`
  run with no skip condition: **54 passed** (27 `memory` + 27 `redis`,
  every clause of the contract against a real Redis server).
- **PostgreSQL**: started (system service), `trading` role and
  `trading_floor` database created, the checked-in migration
  (`storage/migrations/001_initial.sql`) applied cleanly.
  `tests/contract/test_event_store_contract.py` run against
  `TF_TEST_POSTGRES_DSN`: **113 passed**, including the full
  `TestPostgresSpecifics` class (JSONB payload column, BIGINT timestamps,
  concurrent-writer ordering, server-side rejection of non-finite numbers,
  reconnect durability) and `TestOpeningAnOlderDatabase` (migration
  upgrade path).
- **API / end-to-end**: the installed `trading-floor` entrypoint started
  directly (no container) with `TF_MODE=paper`, `TF_FEED=simulated`,
  `TF_BUS=redis` (pointed at the port-6399 instance), `TF_STORAGE_BACKEND=postgres`
  (pointed at the native instance). `/health` returned `"mode":"PAPER"` with
  8 of 9 components `HEALTHY` (LUMEN `DEGRADED` on provider failures —
  expected with no `TF_INTELLIGENCE_PROVIDER` configured, unrelated to
  Phase 1). After ~13 seconds of simulated trading: **2,107 events** in the
  Redis stream `tf:events`, **1,889 rows** in the Postgres `events` table
  for that session — real event-bus traffic and real persistence, observed
  directly (`XLEN`, `SELECT COUNT(*)`), not inferred. Logs swept for
  credentials/auth/order-submission activity: none found. Process stopped
  cleanly.

This is **more** direct infrastructure verification than Phase 0 achieved in
its own equivalent environment (Phase 0's Postgres verification ran
elsewhere; this session verified both Redis and Postgres, plus a live
end-to-end paper run, in-session). It does not substitute for actually
building and running the `docker-compose.yml` stack, which remains
"PENDING EXTERNAL VERIFICATION" for the identical reason Phase 0 already
recorded.

## 17. Live-soak results

**NOT PERFORMED. This is the reason Phase 1 is not tagged validated.**

Before starting the soak, outbound connectivity to both venues was tested
directly:

```
$ curl https://api.binance.com/api/v3/time        → CONNECT tunnel failed, HTTP 403
$ curl https://api.exchange.coinbase.com/time     → CONNECT tunnel failed, HTTP 403
$ websocket wss://stream.binance.com:9443/...     → tunnel closed after handshake, 1006
$ websocket wss://ws-feed.exchange.coinbase.com   → CONNECT tunnel failed, HTTP 403
```

The proxy's own status endpoint confirms these are deliberate policy
denials, not transient errors:

```json
{"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "api.binance.com:443"}
{"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "api.exchange.coinbase.com:443"}
{"kind": "ws_closed_mid_exchange", "detail": "tunnel closed (code 1006, ...) after 6s", "host": "stream.binance.com:9443"}
{"kind": "connect_rejected", "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)", "host": "ws-feed.exchange.coinbase.com:443"}
```

This is a security boundary of the harness this validation ran inside, not
something to disable, route around, or reinterpret. No `TF_FEED=live` run
was attempted or fabricated. No soak duration, REST cross-check, or
reconnect exercise was performed, and none of section 11's observability
data exists for a live run in this session. Reporting a PASS here without
this data would be exactly the false-completeness this validation mandate
explicitly warns against.

**What is needed to complete this section:** run this exact validation
mandate's sections 9-15 from an environment whose network egress permits
outbound HTTPS/WSS to `api.binance.com`, `stream.binance.com`,
`api.exchange.coinbase.com`, and `ws-feed.exchange.coinbase.com`, against
commit `32e67af` (`PHASE1_SOAK_CANDIDATE_SHA`) or the tip of
`phase1-tidal-remediation` at that time, with no other changes required —
every deterministic and infrastructure gate ahead of the soak is already
green.

## 18. Reconnect test results

Not performed — depends on the live soak (section 17).

## 19. Independent REST cross-check results

Not performed — depends on live connectivity (section 17).

## 20. Observed resource metrics

From the in-session paper/simulated end-to-end run (section 16), not a live
soak: 8/9 components HEALTHY within ~13s of startup, 2,107 bus events and
1,889 persisted events with no errors, no exceptions, no crash, clean
shutdown. No live-soak resource curve (uptime, RSS over 20 minutes,
queue-depth/reservation time series, per-symbol sync timings) exists — that
data can only come from section 17.

## 21. Accepted residual limitations

**Correctness limitations:**
- Coinbase's public `level2_batch` channel provides no sequence number and
  therefore no proof that every logical update was received; a dropped
  packet the exchange never resends is undetectable by protocol trust alone
  (documented at length in section 5 and in `agents/tidal/book.py`'s
  `_check_unordered`). This is a property of the public feed, not a gap in
  this implementation, and is not treated as solved.
- `mypy --ignore-missing-imports agents/tidal execution/router` (run as
  supplementary diligence beyond the mandated `mypy core`) surfaces 5
  pre-existing type-narrowing errors (`agents/tidal/book.py`,
  `agents/tidal/agent.py`) — all `float | None` fields used in arithmetic
  the surrounding code's own invariants guarantee non-`None`, which mypy
  cannot infer from a Pydantic model alone. Confirmed identical (same
  count, same shape) on `ffca80f` before this session touched either file
  — pre-existing, not introduced by this validation, and outside the
  mandated `core`-only mypy gate. Recommended for Phase 2 cleanup
  consideration, not blocking.

**Operational limitations:**
- The containerised Docker Compose stack cannot be built or run in this
  sandboxed session (Docker Hub blob CDN blocked) — identical to Phase 0's
  own documented status. Substituted with native-service verification
  (section 16), which is real but not the containerised path itself.
- The live public-market-data soak (sections 9-15 of the mandate) could not
  be run in this session for the same network-policy reason (section 17).
  This is the blocking item for the `phase1-validated` tag.

**Strategy limitations:**
- The current live venue configuration (Binance USDT-quoted, Coinbase
  USD-quoted) structurally produces zero same-instrument cross-venue
  opportunities for BTC/ETH (section 12). This is correct and by design,
  not a limitation to remediate — noted here only so a reviewer does not
  mistake an eventual soak's zero-opportunity count for a defect.

## 22. Validated commit SHA and date

- `PHASE1_SOAK_CANDIDATE_SHA`: `32e67af743f1512e52a7ab711b99403129b05e2f`
  (branch `phase1-tidal-remediation`) — deterministic and infrastructure
  gates green; live-soak gate not attempted.
- No `phase1-validated` tag exists as of this document. See the final
  validation report for the exact reasoning.
- Validation session date: 2026-09-04.
