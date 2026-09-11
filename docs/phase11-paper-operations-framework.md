# Phase 11 — Full-paper operational framework

**Status: REOPENED / SHUTDOWN REMEDIATION VALIDATED / FIELD RETEST REQUIRED.**

Validation does **not** promote the platform toward live execution. `PreLiveReadinessSnapshot` remains conservative: its live-readiness framework fields still report `NOT_VALIDATED` because Phase 11 code validation is not evidence that the strategy or deployment is ready for real money.

Every phase before this one gave a *component* a memory. Phase 6 gave execution
one, Phase 7 reconciliation, Phase 8 coordination, Phase 9 hedging, Phase 10
intelligence. None of them answers the question that sits above all of them:
**what was this run?**

Phase 11 answers it. Session manifests, a session lifecycle, operational
readiness, one aggregation surface, and a registry that remembers what the
platform did — none of which changes what the platform does.

---

## 1. Baseline

Frozen Phase 11 audit checkpoint: `d768cb1c044849eab3a341365dd750e93697273e`.
Phase 11 was audited there, its findings were frozen, then remediation was performed on `validate-phase11-operations`. Earlier-phase baseline CI debt is tracked separately from Phase 11 results.

A later Codespaces field run exposed P11-F1: Docker SIGTERM could stop embedded
Uvicorn without causing the orchestrator/LUMEN forever-tasks to leave their
shared wait, allowing Docker's grace period to expire before
`Platform.stop() -> Recorder.stop() -> finalize_session()`. The affected
historical PostgreSQL session remains OPEN and is intentionally not rewritten.

The narrow shutdown remediation is on
`remediate-phase11-graceful-shutdown`. Its production-code checkpoint
`a0f3a8950218f4c99e6a1d66175f9cb25258c87b` passes a real subprocess
SIGTERM contract that independently verifies a durable SQLite session becomes
COMPLETE with non-null `ended_at`, zero `events_lost`, and persisted events.
A fresh Docker/PostgreSQL field retest is still required before this phase is
closed again.

## 2. Construction-only philosophy

Four rules, the same four that governed Phases 6 through 10:

**Nothing reads it to decide.** `Platform.start()` still starts, `stop()` still
stops, warm-up still decides when trading may begin, the kill switch still
decides when it must halt. No branch consults what the operational registry
holds.

**Nothing is recomputed.** Component summaries come from `HealthRegistry`.
Execution counts come from `Veska.metrics()`. Coordination, hedging and
intelligence come from those phases' own snapshot methods. The operational
snapshot copies; it does not derive.

**Nothing is swallowed.** A startup or shutdown that raises is recorded as
FAILED and the same exception is re-raised, by a bare `raise`. A registry that
swallowed a startup failure would leave a half-built platform looking like a
running one.

**Nothing is a second identity.** Sessions key on the recorder's `session_id`,
which every recorded event already carries.

## 3. The three-axis model

Three concepts, easy to conflate and expensive to confuse:

| Axis | Values | What it means |
| --- | --- | --- |
| **Trading mode** | `PAPER` — and no other | What the platform may do with money |
| **Operational profile** | `PAPER`, `SHADOW` | What a run is *for* |
| **Market feed** | `simulated`, `live` | Where prices come from |

They are orthogonal. Valid combinations:

```
PAPER  / PAPER  / simulated   ordinary offline paper
PAPER  / PAPER  / live        paper trading against live public data
PAPER  / SHADOW / live        structured pre-live rehearsal
PAPER  / SHADOW / simulated   possible; readiness reports it is not a
                              genuine live-market rehearsal
```

**There is no LIVE combination**, and not because one was left out — there is
no live executor to combine with.

`TradingMode` has exactly one member and this phase does not add another. The
profile is read from **`TF_PROFILE`**, never from `TF_MODE`: a profile must not
be settable through the one variable whose only legal value is `paper`, or the
two concepts would be one keystroke apart.

## 4. `OperationalProfile`

`core/models/runtime.py`. PAPER and SHADOW, and both run under
`TradingMode.PAPER` through `PaperExecutor` against `PaperAccount`.

Putting SHADOW here rather than in `TradingMode` is the whole point: a profile
cannot become a trading mode by accident.

