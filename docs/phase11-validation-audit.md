# Phase 11 validation audit — full-paper operations

## Final status

**REOPENED / REMEDIATION VALIDATED / FIELD RETEST REQUIRED**

Repository: `godthefather1952/octopush`

Original validation branch: `validate-phase11-operations`

Field-remediation branch: `remediate-phase11-graceful-shutdown`

Frozen audit checkpoint:

`d768cb1c044849eab3a341365dd750e93697273e`

Final production-remediation checkpoint validated by the focused Phase 11 suite:

`ed3a51eac88ce26ed3033d8df07244ce258889c7`

The documentation commits that follow that remediation checkpoint do not alter
production or test behavior.

Phase 11 remains observational. This validation does not authorize or implement
live trading and does not change the meaning of the conservative
`PreLiveReadinessSnapshot` live-readiness claims.

---

## Field finding after initial closure

### P11-F1 — Docker SIGTERM could leave a durable session OPEN

**Severity:** HIGH

A real Codespaces/Docker paper run on final documented Phase 11 checkpoint
`677c71bd4b7b8c561a5b9c8e7aefb527f2aa104d` ran for 8,351 orchestrator
ticks and reported 162,968 events recorded, 320 paper orders, 306 paper fills,
32 hedges and 418 reconciliations with no runtime error and the PAPER boundary
intact.

The operator then stopped the stack with `./stop-paper.sh`. Docker removed the
trading-floor container after approximately 10.6 seconds and preserved the
PostgreSQL volume. Direct inspection of the exact session afterwards showed:

```
session_id    session-575e8faf35e04e4592afe87da7bb04cb
status        OPEN
ended_at      NULL
events_lost   0
```

The historical row remains intentionally untouched. `events_lost = 0` on an
OPEN session is not proof that the final recorder tail was flushed, so
retroactively certifying it COMPLETE would violate the recorder-integrity
contract.

**Root cause:** the embedded Uvicorn server could own SIGTERM/SIGINT while the
top-level process awaited `asyncio.gather()` across Uvicorn plus the
never-ending orchestrator and LUMEN loops. Uvicorn could shut down its API task
without causing those other tasks to finish. The process therefore remained
alive until Docker's termination grace expired, so the canonical
`Platform.stop() -> Recorder.stop() -> EventStore.finalize_session()` path
was never guaranteed to run.

**Remediation:** `apps/orchestrator/__main__.py` now gives the trading-floor
process explicit ownership of SIGTERM/SIGINT through one shared stop event.
Embedded Uvicorn opts out of installing competing signal handlers across both
older (`install_signal_handlers`) and newer (`capture_signals`) Uvicorn
interfaces. A signal, duration expiry or clean runtime-task termination now
converges on the existing ordered shutdown path exactly once:

```
STOPPING_API
STOPPING_LOOPS
STOPPING_FEEDS
DRAINING_BUS
STOPPING_RECORDER
Recorder.stop()
EventStore.finalize_session()
STOPPED
```

No trading economics, risk gate, execution behavior, reconciliation semantics,
hedging economics, intelligence behavior, CI configuration or PAPER boundary
was changed. `scripts/stop-paper.sh` did not need a second lifecycle
implementation or a larger Docker kill timeout.

**Permanent regression test:**
`tests/contract/test_phase11_process_shutdown.py` launches the real
`python -m apps.orchestrator` process with an embedded API and a temporary
durable SQLite store, waits for a recorded event, sends a real POSIX SIGTERM,
requires a bounded clean process exit, then independently reopens SQLite and
asserts that the exact session is COMPLETE, has a non-null `ended_at`, reports
zero `events_lost`, and contains persisted events.

The test is deliberately a subprocess contract rather than another direct
`await platform.stop()` test, because direct cleanup was already passing and
could not reproduce the field failure.

### Automated validation of P11-F1 remediation

Validated production-code checkpoint:

