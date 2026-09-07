# Phase 7 — MARIN reconciliation framework

Construction of the reconciliation architecture: truth sources, snapshots, run
and discrepancy lifecycles, the resolution workflow, and the seams a future
live platform will need before it can enable execution.

- **Base SHA:** `92c92112875fe66de94ef80f879d32839f661d20` (`phase6-veska-framework`)
- **Parent Phase 5 baseline:** `1fb9a1bbd4eec558d8f59550cc1b411ae524cadf`
- **Branch:** `phase7-marin-framework`
- **Mode:** **STRICT CONSTRUCTION ONLY.** Not an audit, not a validation pass,
  not a remediation pass.
- **Status:** **VALIDATION DEFERRED — FRAMEWORK CONSTRUCTION ONLY.**

---

## 1. Phase 5 frozen baseline

Phase 5 / RUNE is code complete, test validated and frozen. Nothing under
`agents/rune/**`, `risk/**` is in this diff. Consensus, TIDAL, NORO, ZEPHR and
OKAPI's hedge decisions are untouched.

## 2. Phase 6 dependency

Phase 7 consumes what Phase 6 built rather than reaching around it:

| Phase 6 surface | Used by |
| --- | --- |
| `veska.execution_snapshot(now_ms)` | `ExecutionReconciliationSource` |
| `veska.resolve_unknown(...)` | `Marin.apply_order_resolution` |
| `VenueOrderSnapshot` and friends | `VenueTruthSnapshot` |
| `ExecutionVenueGateway` | the future venue source's documented input |

The new framework never touches `OrderManager.orders` or
`PaperExecutor._pending`. That was the point of Phase 6 building the query
seam. Phase 6's own fill behaviour, routing, sizing and OMS transitions are
unchanged.

The existing `Marin.reconcile()` keeps its direct OMS access. Migrating it is a
behavioural change, and this pass makes none.

## 3. Construction-only philosophy

**Nothing added here decides anything.** The registry takes no safety action.
`readiness` gates nothing. No discrepancy is acknowledged, resolved or ignored
except by an explicit caller. No UNKNOWN order is resolved automatically. No
venue is queried. No threshold moved: `CASH_TOLERANCE` and `QTY_TOLERANCE` are
the same aliases they were, severities are unchanged, and `reconcile_every`
stays at 20.

**The existing algorithm remains the answer.** `reconcile()` is byte-for-byte
unchanged and still produces the `ReconciliationResult` the orchestrator's
protection path reads. `run()` gained one line — a mirror into the registry,
strictly after the result exists.

**Speculative safety rules were declined.** `is_resolvable_automatically`
returns False for everything, and the vocabulary deliberately has no name for
forcing a balance, forcing a position, deleting a fill or inventing one.

## 4. Reconciliation architecture

```
                              MARIN
                                │
     ┌──────────────┬───────────┴───────────┬──────────────┐
     ▼              ▼                       ▼              ▼
 EXECUTION       ACCOUNT                  VENUE         RECORDED
  source         source                  source          source
     │              │                  (interface)    (interface)
     ▼              ▼                       ×              ×
   VESKA        PaperAccount           no impl.        no impl.
 execution_    reconciliation_
 snapshot()      snapshot()
     │              │
     └──────┬───────┘
            ▼
   ReconciliationSnapshot        ← a bundle; declares no agreement
            │
            ▼
    ReconciliationRunRecord      ← workflow lifecycle
            │
            ▼
  ReconciliationDiscrepancy      ← tracked disagreement, deduplicated
            │
            ▼
  ReconciliationResolution       ← proposed response; never self-applying
            │
            └──► future operator / recovery layer
```

Modules:

| Module | Purpose |
| --- | --- |
| `core/models/reconciliation.py` | The Phase 7 vocabulary |
| `agents/marin/source.py` | Truth-source interface and the two local sources |
| `agents/marin/registry.py` | Runs, discrepancies, resolutions, persistence seam |
| `agents/marin/policy.py` | What a discrepancy means. Conservative by design |

