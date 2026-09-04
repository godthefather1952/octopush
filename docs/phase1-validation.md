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

A subsequent polish pass (section 23) fixed three accounting/observability
defects found in a simulated paper run after the above validation, re-ran
every deterministic gate (including real Redis and Postgres, this time both
exercised in the same session as the full suite), and re-checked live
connectivity once more. The live-soak blocker in section 17 is unchanged —
still a proxy-level policy denial, re-confirmed rather than assumed — so the
polish pass does not change this document's bottom line: no
`phase1-validated` tag, Phase 1 code/data path is polished and green, live
public-feed soak remains externally blocked.

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

## 23. Phase 1 polish (accounting and observability fixes)

A simulated paper run after the validation above surfaced three defects in
how P&L and risk were computed and reported — not in the underlying cash or
position accounting, which was already correct, but in the derived values
built on top of it. This section documents each defect, its fix, and the
proof that the fix is correct, plus a dashboard wording fix and a full
re-verification.

### 23.1 Portfolio P&L semantics (`core/models/portfolio.py`)

**Symptom:** a flat account with $100,430.48 equity (a $430.48 gain over the
$100,000 initial balance, after $4,954.40 in fees) reported net P&L of
$10,339.27 — more than 20x the account's actual gain.

**Root cause:** `PortfolioState.gross_pnl` and `.net_pnl` had the fee
adjustment backwards:

```python
gross_pnl = realized_pnl + unrealized_pnl + fees_paid   # added fees BACK on
net_pnl   = realized_pnl + unrealized_pnl                # never subtracted them
```

`PositionState.apply()`'s realized-P&L return value, and `unrealized_pnl`,
are both pre-fee trading P&L; fees are tracked separately in `fees_paid` and
are already subtracted from cash exactly once, via `FillEvent.cash_delta`.
The old `net_pnl` was therefore identical to pre-fee P&L (never subtracting
fees at all), and `gross_pnl` overstated even that by adding the fee total
back on top a second time. Cash accounting itself was never wrong — only
these two read-only properties built on top of it.

**Fix:**

```python
gross_pnl = realized_pnl + unrealized_pnl     # before fees
net_pnl   = gross_pnl - fees_paid             # after fees -- reality
```

**Proof:** `tests/unit/test_portfolio_pnl_semantics.py` (9 tests) and one
added test in `tests/integration/test_api.py`
(`test_state_pnl_reconciles_to_the_corrected_portfolio_formula`) prove, with
concrete numeric expectations: a flat profitable round trip, a flat losing
round trip, an open position (gross includes unrealized, net still
subtracts the full fee total exactly once), multiple partial fills, mixed
maker/taker fees, zero fees, no double subtraction between cash and
`net_pnl`, that the Prometheus `tf_gross_pnl`/`tf_net_pnl` gauges and the
`/api/state` response use the corrected formula, and that MARIN's
`recompute_from_fills` reconciliation still agrees. For every flat case,
`equity - initial_balance == net_pnl` holds to floating-point tolerance —
the identity the old formula broke. `/api/state` and the dashboard needed no
code change: both read `PortfolioState.gross_pnl`/`.net_pnl` directly, so
the fix propagates automatically.

### 23.2 Risk-utilization freshness (`agents/rune/core.py`,
`apps/orchestrator/orchestrator.py`)

**Symptom:** after a position closed (portfolio gross/net exposure back to
$0), the dashboard's risk-utilization panel kept showing the position's
exposure as if it were still open (gross ≈ $50,025, unhedged ≈ $12).

**Root cause:** `state.risk_utilization` was written only inside
`Orchestrator._risk_check()` — invoked only while evaluating a *new* trade
intent. It described the portfolio as of the last new-trade risk evaluation,
not the portfolio that exists right now; once a position closed with no new
opportunity following it, the stale snapshot could persist indefinitely.

**Fix:** `RuneCore.utilization()` was narrowed to take exactly the inputs it
uses — `(portfolio, unhedged_notional, strategy_exposure)` — rather than a
full `RiskContext` (which also carries kill-switch/health/consensus fields
this calculation never reads), making it callable from anywhere with just
current state. `Orchestrator._refresh_risk_utilization()` now calls it
unconditionally on every tick, immediately after the portfolio is marked, in
addition to `_risk_check()`'s own call on the same method for new-trade
evaluations. One calculation, two call sites — no second implementation was
introduced.