`a0f3a8950218f4c99e6a1d66175f9cb25258c87b`

Python 3.11 unit + contract result:

```
1 failed, 1382 passed, 126 skipped in 63.57s
```

The sole failure is the pre-existing packaging assertion for
`${TF_FEED:-simulated}`. The ordinary pre-remediation baseline was
`1 failed, 1381 passed, 126 skipped`, so the new process-level SIGTERM
contract contributed one additional passing test and no additional failure.

Other gates at the same production-code checkpoint:

- PAPER boundary: **PASS**
- mypy(core): **PASS**
- Ruff: exactly the same three baseline findings only
  (`agents/marin/agent.py` I001, `agents/marin/source.py` I001,
  `agents/okapi/registry.py` SIM102)
- New Phase 11 Ruff findings: **0**

A real Docker/PostgreSQL field retest remains required before Phase 11 can be
closed again. The retest must create a new session, stop it through
`./stop-paper.sh`, then prove that new row is COMPLETE with non-null
`ended_at`, zero `events_lost`, and persisted events. The historical OPEN
session above must remain unchanged.

## Scope

Phase 11 is the platform/session operational layer, not an agent. The audit
covered the runtime vocabulary, session registry, production lifecycle witness,
aggregated operational snapshot, readiness projection, session summary, API
projection, recording state and the Phase 11 reads of earlier-phase owners.

Primary files:

- `core/models/runtime.py`
- `apps/operations/registry.py`
- `apps/orchestrator/wiring.py`
- `apps/orchestrator/__main__.py`
- `apps/api/app.py`
- `apps/orchestrator/coordination.py`
- `agents/okapi/agent.py`

Cross-phase authorities checked included OMS/PaperExecutor, MARIN, OKAPI, LUMEN,
HealthRegistry, the recorder, the coordination registry and the paper-only
configuration boundary.

No CI configuration was changed.

---

## Frozen findings

### P11-C1 — canonical operational snapshot crashed

**Severity:** CRITICAL

`Platform.operational_snapshot()` passed `ticks` and `events_recorded`
to `OperationalRegistry.metrics()`, while the registry supplied the same
keywords itself. Python therefore raised a duplicate-keyword `TypeError` and
`GET /api/operations` could not return its canonical snapshot.

**Remediation:** `OperationalRegistry.metrics()` now has explicit optional
`ticks` and `events_recorded` parameters. The caller can provide the
authoritative live values exactly once; otherwise the current session record is
used.

**Proof:** the Phase 11 API regression test invokes the production
`/api/operations` route and receives a serializable 200 response.

### P11-H1 — lifecycle witness did not cover the real process lifecycle

**Severity:** HIGH

The production CLI started the bus before `Platform.start()` created a Phase
11 session. A bus-start failure therefore had no operational session to mark
FAILED. API and loop shutdown also happened outside `Platform.stop()`, while
Phase 11 declared shutdown stages for those actions.

**Remediation:** `Platform.prepare_start()` creates the canonical record
before externally-owned startup; `Platform.start_bus()` witnesses the real bus
start and records/re-raises failures. The production CLI calls the bus witness
before `Platform.start()`, preserving the historical bus → storage → feeds
ordering. The CLI records STOPPING_API and STOPPING_LOOPS beside the existing
shutdown actions.

Direct deterministic callers of `Platform.start()` retain their prior
synchronous-bus behavior.

**Proof:** tests cover bus-start failure, recorder-start failure, exception
identity, successful lifecycle and repeated stop idempotence.

### P11-H2 — readiness could contradict its own fields

**Severity:** HIGH

The initial producer could report `ready=True` while storage was unhealthy
and used weak proxies such as object existence or hard-coded booleans for other
dimensions.

**Remediation:** readiness now copies or derives facts from existing
authorities: required HealthRegistry components, RUNE health, VESKA health and
execution-disabled state, MARIN's clean reconciliation plus health, OKAPI's own
readiness, recorder health when recording was requested, explicit bus/feed
lifecycle state, UNKNOWN orders and the kill switch.

