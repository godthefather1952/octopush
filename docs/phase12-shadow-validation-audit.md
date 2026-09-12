# Phase 12 — Shadow validation audit

**Disposition: PHASE 12 REMEDIATED / AUTOMATED VALIDATION PASS / FIELD SHADOW TEST PENDING.**

## Checkpoints

- Repository: `godthefather1952/octopush`
- Frozen audit checkpoint: `1acd9c58b5885c4be44be1529a5831d8e821815c`
- Remediation branch: `remediate-phase12-shadow`
- Last production-code-changing commit: `42baa7ffa02036fa38de762c2f2dad10fbfabdce`
- Validated code-and-test checkpoint: `60190fca1ebce95a79fac7ad6f06f49e7662dbdf`
- CI configuration changed: **no**
- Phase 13 started: **no**

The Phase 11 historical OPEN session `session-575e8faf35e04e4592afe87da7bb04cb` was not modified. The final Phase 11 Docker/PostgreSQL field retest remained deferred by operator direction.

## Frozen findings and disposition

| Finding | Severity | Result |
| --- | --- | --- |
| P12-C1 — observer changed RUNE error-rate input | CRITICAL | REMEDIATED |
| P12-H1 — fills not duplicate-safe/plan-safe | HIGH | REMEDIATED |
| P12-H2 — lifecycle mirror untruthful/incomplete | HIGH | REMEDIATED |
| P12-H3 — terminal state could regress | HIGH | REMEDIATED |
| P12-H4 — readiness could contradict itself | HIGH | REMEDIATED |
| P12-H5 — observer failures/gaps hidden | HIGH | REMEDIATED |
| P12-M1 — mutable registry query aliases | MEDIUM | REMEDIATED |
| P12-M2 — resident/lifetime counter mismatch | MEDIUM | REMEDIATED |

## Remediation

### P12-C1 — economic neutrality

`Subscription` now has `health_relevant: bool = True`. InMemoryEventBus and RedisStreamBus retain ordinary per-subscription delivery/error diagnostics but feed only health-relevant subscribers into the rolling error window consumed by RUNE. ShadowObserver subscribes with `health_relevant=False`. No RUNE limit, threshold, sizing rule, gate logic, or approval semantics changed.

### P12-H1 — fill integrity

ShadowRegistry indexes client order IDs to exact shadow executions. Fill observation resolves `client_order_id -> execution`, computes notional from serialized quantity × price, and treats a known fill ID as a complete no-op including monetary values and timestamps. The previous newest-execution fallback is gone.

### P12-H2 / P12-H3 — lifecycle truth

`REJECTED` is distinct from `RISK_REJECTED`. Strategy-state events carry the existing authoritative rejection reason as observational detail. RISK_FAIL remains the risk-specific rejection source. APPROVED_REDUCED is correctly observed as approved. Decision/execution transitions are monotonic and terminal records cannot be resurrected by stale or duplicate delivery.

### P12-H4 — readiness

ShadowReadiness remains reporting-only. It derives PAPER mode, SHADOW profile, public-feed configuration, usable market state, recorder health, coordination readiness, RUNE health, VESKA/PaperExecutor availability, MARIN state, OKAPI hedge availability, private-execution absence, and observer gaps from their actual owners. False mandatory conditions add reason codes. LUMEN remains optional.

### P12-H5 — observer visibility

Observer accounting separates events seen, intentionally ignored events, unattributable events, and actual handler failures. Failures remain isolated from trading but are logged at WARNING and surfaced in ShadowSnapshot/ShadowReadiness. The observer remains excluded from the risk-health denominator.

### P12-M1 / P12-M2 — registry integrity

Query/checkpoint surfaces return deep detached copies; stored order/checkpoint inputs are detached. `decisions_total` is lifetime observed count and `resident_decisions` is explicit, so compaction cannot make the snapshot vocabulary contradictory.

## Permanent Phase 12 validation