## 5. Truth-source separation

Reconciliation only means something if there is more than one account of what
happened. The temptation is to collapse them into one shared mutable object and
call agreement "consistency" — but that object cannot disagree with itself, so
it can never find anything.

| Kind | Answers | Authority | Exists? |
| --- | --- | --- | --- |
| **EXECUTION** | What orders and fills does execution believe exist? | INTERNAL | **Yes** |
| **ACCOUNT** | What cash, positions and P&L does the ledger believe resulted? | INTERNAL | **Yes** |
| **VENUE** | What does the external venue authoritatively report? | AUTHORITATIVE | **No** — interface only |
| **RECORDED** | What does durable event history reconstruct? | DERIVED | **No** — interface only |
| **OPERATOR** | What does a human say, with evidence? | AUTHORITATIVE | Via explicit calls |

`SourceAuthority` is descriptive metadata, not comparison logic. Nothing treats
AUTHORITATIVE as "always right": a venue is definitive about whether an order
exists and says nothing about what the platform intended.

The RECORDED source matters for a reason worth stating: EXECUTION and ACCOUNT
were both written by this platform, so they can be wrong together. The event
log is the only account written for a different purpose.

## 6. Account truth

`PaperAccount.reconciliation_snapshot(now_ms)` — additional to
`PaperAccount.snapshot()`, which returns a `PortfolioState` for the risk and
dashboard paths and is unchanged.

Three differences that matter to a reconciler and not to a risk gate:

- **It takes its instant.** No clock read, so a capture made during replay
  carries the instant the original run recorded.
- **It names the unsealed tail.** `unsealed_fill_ids` alongside the checkpoint
  totals, so a reader can tell which history is still comparable fill-by-fill
  from which is covered only by aggregates. A reconciler that could not tell
  those apart would report a missing fill every time a prefix was sealed.
- **It summarises positions.** `PositionSummary` copies values from
  `PositionState` unchanged — nothing recomputes an economic quantity
  differently from `PaperAccount`, because a reconciler deriving its own P&L
  would be comparing the ledger against this module's arithmetic.

## 7. Execution truth

`ExecutionReconciliationSource` wraps `veska.execution_snapshot(now_ms)`.
VESKA rather than the executor, so plan state is included — plans are part of
what execution believes. The wrapper holds no comparison logic and no state.

## 8. Venue truth

`VenueTruthSnapshot` bundles the Phase 6 transport-neutral models:
`VenueOrderSnapshot`, `VenueFillSnapshot`, `VenuePositionSnapshot`,
`VenueBalanceSnapshot`. No exchange-specific field names anywhere.

`complete` is load-bearing: a paged query that stopped, a rate limit hit
halfway, or a positions call that succeeded while balances did not, must all
set `complete=False`. A reconciler comparing against a silently truncated venue
view reports fills as missing that were never fetched.

**NO AUTHENTICATED VENUE SOURCE IS IMPLEMENTED.** See §17.

## 9. Recorded truth

`RecordedTruthSnapshot` and `RecordedReconciliationSource` are interfaces.
Phase 2's replay engine is untouched and no reconstruction is performed.
`through_sequence` bounds the claim — a reconstruction is only an account of
the events it actually read.

## 10. ReconciliationSnapshot

One captured view of every available source at one logical instant. **A bundle,
and nothing more**: it states what was captured and what was not, and never
says whether the sources agree, because that is a comparison and comparison is
a separate act performed by something testable on its own.

`sources: list[SourceHealth]` is why. A source that fails is recorded as
unavailable with the reason, not dropped. `missing_sources` lets a reader tell
"the venue reported nothing" from "the venue was never asked" — opposite
conclusions, and a bundle that cannot express the second invites a reconciler
to invent agreement out of silence.

`Marin.capture_snapshot(now_ms)` catches any exception a source raises and
records it as unavailable: one broken source must not take the run down.