LUMEN remains optional and does not enter the required reason set.

**Proof:** tests cover before-start, no-market, intentional no-record/no-feed
sessions, unhealthy storage, optional LUMEN and kill-switch state.

### P11-H3 — read-only operational snapshot mutated OKAPI

**Severity:** HIGH

`operational_snapshot()` called `Okapi.okapi_snapshot()`, whose snapshot
path called `mirror_targets()` and wrote target-registry state.

**Remediation:** target metadata is mirrored when `set_desired_delta()`
actually changes the authoritative target. OKAPI snapshot methods no longer
create/advance target-registry state merely because somebody reads them.

Focused validation found a second observational defect: newly constructed
`DeltaReport` objects minted random event IDs on every same-instant snapshot,
so repeated GETs were not equivalent even after registry mutation was removed.
Snapshot-only delta reports now use stable derived identities while published
delta reports retain their existing event-ID semantics.

**Proof:** same-instant repeated operational snapshots are equal and leave
target/hedge registry state unchanged.

### P11-M1 — OperationalMetrics omitted real activity

**Severity:** MEDIUM

Most modeled counters remained at their default zero values.

**Remediation:** operational metrics now source session facts from existing
owners: orchestrator ticks, recorder events, OMS lifetime order/fill counters,
coordination opportunity/risk-rejection counters, OKAPI hedge requests, MARIN
completed runs, LUMEN completed analyses and the shadow registry.

The two missing observation-only totals were added at the points where the
facts already become true; they participate in no decision.

### P11-M2 — SessionSummary returned default zeroes

**Severity:** MEDIUM

Opportunities, rejections, reconciliations, gross P&L and fees were modeled but
not populated.

**Remediation:** the summary now uses coordination lifetime opportunity and
terminal-rejection totals, OMS order/fill totals, OKAPI hedge requests, MARIN
completed reconciliations and PortfolioState's gross/net P&L and fees.

### P11-M3 — incidents were not session scoped

**Severity:** MEDIUM

The retention contract promised that an unresolved incident prevents session
compaction, but incidents had no session identity and compaction did not inspect
them.

**Remediation:** incidents carry the canonical operational/recorder
`session_id`; queries can filter by session; current operational snapshots
show current-session incidents; unresolved incidents block compaction of their
session.

### P11-M4 — recording requested and recording active were conflated

**Severity:** MEDIUM

The snapshot used `_recording` for both requested and active state.

**Remediation:** `requested` comes from the immutable session manifest;
`active` remains current recorder activity/health.

### P11-M5 — lifecycle counters counted method calls, not sessions

**Severity:** MEDIUM

Repeated starting/stopping/failure calls could inflate lifetime session
counters.

**Remediation:** transitions are idempotent by current session state. A session
is counted started/completed/failed at most once, completed sessions cannot be
rewritten as failed, and repeated cleanup does not manufacture additional
sessions.

---

## Dedicated Phase 11 tests

The permanent audit suite is:

- `tests/audit/test_phase11_operations_registry.py`
- `tests/audit/test_phase11_operations_lifecycle.py`
- `tests/audit/test_phase11_operations_snapshot.py`
- `tests/audit/test_phase11_operations_readiness.py`

These contain **14 Phase 11 tests**.

Because the repository's backend-enabled Python 3.12 full-suite job has a known
pre-existing long-running condition, the same four audit files were also
mirrored on a separate test-only branch into `tests/unit/`. That branch was
created from the exact production remediation checkpoint and changed no
production code or CI configuration.

Focused validation branch:

`validate-phase11-operations-focused-ci`

Focused validation SHA:

`9049b5fd25e088406a79098b352125505df731b7`