`tests/unit/test_phase12_shadow.py` contains 16 test functions / 17 pytest cases covering:

- RUNE error-rate neutrality at exactly 0.25 and above 0.25;
- duplicate/delayed fill identity and missing attribution;
- terminal decision/execution monotonicity;
- APPROVED_REDUCED and generic rejection semantics;
- detached decision/order/checkpoint reads;
- lifetime/resident compaction counts;
- observer failure visibility;
- same-logical-time snapshot/API purity;
- readiness contradiction prevention;
- PAPER-only executor/adapter/API boundary;
- deterministic PAPER-vs-SHADOW economic equivalence.

The equivalence harness uses identical settings, ManualClock, seeded synthetic market/dislocation and deterministic IDs. It compares opportunity state/economics, full RUNE decisions and gates, OMS orders, fills, portfolio state, OKAPI hedge records, MARIN's result, and kill-switch state. RUNE must be exercised and the economic summaries must be exactly equal.

## Validation evidence

Final validated checkpoint: `60190fca1ebce95a79fac7ad6f06f49e7662dbdf`.

Python 3.11 unit + contract: `1 failed, 1399 passed, 126 skipped, 1 warning in 74.53s`.

The sole failure is the pre-existing packaging contract `tests/contract/test_packaging.py::TestComposeMatchesTheConfiguration::test_the_feed_compose_selects_is_a_real_feed`, which treats compose's `${TF_FEED:-simulated}` expression as a literal feed name. All 17 Phase 12 cases pass.

PAPER boundary: **PASS**.

mypy(`core/`): **PASS**.

Ruff: exactly the three pre-existing findings and no new Phase 12 finding:

- `agents/marin/agent.py:40` — I001
- `agents/marin/source.py:22` — I001
- `agents/okapi/registry.py:360` — SIM102

Python 3.12 backend contract precheck: **PASS**. The following full-suite step entered the repository's established long-running condition. This is baseline infrastructure/test-runtime debt, not a Phase 12 regression.

An intermediate boundary test incorrectly rejected every API path containing the string `live`, including the intentional GET-only `/api/pre-live` readiness endpoint. That was classified **C — TEST DEFECT** and corrected without changing production behavior.

## PAPER-only boundary

The validated branch still has only TradingMode.PAPER, Docker TF_MODE=paper, PaperExecutor/PaperAccount, public unauthenticated adapters, no adapter order-submission capability, GET-only `/api/shadow`, GET-only `/api/pre-live`, no promotion route, no exchange credentials, and no live executor.

## Remaining field validation

Automated closure does not validate simulated fills against real venue fills. The next allowed step is an actual Codespaces SHADOW field rehearsal against public live market data, while execution remains PAPER. Inspect readiness, recording, public-feed freshness, observer gaps, graceful shutdown, and session finalization before any later phase.

## Final disposition

**PHASE 12 REMEDIATED / AUTOMATED VALIDATION PASS / FIELD SHADOW TEST PENDING**

Do not infer private-venue or live-deployment readiness from this closure.

---

# Live-feed field rehearsal — first attempt and remediation

The field rehearsal called for above was run. It did not pass, and this
section records why, what was fixed, and what is still unproven.

Field run: branch `remediate-phase12-shadow`, HEAD
`894ed48a96e8c804b0f4151d62c3e663867b2935`, session
`session-a884bd006c334809985844b466c805e4`.

Startup itself was correct — PAPER mode, SHADOW profile, live feed,
PaperExecutor, public data only, no real orders — and the dedicated Phase 12
suite passed 17/17. The live market feeds then never became usable, for three
separate reasons, tracked separately below.

Remediation branch: `remediate-phase12-live-feed-field`, cut from
`894ed48a96e8c804b0f4151d62c3e663867b2935`.

## P12-F1 — Coinbase public snapshot exceeded the client receive limit

**Severity: HIGH. Status: REMEDIATED IN CODE / FIELD RETEST PENDING.**

VENUE_B failed every attempt with

    sent 1009 (message too big) ... exceeds limit of 1048576 bytes