`Settings.operational_profile` defaults to PAPER, so nobody has to set anything
new for today's behaviour. `Settings.feed` is new too — the value
`load_settings` already validated, recorded so a manifest can state it rather
than re-reading the environment.

### Why `runtime.py` and not `operations.py`

`core/models/ops.py` already exists and holds `HedgeIntent`, `DeltaReport`,
`KillSwitchState`, `SystemEvent` and the reconciliation mismatches. An
`operations.py` beside it, differing by three letters and describing something
else entirely, would be a name nobody could disambiguate at a glance six months
from now. `runtime.py` says what the module is: the session/runtime layer.

## 5. `SessionManifest`

What a session was configured to be, written once from settings already fixed:
the three axes, symbols, venues, backends, the paper balance, the intelligence
provider, the config digest.

Three fields state the boundary in the record itself:

```
paper_executor:         True
private_venue_access:   False
real_order_submission:  False
```

so a SHADOW manifest cannot be misread as a live one by a reader who skims.

`session_id` is the recorder's. Minting a second would give one run two names
and force every future reader to learn which is canonical.

## 6. `OperationalSessionRecord`

Status, startup stage, shutdown stage, the manifest, tick and event counts, and
a `failure` string.

`SessionStatus` has no RESUMED or RESTARTING: a session is one process
lifetime, the platform has no resume semantics, and a value it could never
reach would invite someone to implement one to justify it.

## 7. Lifecycle

`StartupStage` names the order the composition root and `Platform.start()`
already work in. `ShutdownStage` names what `stop()` and `__main__` already do.

**Neither restructures anything.** No stage runner, no generic sequencer.
Shutdown ordering in particular is load-bearing — the API unwinds its own
lifespan before anything is cancelled, and the recorder closes last so the
events explaining a shutdown are persisted — and reordering it to make an enum
tidy would break that.

## 8. `OperationalRegistry`

`apps/operations/registry.py`. `create_session`, `mark_starting`,
`mark_running`, `mark_stopping`, `mark_stopped`, `mark_failed`,
`set_startup_stage`, `set_shutdown_stage`, `update_counts`, `note`, `annotate`,
`record_incident`, the queries, `metrics`, `compact` — plus the
`OperationalStore` ABC with no implementation.

**Every mutation takes `now_ms`.** Nothing in the module reads a clock.

Metadata calls return `None` rather than raising when no session record exists.
A bookkeeping call that raised because a record was missing would let the record
break the thing it records.

`mark_running` and `mark_stopping` leave a FAILED session FAILED. Nothing here
converts a recorded failure into a success.

`EventStore` is untouched. It stores the events a session produced, which is a
different question from what the session *was*.

## 9. `OperationalMetrics`

Counters. No thresholds, no alerting, no classification. The per-session counts
come from the components that own them, so the snapshot cannot disagree with the
component it describes.

`OperationalIncident` exists as vocabulary; **no automatic incident policy
does.** Nothing raises one, escalates one, or acts on one. `HealthRegistry`
remains the authority on component health, the kill switch on halting.

`OperatorAnnotation` is free text with a free-text author label — no identity
system, no authentication, and no API write route.

## 10. Component summaries

`OperationalComponentSummary` copies `HealthRegistry`'s own snapshot. **No new
health semantics**: `required` mirrors the existing `REQUIRED_COMPONENTS` set
rather than introducing a second notion of what matters.

## 11. `OperationalReadiness`

**Reporting only. It gates nothing** — not `Platform.start()`, not
`Orchestrator.tick()`, not `_seek`, not execution.

`ready` is False whenever anything required is unestablished, including when
nothing has been checked. Absence of evidence is not readiness. The validated
producer derives each reported dimension from an existing authority rather than
object existence or hard-coded optimism. Reason codes include `BUS_NOT_STARTED`,
`FEED_NOT_READY`, `NO_MARKET_STATE`, `COMPONENT_UNHEALTHY:<name>`,
`RISK_NOT_READY`, `EXECUTION_NOT_READY`, `RECONCILIATION_NOT_READY`,
`HEDGING_NOT_READY`, `UNKNOWN_ORDERS:n`, `STORAGE_UNHEALTHY`,
`NOT_RECORDING`, and `KILL_SWITCH_ENGAGED`.