Its production tree descends directly from
`ed3a51eac88ce26ed3033d8df07244ce258889c7`; the additional commits only copy
the Phase 11 test files into the existing Python 3.11 discovery path.

### Focused result

```
1 failed
1395 passed
126 skipped
1 warning
```

The sole failure is the pre-existing packaging test:

`tests/contract/test_packaging.py::TestComposeMatchesTheConfiguration::test_the_feed_compose_selects_is_a_real_feed`

which interprets:

`${TF_FEED:-simulated}`

as a literal feed value.

Compared with the ordinary baseline result of:

```
1 failed
1381 passed
126 skipped
```

all **14 added Phase 11 tests passed**.

An earlier focused run intentionally caught one residual Phase 11 failure:
same-instant OKAPI snapshot delta-report IDs differed. That finding was fixed
before the final focused result above.

---

## Other validation gates

### Paper-only boundary

**PASS**

The dedicated PAPER boundary job passes. No `TradingMode.LIVE`, live executor,
authenticated venue order path or real-order submission was added.

### mypy(core)

**PASS**

```
mypy --ignore-missing-imports core
```

passes on the Phase 11 branch.

### Python 3.11 unit + contract on the normal production branch

**BASELINE ONLY**

```
1 failed
1381 passed
126 skipped
```

The only failure is the same packaging assertion described above. The corrected
Phase 11 lifecycle implementation restored normal runtime for this suite after
an intermediate remediation attempt had accidentally introduced a background
bus dispatcher into direct `Platform.start()` callers.

That intermediate regression was not accepted. The final implementation
preserves the old direct-start semantics and witnesses production bus startup
through a dedicated hook.

### Ruff

**BASELINE ONLY**

Three findings, all present before Phase 11:

- `agents/marin/agent.py` — I001
- `agents/marin/source.py` — I001
- `agents/okapi/registry.py` — SIM102

No new Phase 11 Ruff finding remains.

### Backend-enabled Python 3.12 full suite

**BASELINE LONG-RUNNING CONDITION**

Backend precheck succeeds, then the workflow enters the long-running
`Full suite` step. The same condition was independently observed on Phase 9
base `6f5cf33ddb019292a6853a04a5aef2e216807a25`, before the Phase 10 or Phase
11 remediation. It is therefore not classified as a Phase 11 regression.

The workflow was inspected; its configuration was not modified.

---

## Cross-phase safety result

No Phase 11 remediation changes:

- trading mode;
- consensus weights;
- RUNE thresholds or hard gates;
- strategy opportunity thresholds;
- VESKA economic routing/sizing/fill behavior;
- UNKNOWN order semantics;
- MARIN reconciliation economics;
- LUMEN provider selection/prompts;
- PaperExecutor's exclusive execution role;
- venue authentication/credential boundaries;
- replay's economic decision rules.

The only OKAPI production adjustment moves mirror-metadata maintenance from a
read path to the authoritative target-write path and stabilizes IDs only in
the non-published snapshot representation.

---

## Final disposition

Original frozen Phase 11 findings: **9**

Original remediation: **9 / 9**

Post-closure field findings: **1**

P11-F1 automated remediation: **VALIDATED**

Permanent process-level SIGTERM contract: **PASS**

Python 3.11 new regressions: **0** (only the known packaging baseline remains)

PAPER-boundary regressions: **0**

mypy(core) regressions: **0**

Ruff regressions: **0** (three known baseline findings remain)

Historical affected PostgreSQL session:
`session-575e8faf35e04e4592afe87da7bb04cb` — **OPEN / intentionally untouched**

Phase 11 is **not closed again yet**. The code-level remediation is validated,
but the exact Docker/Codespaces shutdown path that exposed P11-F1 must be
retested against PostgreSQL and produce a new COMPLETE session before closure.

**Current status: PHASE 11 REOPENED / REMEDIATION VALIDATED / FIELD RETEST REQUIRED**

Do not begin Phase 12 until that field retest passes.