## 11. Run lifecycle

`ReconciliationRunStatus`: CREATED → CAPTURING → COMPARING →
DISCREPANCIES_FOUND / CLEAN → AWAITING_RESOLUTION → RESOLVING → RESOLVED, or
FAILED.

Distinct from `ReconciliationResult.ok`, which is unchanged and stays the
answer to "did this comparison find a critical mismatch?". This is the state of
the larger process around it — capture, compare, and the resolution that can
outlive many comparisons.

`ReconciliationTrigger` records **why** a run exists: STARTUP, PERIODIC,
POST_FILL, POST_CANCEL, UNKNOWN_ORDER, MANUAL, RECOVERY, REPLAY. The platform
still runs only PERIODIC reconciliation on the existing cadence; **no new
automatic run is triggered anywhere**.

`Marin.begin_reconciliation(now_ms, trigger=..., reason=...)` opens a run and
captures the bundle. It **stops at CAPTURING** and compares nothing —
forcing the existing algorithm through a new engine would change behaviour
under the guise of structure. Nothing calls it.

## 12. ReconciliationRegistry

Runs, discrepancies and resolutions, in memory, keyed by id. Every mutation
takes `now_ms` explicitly; no clock is read (P2-14).

It takes **no safety action**: no kill-switch call, no ledger mutation, no
order resolution, no venue query, and nothing in the platform consults it to
decide whether trading may continue.

`ReconciliationStore` is the persistence seam — an ABC with no implementation.
The registry accepts one and writes through when present, so a later pass adds
a class rather than restructuring the registry. Reads still come from memory;
making it a cache with an invalidation story is a decision for whoever builds
the backend. An in-memory registry is sufficient for a paper session, and
inventing a schema before anyone knows which queries matter would be premature.

## 13. Discrepancy lifecycle

`ReconciliationDiscrepancy` is the workflow record *around* a `Mismatch`, not a
replacement for it. `Mismatch` is unchanged and stays the measurement: one
comparison, one moment. A `Mismatch` cannot answer "is this the same problem we
saw an hour ago?" because it has no identity beyond its own run. That question
is why the discrepancy exists.

`DiscrepancyStatus`: OPEN → ACKNOWLEDGED → RESOLVING → RESOLVED, or IGNORED.
**Nothing moves a discrepancy out of OPEN on its own.** IGNORED in particular
is only reachable by an explicit caller: a reconciler that can quietly decide a
disagreement does not matter is a reconciler that reports agreement it has not
established.

### Identity

`discrepancy_identity(kind, entity_type, entity_id, source_a, source_b)` — one
function, on purpose. Deduplication is the difference between reporting one
persistent cash discrepancy and four hundred copies of it, and getting it wrong
in either direction is bad: too loose and two real disagreements merge, too
tight and every run manufactures a new one.

The basis is what the disagreement is *about*, not what it measured — the same
cash key disagreeing by a different amount next run is the same discrepancy
getting worse. The source pair is ordered so A-vs-B and B-vs-A are one
disagreement. A discrepancy marked RESOLVED that is then seen again is
**reopened**: something concluded it was fixed, and it was not.

Whether this basis is sufficient is a validation question. It is deliberately
simple rather than clever.

### Adapter

`discrepancy_from_mismatch(...)` is pure and lossless in the direction that
matters: kind, severity, key and numbers cross over untouched. It exists so the
current algorithm can feed the new workflow without being rewritten. **No
comparison logic moves, no severity is reinterpreted, no new mismatch is
detected.** Severity keeps the existing WARNING / CRITICAL vocabulary —
`Severity` is reused, not replaced.

## 14. Resolution lifecycle

`ResolutionStatus`: PROPOSED → APPROVED → APPLYING → APPLIED, or REJECTED /
FAILED. Proposal and approval are two steps on purpose: a reconciler that
proposes and applies in one motion is a reconciler that can silently rewrite
the ledger.