**LUMEN's absence never makes this unready.** LUMEN is optional — not in
`REQUIRED_COMPONENTS`, not in `ConsensusConfig.required_agents` — and the
shipped default provider is permanently unavailable. A readiness model that went
False because the default configuration is the default configuration would
report a fault where there is none. `intelligence_available` is reported and
never required, and `required_agents_ready` is computed over the very same
`REQUIRED_COMPONENTS` list the warm-up path uses — TIDAL, NORO, ZEPHR, RUNE,
VESKA, MARIN — so the two cannot come to disagree about what the platform
needs. LUMEN is not in that list, and this phase does not add it.

## 12. `OperationalSnapshot`

**One canonical aggregation surface**, so a future dashboard, API or operator
tool does not have to rummage through every component.

It carries the session record, component summaries, and each other phase's own
snapshot serialized: Phase 8 coordination, Phase 5 risk state, Phase 6
execution metrics, Phase 7 reconciliation, Phase 9 hedging, Phase 10
intelligence, the portfolio, and recording state.

Compact by construction — those snapshots are themselves compact. A view
carrying every order and every fill would be sized by session history rather
than by what is currently happening.

**No decision is made from any aggregate.**

## 13. Platform start mirror

```python
now = self.clock.now_ms()
self.operations.create_session(self.session_manifest(now, recording=record), now)
self.operations.mark_starting(now)
try:
    ...the existing startup actions, unchanged, with stage calls beside them...
except BaseException as exc:
    self.operations.mark_failed(..., failure=f"{type(exc).__name__}: {exc}")
    raise                      # the SAME exception
self._started = True
self.operations.mark_running(self.clock.now_ms())
```

`Platform.prepare_start()` creates the canonical session record before an
externally owned startup action. The production composition root then calls
`Platform.start_bus()` followed by `Platform.start()`, preserving the historical
bus → storage → feeds order while allowing a bus-start exception to be recorded
against the same session and re-raised unchanged. Direct deterministic callers
of `Platform.start()` retain their prior synchronous-bus semantics; the Phase 11
witness does not force a background dispatcher into tests or replay-style
callers.

## 14. Platform stop mirror

The same shape: `mark_stopping`, shutdown-stage calls beside the existing
teardown, `mark_failed` and a bare `raise` if it fails, then `update_counts`
and `mark_stopped`.

`stop()` ordering is unchanged. A session already marked FAILED gets its
`stopped_at` stamped but is not re-marked STOPPED — a run that failed did not
later succeed at stopping cleanly.

## 15. Recording lifecycle

Recording is first-class operational state, exposed through the snapshot:
`requested`, `active`, `session_id`, `events_recorded`, `storage_backend`,
`config_digest`.

**No recorder behaviour changed.** The buffer, the flush policy, the capacity
bound and the health reporting are exactly as they were.

## 16. Paper tooling

`start-paper.sh` remains the default launcher, unchanged, and still calls
`assert_paper_mode()` before touching the machine. Nothing was added that makes
it capable of anything else.

`docker-compose.yml`:

* `TF_MODE: paper` stays **hard-coded**, deliberately not `${TF_MODE:-paper}`.
  Making it environment-selectable would turn a structural guarantee into a
  variable somebody could set.
* `TF_FEED: ${TF_FEED:-simulated}` and `TF_PROFILE: ${TF_PROFILE:-paper}` are
  parameterised. **Default behaviour is byte-identical to before.**

`status.sh` reports trading mode, profile, feed and execution additively.

## 17. API and query surface

Read-only, all of it:

* `GET /api/operations` — the operational snapshot
* `GET /api/pre-live` — what a live deployment would need
* `GET /health` — gains `profile`, `feed`, `executor`; `mode` unchanged

On the `Platform`: `operational_snapshot(now_ms)`, `current_session()`,
`operational_readiness(now_ms)`, `session_manifest(...)`,
`session_summary(now_ms)`, `pre_live_readiness(now_ms)`.

**No write counterpart exists.** No start, no stop, no profile change. A
running platform's lifecycle belongs to the process that started it, and an
HTTP route that could restart it — or quietly move it to another profile —
would be a control path nobody specified who may use.