and entered a permanent reconnect loop, so TIDAL, NORO and ZEPHR never warmed.

Root cause: `venues/base/ws.py` called `websockets.connect(url,
ping_interval=15, close_timeout=5)` and never passed `max_size`. The library
default is exactly 1,048,576 bytes, which matches the limit in the error. A
legitimate full Coinbase level-2 snapshot is larger, so the client closed the
connection itself before the first snapshot could be parsed. Nothing about the
adapter, the parser or the venue was wrong.

Fix: `VenueConfig.ws_max_message_bytes`, a finite per-venue bound
(default 8 MiB, validated range `0 < n <= 64 MiB`), passed into
`websockets.connect(max_size=...)` by the shared transport.

`max_size=None` was rejected as the fix. This is unauthenticated input from a
public endpoint; an unbounded receive size makes process memory a function of
what a remote server chooses to send. 8 MiB is roughly an order of magnitude
above the snapshot that failed, which covers a full-depth book for a liquid
instrument with headroom; 64 MiB is the point past which a single public
market-data frame is no longer plausible.

**This is a transport bound, not a storage bound.**
`max_book_levels_per_side` is unchanged and still fails a book closed on
overflow. A message large enough to be received is still contained afterwards
if it would overflow the local book, and `tests/unit/test_ws_message_bound.py`
asserts that independence directly rather than assuming it.

Tests (`tests/unit/test_ws_message_bound.py`, 18 cases):

- the configured value reaches `websockets.connect(max_size=...)` through the
  real `_session()` call site, and is the configured value rather than a
  venue-specific constant;
- a loopback `websockets` server — no exchange is contacted — serves a payload
  above 1 MiB: refused under the old default, received byte-for-byte intact
  under the configured bound, and still refused past the configured bound;
- the bound stays finite: `None`, zero, negative and implausibly large values
  are all rejected at configuration load;
- raising the transport bound does not raise, disable or otherwise weaken the
  book storage ceiling, and an overflowing book still fails closed with every
  level retained.

Field result: **NOT YET OBSERVED.** See "Field retest status" below.

## P12-F2 — Binance public endpoint returned HTTP 451 from Codespaces

**Severity: FIELD / ENVIRONMENT. Status: UNRESOLVED — INVESTIGATION TOOLING
ADDED, NO ENDPOINT CHANGED.**

VENUE_A failed with `server rejected WebSocket connection: HTTP 451`.

451 is an access decision the server made about the caller. It is not a
malformed request, and it is not an adapter defect. It was **not** worked
around: no proxy, no VPN, no mirror, no undocumented endpoint, no credential
and no authenticated API was added or considered as a remedy.

Deciding it correctly needs two answers, and this remediation could produce
neither from the environment it ran in:

1. whether a candidate endpoint is reachable **from the environment that will
   run the rehearsal** — which is Codespaces, not here;
2. whether it preserves every semantic the adapter depends on.

The adapter's actual contract, established by inspection, is:

- combined stream `{ws_base}?streams=<name>/<name>/...` with names
  `{symbol}@depth@100ms` and `{symbol}@trade`, symbol lowercase concatenated;
- a combined-stream envelope carrying `stream` and `data`;
- `depthUpdate` events carrying `U` (first update id) and `u` (final update
  id), contiguous across consecutive updates — `DepthSynchronizer` depends on
  this and an endpoint that breaks it would be a worse outcome than the 451;
- `trade` events;
- `GET {rest_base}/api/v3/depth?symbol=...&limit=...` returning `lastUpdateId`
  and both sides, where limit is one of 5/10/20/50/100/500/1000/5000;
- symbols `BTC-USDT` and `ETH-USDT` in concatenated venue spelling.

`scripts/lib/probe_venue_a.py` checks a candidate endpoint against every one
of those and reports them separately, so an endpoint cannot be adopted merely
because it connects. It sends no credentials and touches no private endpoint.