`ResolutionAction`: NO_ACTION, REFRESH, REQUERY, RESOLVE_ORDER,
ADJUST_INTERNAL_STATE, REBUILD_FROM_EVENTS, ESCALATE_OPERATOR.

**Nothing performs any of these automatically.** The dangerous operations —
forcing a balance, forcing a position, deleting a fill, inventing a fill — are
absent from the vocabulary entirely, so no future caller can name one by
accident.

`Marin.propose_resolution(...)` records an intention and has no side effect.

## 15. UNKNOWN-order resolution bridge

```python
result = await marin.apply_order_resolution(
    veska, client_order_id, authoritative_status, now_ms,
    source=ReconciliationSourceKind.OPERATOR, evidence={...},
)
```

Phase 6 gave the execution layer one explicit door out of UNKNOWN. This is the
reconciliation side of it: record what is being applied and on whose evidence,
then open the door. The resolution is written before the attempt and updated
with the outcome, so a failed application leaves evidence rather than silence.

**Nothing calls this.** Not `reconcile`, not `run`, not a heartbeat, not a
timeout, not the registry. An order is UNKNOWN precisely because the platform
does not know what happened to it, and the only honest way out is a caller
arriving with an answer from somewhere that does. MARIN adds no judgement: it
does not query a venue, guess a status, or invent a fill.

## 16. Readiness

`marin.readiness(now_ms) → ReconciliationReadiness`. No side effect.

**Observability only.** Nothing gates trading on it. The orchestrator's
protection path still reads `marin.last_result.ok` exactly as before, and no
kill-switch trigger consults it.

`ready` is False whenever anything is unestablished — including when no run has
happened at all. Absence of evidence is not readiness. Reason codes:
`NO_EXECUTION_SOURCE`, `NO_ACCOUNT_SOURCE`, `NO_RECONCILIATION_YET`,
`LAST_RUN_HAD_CRITICAL_MISMATCH`, `OPEN_CRITICAL_DISCREPANCY`,
`UNRESOLVED_ORDERS`.

## 17. Startup reconciliation seam

`marin.prepare_startup_reconciliation(now_ms) → StartupReconciliationRequest`.

Framework only. `Platform.start()` is unchanged and does not block on MARIN; no
venue is queried; paper trading is unaffected.

What it produces is the shape of the requirement: which sources a live start
must have (EXECUTION, ACCOUNT and **VENUE** — listed as required so the request
reports it missing rather than quietly succeeding without it), and the two
conditions that cannot be waived: no unresolved critical discrepancy, and no
order whose venue-side state is unknown.

## 18. Continuous reconciliation seam

Cadence is unchanged. `reconcile_every` stays at 20, the orchestrator's call
site is untouched, and **no new run is triggered**. What Phase 7 adds is the
vocabulary to say why a run happened — a startup run and a post-fill run answer
different questions and should not be indistinguishable afterwards.

## 19. Legacy `reconcile()` compatibility

`reconcile()` is unchanged: same comparisons, same tolerances, same severities,
same result.

`run()` gained one call — `mirror_result(result, ...)` — placed after the
algorithm has produced its answer and after compaction, before publication.
It reads `result` and writes to the registry, **in that direction only**, so it
cannot influence reconciliation, the published event, or the orchestrator's
protection path. Nothing downstream consults the registry.

The stress suite calls `reconcile()` directly and is therefore untouched by the
mirror.

## 20. Future live-money role

A live start will eventually require, in order:

1. connect the private venues;
2. capture authoritative orders, fills, balances and positions;
3. reconcile against internal execution and account truth;
4. establish no unresolved critical discrepancies and no UNKNOWN orders;
5. only then enable execution.

**None of that enforcement is implemented.** This phase ensures MARIN has the
structure to support it: the source kinds, the snapshot shapes, the readiness
question and the startup request all exist, so that phase adds callers rather
than concepts.

## 21. Retention and archival