**Proof:** `tests/integration/test_risk_utilization_freshness.py` (6 tests):
utilization reflects an open position; the very next tick after a close
shows zero portfolio exposure *and* zero risk-utilization exposure, with no
new trade or risk evaluation involved; unhedged notional tracks a position
through partial closes down to zero; `/api/state`'s `risk` block matches the
live snapshot; a 300-tick run shows no regression in RUNE's own gating
(limits never breached). Mutation-tested: removing the
`_refresh_risk_utilization` call reproduces 3 of the 6 failures; restoring
it (verified byte-identical via `diff`) returns all 6 to green.

### 23.3 Trade-attribution P&L leakage (`monitoring/attribution.py`,
`apps/orchestrator/orchestrator.py`)

**Symptom:** the attribution feed showed repeated trade rows of
$2,000-$2,700 realized P&L each, while total account equity had grown by
only about $430 over the whole session — attribution rows summed to many
times the account's actual result.

**Root cause, confirmed by reading the code (not assumed):**
`Orchestrator._finish_attribution()` computed a closing opportunity's
realized P&L by reading `PositionState.realized_pnl` for each leg's
venue:symbol — but that field is a **lifetime-cumulative** counter per
venue:symbol, not a per-opportunity one. A second (or later) opportunity
that traded the same venue:symbol therefore inherited every prior
opportunity's cumulative realized contribution on top of its own, and the
inherited amount only grew as more same-symbol opportunities closed —
exactly the reported symptom.

**Fix (fill-grounded, narrowest correct mechanism — no portfolio-lot
accounting framework was built):** `Orchestrator._on_fill()` now tracks a
`dict[str, float]` baseline of the last-observed cumulative
`PositionState.realized_pnl` per venue:symbol
(`Orchestrator._realized_baseline`). On every fill, the delta since that
key's last-seen value is computed — the isolated, per-fill realized
contribution — and the baseline is advanced **unconditionally**, whether or
not an attribution builder claims the fill, so a hedge or orphan fill on the
same venue:symbol can never corrupt a later opportunity's delta. Only when a
builder exists for the fill's own `correlation_id` is the delta fed to it,
via a new `AttributionBuilder.add_realized(delta)` method, accumulating into
a renamed `realized_pnl_gross` field (pre-fee, mirroring the section 23.1
terminology). `AttributionBuilder.build()` now subtracts `fees` itself,
moving that computation out of `_finish_attribution`, which no longer reads
`portfolio.positions` at all.

This design is also correct for **overlapping** opportunities on the same
venue:symbol (two opportunities open concurrently, sharing one average-cost
position): because attribution is keyed by which fill produced the P&L, not
by a snapshot taken at some later close time, each opportunity still gets
exactly its own contribution regardless of interleaving. No case was
identified where exact attribution is impossible with fills alone, so no
"fail closed / unsupported" path was needed.

**Proof:** `tests/integration/test_trade_attribution_isolation.py` (10
tests): two sequential profitable opportunities on the same venue:symbol
(the second does not include the first); a profitable trade followed by a
loss; five sequential same-symbol opportunities each reporting the same
constant result regardless of how many closed before them; interleaved
BTC/ETH opportunities that do not cross-contaminate; fees landing on the
attribution exactly once; partial entry/exit fills all attributed correctly;
attribution totals across fully-closed non-overlapping opportunities
reconciling exactly to the account's own net realized result; two
concurrently-open opportunities on the same symbol each getting their exact
share of a shared position's realized result; the scorecard recording the
corrected value; and a direct regression test mirroring the reported bug
(eight sequential same-symbol trades, each reporting the same small
constant result rather than a growing cumulative one). Mutation-tested:
reverting `_finish_attribution` to the old cumulative-read logic reproduces
9 of the 10 failures; restoring the fix (verified byte-identical) returns
all 10 to green.

### 23.4 Dashboard connection-label wording (`apps/dashboard/index.html`)

**Symptom:** on a failed `fetch` to the API, the dashboard's "last updated"
field showed the bare word `"disconnected"`, which reads as venue/exchange
connectivity (reported separately, per-venue, in the consolidated table) —
not what actually happened.