`POST /api/kill-switch` and `/api/kill-switch/clear` are untouched. No trade
submission, cancel, manual hedge or promotion endpoint exists.

## 18. `SessionSummary`

Counts and P&L, once a session is over. **Nothing classifies a session** as
good, bad, profitable enough, or ready for anything. A P&L number is a
measurement; what it means about a strategy is research, and what it means about
readiness for real money is a governance question this phase may not answer.

## 19. Retention

`OperationalRegistry.compact` releases nothing by default, and there is no
arbitrary count limit. Even when asked: a running session is never released, the
current session is never released, and a session with an unresolved incident is
never released.

## 20. Future live-readiness role

`PreLiveReadinessSnapshot` records what a live deployment would need against
what exists. **It starts nothing** — no promotion path, no flag it sets, no code
that reads it to permit anything.

Today it reports:

| | |
| --- | --- |
| `paper_framework` … `operator_controls` | `NOT_VALIDATED` |
| `private_venue_connectivity` | `NOT_IMPLEMENTED` |
| `live_executor` | `NOT_IMPLEMENTED` |
| `credential_boundary` | `NOT_IMPLEMENTED` |
| `live_reconciliation` | `NOT_IMPLEMENTED` |
| `deployment_authorization` | `NOT_IMPLEMENTED` |

`live_capable` is a property that returns `False` unconditionally — not a policy
toggle. There is no live executor, no authenticated venue and no credential
path, so there is nothing to flip.

A framework existing is not a framework working. A snapshot that reported
otherwise would be the single most dangerous object in the repository.

## 21. Paper-only boundary

`TradingMode.PAPER` remains the only member. No `TradingMode.LIVE`, no
`TF_MODE=live`, no reinterpretation of `TF_MODE`, and no `TODO` mentioning
either. No `LiveExecutor`, not even a stub. No exchange credentials, no
credential placeholders, no authenticated endpoint, no private WebSocket.

Phase 6's `ExecutionVenueGateway` remains the future seam, with nothing behind
it.

---

## VALIDATION

Phase 11 was audited and remediated against frozen checkpoint
`d768cb1c044849eab3a341365dd750e93697273e`.

The validation suite covers:

* operational registry transition idempotence;
* session-scoped incidents and compaction retention;
* bus-start and recorder-start failure witnessing with exception identity preserved;
* clean and repeated shutdown;
* operational readiness coherence, optional LUMEN handling, recording-disabled sessions, storage failure and kill-switch state;
* the canonical `Platform.operational_snapshot()` path;
* `GET /api/operations` returning a serializable snapshot;
* repeated same-instant operational reads being observational and deterministic;
* no OKAPI target/hedge registry mutation from operational GET/read paths;
* operational metrics matching their authoritative component counters;
* session summaries matching authoritative order/fill/reconciliation/hedge/P&L sources;
* the PAPER-only boundary.

A test-only focused-CI branch mirrored the four Phase 11 audit files into the
existing Python 3.11 unit-test discovery path without changing production code
or CI configuration. On production-equivalent Phase 11 code, all **14 Phase 11
tests passed**. The job result was `1 failed, 1395 passed, 126 skipped`; the
single failure was the pre-existing packaging assertion that treats
`${TF_FEED:-simulated}` as a literal feed value.

On the Phase 11 production branch, the PAPER boundary passes and
`mypy --ignore-missing-imports core` passes. Ruff reports only the same three
pre-existing findings in MARIN/OKAPI that were present before Phase 11.
Python 3.11 unit + contract remains at the known baseline
`1 failed, 1381 passed, 126 skipped` when the focused mirrors are absent.

The backend-enabled Python 3.12 workflow still enters the repository's
pre-existing long-running `Full suite` condition. The same condition was
observed on the Phase 9 base before Phase 10 or Phase 11 remediation, so it is
classified as repository baseline CI debt rather than a Phase 11 regression.

See `docs/phase11-validation-audit.md` for the frozen findings, remediation
mapping, validation evidence and remaining baseline debt.

**Final Phase 11 status: VALIDATED / CLOSED.**