`ReconciliationRegistry.compact(keep_terminal=True)` releases **nothing** by
default. Even when asked to release, a run holding an OPEN discrepancy or a
pending resolution is never eligible, and no discrepancy or resolution is ever
deleted. Discarding an unresolved disagreement is how a platform decides a
problem stopped existing because it stopped looking.

`ArchivedReconciliationRuns` keeps counts for released runs — the shape
`ArchivedOrders` established. Nothing populates it yet.

`PaperAccount.seal()`, `OrderManager.compact()` and `Marin.compact()` are
unchanged. `Marin.compact_reconciliation_history()` is separate and touches
only the Phase 7 registry.

## 22. Paper-only status

Unchanged. `Veska.__init__` still refuses a non-paper executor.
`PaperExecutor` is still the only concrete `Executor`. Nothing under
`agents/marin/**` or `core/models/reconciliation.py` imports a network client,
handles a credential, or constructs a venue connection. The venue source is an
abstract class with no subclass and no construction site anywhere.

## 23. Events and health

No new event types. `RECONCILIATION_COMPLETE` and `RECONCILIATION_MISMATCH` are
published exactly as before, with the same payload. MARIN's heartbeat logic,
health thresholds and kill-switch consequences are unchanged.

## 24. A note on one import

`core/models/reconciliation.py` imports the venue snapshot models from
`execution/gateway.py` — the one place `core/` reaches into `execution/`. Phase
6 put those transport-neutral shapes there, and duplicating them here to
preserve the layering would create two definitions of venue truth that could
drift, which is worse than one import in one direction. There is no cycle:
`execution.gateway` depends only on `core.models.common` and
`core.models.execution`, neither of which knows this module exists.

Relocating the venue models to `core/models/` is a reasonable later cleanup.
It is not worth churning Phase 6 for during a construction pass.

---

## VALIDATION DEFERRED

Deliberately **not** proven by this pass. Each is a question a later validation
phase exists to answer with evidence:

- **Cash reconciliation correctness** — whether the recomputation genuinely
  catches every way running cash can diverge from the fill log.
- **Position reconciliation correctness** — including sign, average-cost
  accounting and flat-position edges.
- **Fill-set correctness** — both directions, across the sealed boundary.
- **Fee reconciliation** — whether the checkpoint-plus-tail sum is right.
- **P&L reconstruction** — realised, unrealised, and the daily reset.
- **UNKNOWN treatment** — what an UNKNOWN order should do to reconciliation,
  readiness and order capacity.
- **Duplicate-fill reconciliation** — whether the dedupe windows on both sides
  stay in step.
- **Compaction correctness** — whether sealing can ever bury a discrepancy.
- **Sealed-prefix correctness** — whether `_seal_boundary` can manufacture a
  mismatch it then reports.
- **Snapshot consistency** — whether two sources captured at one instant are
  genuinely simultaneous, or can straddle a mutation.
- **Source availability handling** — whether a missing source is always
  distinguishable from an empty one downstream.
- **Discrepancy deduplication** — whether the identity basis is right.
- **Resolution safety** — what evidence should be required before internal
  state may be adjusted, and by whom.
- **Startup readiness** — which conditions must actually gate a live start.
- **Venue-versus-internal authority** — which source wins which question.
- **Recorded-event reconstruction** — whether the event log can rebuild the
  ledger exactly.
- **Replay equivalence** — whether reconciliation reproduces under replay.
- **Resource retention** — what the registry should retain, and for how long.
- **Performance** — the cost of capture and of the query surface under load.
- **Failure recovery** — what happens when a source fails mid-run, repeatedly.

No tests were written for any of these, and none should be inferred from the
structures above. The framework's purpose is to make each testable in
isolation.

## Testing status

**VALIDATION DEFERRED — FRAMEWORK CONSTRUCTION ONLY.**

Nothing in this pass was executed under a test runner, no linter or type
checker was run, and no application was started. No test file was created or
modified.