**Fix:** presentation-only change to `"DASHBOARD DISCONNECTED FROM API"`.
No venue-connectivity logic was touched.
`tests/integration/test_api.py::test_dashboard_connection_failure_label_is_unambiguous`
asserts the new wording is present in the served HTML and the old bare
string is gone.

### 23.5 Verification

- Focused suites (portfolio, paper account/executor, orchestrator, risk,
  MARIN, attribution, API/dashboard, all new Phase 1 polish tests): all
  green.
- `pytest -q tests/contract/test_event_bus_contract.py`: 27 passed, 27
  skipped (Redis suite skipped without `TF_TEST_REDIS_URL`).
- `pytest -q tests/contract/test_developer_tooling.py`: 64 passed, 5
  skipped (Docker unavailable).
- `ruff check .`: all checks passed.
- `mypy --ignore-missing-imports core`: no issues found in 24 source files.
- `pytest -q tests`: **1163 passed, 0 failed, 74 skipped** (skips are all
  Redis/Postgres/Docker infrastructure gates, not evaluated by default).
- With a native Redis (`redis-server --port 6399`) and native PostgreSQL 16
  service both running and `TF_TEST_REDIS_URL`/`TF_TEST_POSTGRES_DSN` set:
  `pytest -q tests` → **1232 passed, 0 failed, 5 skipped** (the 5 remaining
  skips are exclusively "no Docker daemon" — Docker Hub's blob CDN is
  blocked in this sandbox, identical to Phase 0's own documented
  limitation). Zero failures with real infrastructure exercised, matching
  the mandate's requirement to run the Redis/Postgres contract variants
  where the environment permits.

### 23.6 Simulated accounting-proof evidence

`scripts/phase1_accounting_proof.py` drives a fully offline, deterministic
1,500-tick paper session (in-memory bus/store, `ManualClock`, the default
synthetic market's recurring cross-venue dislocations) and reconciles every
identity the mandate requires. Output from one run:

```
ticks run:                 1500
closed opportunities:      32
open positions:            2 (NOT FLAT)
initial_balance:           100,000.00
cash:                      100,407.71
equity:                    100,399.87
gross_pnl:                 2,217.54
fees_paid:                 1,817.67
net_pnl:                   399.87
realized_pnl:              2,227.85
unrealized_pnl:            -10.31
gross_exposure:            49,942.70
net_exposure:              -7.84
sum(attribution.realized_pnl) over 32 closed trades: 452.69
```

Reconciliation (all differences below 1e-6, i.e. floating-point-exact):

- `equity - initial_balance` (399.872417) == `net_pnl` (399.872417) —
  RECONCILES. (The account was not flat at the end — two positions were
  still open — so this identity also equals `net_pnl` because `net_pnl`
  already includes `unrealized_pnl`.)
- `gross_pnl - fees_paid` (399.872417) == `net_pnl` (399.872417) —
  RECONCILES.
- `realized_pnl + unrealized_pnl - fees_paid` (399.872417) == `net_pnl`
  (399.872417) — RECONCILES.

**Attribution total does not equal `realized_pnl - fees_paid` directly, and
that is expected, not a bug** — the mandate explicitly warns against
comparing these blindly when hedge or non-opportunity fills exist. Replaying
the fill log with the exact per-fill delta logic
`Orchestrator._track_realized_delta` uses, bucketed by category:

| category | gross | fees | net |
|---|---|---|---|
| closed opportunities (32) | 2,227.854594 | 1,775.160061 | 452.694534 |
| still-open opportunities (pending, not yet in `scorecard.trades`) | 0.000000 | 27.501470 | -27.501470 |
| hedges / fills outside any tracked opportunity | 0.000000 | 15.006958 | -15.006958 |

- closed-category net (452.694534) == `sum(attribution.realized_pnl)`
  (452.694534) — RECONCILES exactly.
- sum of all three categories (410.186106) == `realized_pnl - fees_paid`
  (410.186106) — RECONCILES exactly.

Every identity the mandate requires holds to floating-point precision. The
remaining two categories (still-open opportunities and OKAPI hedge fills)
are legitimate, separately-accounted categories, not missing or leaked P&L:
an open opportunity's contribution simply has not been finalized into
`scorecard.trades` yet, and a hedge fill is never registered against an
opportunity attribution builder in the first place (by design — OKAPI's
standing hedge loop is not itself an "opportunity").

### 23.7 Live-soak status (re-checked)

Connectivity to Binance and Coinbase was re-checked once, as the mandate
requires, without retrying or bypassing the proxy:

```
$ curl https://api.binance.com/api/v3/ping          → CONNECT tunnel failed, HTTP 403 (connect_rejected)
$ curl https://api.exchange.coinbase.com/time       → CONNECT tunnel failed, HTTP 403 (connect_rejected)
```

Unchanged from section 17: a deliberate proxy-level policy denial, not a
transient failure. **PHASE 1 LIVE PUBLIC-FEED SOAK = PENDING EXTERNAL
ENVIRONMENT.** No `phase1-validated` tag was created or moved as a result of
this polish pass, consistent with section 17's unmet condition.

### 23.8 Polished commit

`PHASE1_POLISHED_SHA` is the `HEAD` of `phase1-tidal-remediation` introduced
by this polish pass's commit (`git rev-parse HEAD` on this branch after that
commit) — recorded in the accompanying validation report rather than
hardcoded here, since a commit cannot cite its own hash. All of section
23.5's verification, including the real-Redis/real-Postgres full-suite run,
was performed against that commit's working tree before it was committed.