**No endpoint was changed.** Adopting one without evidence it is officially
supported, public, and reachable from the target environment would be
guessing, and the guess would be embedded in production configuration.

**VENUE_A symbols are unchanged and must stay unchanged.** VENUE_A carries
BTC-USDT/ETH-USDT and VENUE_B carries BTC-USD/ETH-USD because those are
different instruments. Collapsing USD and USDT to manufacture a cross-venue
pair is the defect (TIDAL-C3) this platform already removed once, and a field
run with no same-instrument cross-venue opportunity is an acceptable result.

## P12-T1 — Field harness polled the wrong API port

**Severity: TEST/HARNESS DEFECT. Status: REMEDIATED.**

The rehearsal reported `ERROR: Trading Floor API never became reachable`. The
API was up and had logged `http://0.0.0.0:8080/`. The ad-hoc field script was
polling a loopback URL on a port of its own invention — one that has never
matched this repository. The platform was not at fault and no production
change was made for this.

`scripts/lib/common.sh` already defines `TF_API_PORT="${TF_API_PORT:-8080}"`
and `TF_API_URL`. The remediation adds `scripts/field-check-shadow.sh`, which
sources that file and uses those values, and `scripts/lib/field_shadow.py`,
which has no fallback URL at all: given nothing it exits rather than polling a
guess and reporting the platform unreachable.

The script also gates on the safety envelope before observing anything —
PAPER, SHADOW, live feed, PaperExecutor, and the manifest's
`private_venue_access` / `real_order_submission` flags, read from the running
platform's own endpoints. A rehearsal that cannot prove those is not observed.
Every request it makes is a GET.

`tests/unit/test_field_harness_port.py` (9 cases) pins the shared definition
as the single source of the port, proves the field tooling reuses rather than
redefines it, and fails if any repository tooling reintroduces the stale port.

## Automated validation of this remediation

Run on the remediation branch:

- Phase 12 dedicated suite `tests/unit/test_phase12_shadow.py`: **17 passed**;
- new transport and harness tests: **27 passed** (18 + 9);
- venue, reconnect and book-storage suites: **113 passed**, no weakening;
- PAPER boundary `tests/audit/test_phase6_paper_boundary.py`: **30 passed**;
- mypy(`core/`): **PASS**;
- Ruff: **exactly the three pre-existing baselines**, zero new findings;
- Python 3.11 `tests/unit tests/contract`:
  `1 failed, 1421 passed, 131 skipped` — the single failure is the known
  `${TF_FEED:-simulated}` packaging baseline, unchanged and not repaired here.

The skip count differs from the 126 recorded above because the machine this
ran on has no Redis or PostgreSQL; those backend suites skip rather than run.
That is an environment difference, not a regression.

## Field retest status

**NOT PERFORMED.** This remediation was produced in an environment with no
Docker daemon, no PostgreSQL, no Redis and no outbound access to any exchange
(every probe returned a proxy-level refusal, which is this environment's
allowlist and is **not** evidence about Binance, Coinbase or Codespaces).

Consequently the following remain unproven and must not be reported otherwise:

- that VENUE_B connects, receives its snapshot without code 1009, stays
  connected, and becomes healthy;
- that VENUE_A is or is not usable from Codespaces;
- ticks, observer progress, reconnect counts, readiness codes, recorder
  health, memory behaviour over a 5–10 minute window;
- graceful shutdown, session `COMPLETE`, `ended_at`, `events_lost == 0`.

The database state of `session-a884bd006c334809985844b466c805e4` was likewise
not observable from here and was not modified. Neither historical session was
touched.

## Disposition

**PHASE 12 LIVE-FEED REMEDIATION — CODE COMPLETE / FIELD RETEST PENDING**

P12-F1 is remediated in code and proven by test, including against a real
socket. P12-T1 is remediated. P12-F2 is unresolved and deliberately left
truthful in production. None of the three may be called field-validated until
a Codespaces rehearsal produces the evidence above.