## 24. Attribution dispatch-order hardening (post-polish)

Independent review raised a residual concern in the section 23.3 attribution
fix: it derived each fill's realized-P&L delta *inside* the PAPER_FILL
handler, by comparing the position's current cumulative `realized_pnl`
against a remembered per-venue:symbol baseline. `PaperExecutor.poll()`
applies every live order's fill to `PaperAccount` synchronously as it
iterates, and `Orchestrator._settle()` calls `bus.drain()` only once, after
`poll()` returns for the whole cycle — `InMemoryEventBus.publish()` enqueues
an event, it does not dispatch it. So two fills on the same venue:symbol
from two *different* opportunities can both mutate the shared position
before either one's PAPER_FILL handler runs, and whichever handler happens
to be dispatched first (FIFO order, not application order) would read
current cumulative state that already reflects both fills.

**Confirmed, not theoretical**: `tests/integration/test_attribution_dispatch_ordering.py`
reproduces the exact scenario from the review (fill A takes cumulative
realized P&L 0 → +10, fill B on the same symbol then takes it +10 → +25,
both applied and queued before either handler runs) using the real
`InMemoryEventBus`, with no `drain()` between the two fills. Under the
section 23.3 implementation this attributed +25 to opportunity A and 0 to
opportunity B — confirmed by mutation-testing (reverting to the baseline
implementation reproduces 2 of 4 new test failures).

**Fix**: `PaperAccount.apply_fill` now captures each fill's own realized-P&L
delta (the exact value `PositionState.apply` already returns) directly onto
the `FillEvent` as `realized_pnl_delta`, before the fill is ever published.
`Orchestrator._on_fill` reads that value straight off the event instead of
reconstructing it from live account state, so attribution no longer depends
on bus dispatch order at all — it is fixed the moment the fill is applied,
not when its handler happens to run. This also let
`Orchestrator._track_realized_delta` and its `_realized_baseline` dict be
removed entirely: the per-venue:symbol bookkeeping they existed for is no
longer needed once each fill already carries its own answer.

4 new tests cover: two different opportunities closing the same symbol
without draining between fills; two fills belonging to the same opportunity;
a hedge fill (no attribution builder) interleaved between two attributed
fills; and idempotency under a duplicate PAPER_FILL delivery. Full
re-verification: 1236 passed, 0 failed, 5 skipped (Docker only), with real
Redis and PostgreSQL exercised; `ruff check .` and
`mypy --ignore-missing-imports core` both clean. The accounting-proof
script (section 23.6) was re-run and produced identical, still-reconciling
numbers.

`PHASE1_FINAL_POLISH_SHA` is the `HEAD` of `phase1-tidal-remediation`
introduced by this hardening commit, recorded in the accompanying report for
the same reason `PHASE1_POLISHED_SHA` is not hardcoded here.
