# Phase 8 — Orchestrator / consensus coordination framework

**Status: FRAMEWORK CONSTRUCTED. NOT VALIDATED.**

Phase 8 is a strict construction pass. It adds a coordination layer that lets the
platform say *what it did* — which tick, which phase, which agents were asked,
who answered, which consensus produced which intent, which intent produced which
decision, which decision produced which orders.

It changes no decision. Not one threshold, weight, formula, gate, transition,
fill, size, price or route moved. Every model, registry and query added here is
observed by readers and consulted by nothing.

The line this phase must not cross is short enough to state once:

> A coordination registry that starts influencing decisions stops being a record
> and becomes a second, untested decision authority — one nobody knows is there.

Everything below is downstream of that sentence.

---

## 1. Phase 5–7 frozen baseline

Phase 5 closed RUNE's risk boundary. Phase 6 built VESKA's execution framework.
Phase 7 built MARIN's reconciliation framework. All three remain frozen here.

Explicitly unchanged by this phase:

* `ConsensusEngine.combine` — the weighted-mean arithmetic, `_effective_weight`,
  abstention handling, missing-agent handling, degraded weighting.
* `ConsensusEngine.entry_allowed` and `continuation_allowed`.
* `ConsensusConfig` — weights, `required_agents`, `entry_threshold`,
  `exit_threshold`, `degraded_grace_ms`, `degraded_weight_factor`,
  `agent_response_timeout_ms`.
* `ResponseBarrier.expect`, `record`, `wait`, `forget`, `responded`,
  `outstanding`, `late_responses` — every waiting and completion semantic.
* RUNE's gates and sizing, VESKA's planning and order lifecycle, MARIN's
  reconciliation algorithm and tolerances, OKAPI, TIDAL, NORO, ZEPHR, LUMEN and
  the cross-venue detector.
* The canonical tick order: OBSERVE, SETTLE, MEASURE, PROTECT, MANAGE, SEEK.
* The paper-only boundary. No credential, transport, endpoint, key, signature or
  exchange SDK appears anywhere in this phase.

## 2. The layering repair Phase 7 deferred

Phase 7 flagged one knowingly-accepted trade: `core/models/reconciliation.py`
imported the venue value types from `execution/gateway.py`, which made `core/`
depend on `execution/` — backwards in a repository where the dependency has
always run the other way.

Phase 8 performs the cleanup it promised:

* The six transport-neutral value models — `VenueOrderAck`, `VenueOrderSnapshot`,
  `VenueFillSnapshot`, `VenuePositionSnapshot`, `VenueBalanceSnapshot`,
  `VenueGatewayCapabilities` — now live in `core/models/venue_execution.py`.
* `ExecutionVenueGateway` stays in `execution/gateway.py`. An interface belongs
  to the layer that implements it; a value type does not.
* `execution/gateway.py` re-exports all six, so
  `from execution.gateway import VenueOrderSnapshot` keeps working and there is
  exactly one canonical definition of each class.
* `core/**` no longer imports from `execution/**`. The only remaining mention of
  `execution` inside `core/` is one docstring sentence explaining the move.

Nothing about reconciliation, execution or the paper boundary changed — only
where three hundred lines of value types are defined.

## 3. Construction-only philosophy

Every addition in this phase obeys four rules.

**Nothing reads it to decide.** The orchestrator writes to the coordination
registry beside code that already ran. No branch, guard, threshold or return
anywhere consults what the registry holds. Deleting the registry entirely would
leave the platform's behaviour identical.

**Nothing is recomputed.** `ConsensusEvaluationRecord` copies `score`,
`agreement`, `complete`, the contributions and the missing/degraded/abstained
sets straight off the `ConsensusResult` the engine produced. If it recomputed
them the platform would have two consensus engines, and the one nobody tested
would eventually win an argument.

**Nothing waits.** `ResponseBarrier` decides who answered.
`CoordinationRegistry.record_barrier_result` copies that answer down.

**Nothing is swallowed.** A tick that raises is recorded as FAILED and the same
exception is re-raised, untouched — no catch, no retry, no translation.

## 4. Coordination architecture

```
Orchestrator.tick()
  │
  ├── _observe()                      → MarketState, and THE tick time
  │
  └── _tick_body(market)
        ├── coordination.begin_tick(now, ...)
        ├── OBSERVE   (recorded closed: it produced `market`)
        ├── SETTLE    → _settle()
        ├── MEASURE   → _measure(), _refresh_risk_utilization()
        ├── [warm-up may return here]
        ├── PROTECT   → _protect()
        ├── MANAGE    → _manage()
        └── SEEK      → _seek()          (only when trading is allowed)

  on success → coordination.complete_tick(now)
  on raise   → coordination.fail_tick(now, error=...)  then  raise
```

Every `coordination.*` call above sits *beside* code that already existed. No
call was moved, no branch introduced, no generic phase dispatcher built. That
restraint is deliberate: a phase dispatcher elegant enough to justify itself
would have had to change control flow, and control flow is what this phase is
not allowed to touch.

## 5. Two lifecycles, again

The platform already keeps two distinct lifecycles apart in three places, and
Phase 8 adds the fourth:

| One thing | The workflow around it |
| --- | --- |
| `OrderStatus` — one order at a venue | `ExecutionPlanStatus` — the plan those orders work |
| `ReconciliationResult.ok` — one comparison | `ReconciliationRunStatus` — the run |
| `ConsensusResult.complete` — did every required agent supply a usable opinion? | `ConsensusRequestStatus` — the coordination around asking |

`ConsensusResult.complete` is unchanged and remains the answer to "did every
required agent supply a usable opinion?". `ConsensusRequestStatus` is the state
of the *request*: CREATED, WAITING, READY, TIMED_OUT, COMPLETED, FAILED.

TIMED_OUT is not an error. A missing agent is an expected outcome the consensus
engine already models — an absent agent is never a neutral vote — so the
platform decides with what arrived, exactly as before.

A correlation id names one *round* of asking, not the opportunity's whole life.
Re-registering while a round is still open returns the record already held — the
platform legitimately re-registers when a wait is re-entered. Registering after a
round finished, or for a different purpose, opens a new record and moves the
correlation lookup to it while keeping the finished one: an opportunity is asked
about again on every tick it is held, and folding a continuation into the entry
record would leave the record claiming an entry consensus was computed at exit
time.

## 6. Tick lifecycle records

`OrchestrationPhase` names the six working phases plus `IDLE` (the absence of a
phase, deliberately excluded from `TICK_PHASE_ORDER`).

`OrchestrationTickStatus` has CREATED, RUNNING, COMPLETE and FAILED. There is no
CANCELLED: the platform has no concept of cancelling a tick in flight, and a
value the system cannot reach would invite someone to implement a behaviour to
justify it.

`OrchestrationPhaseRecord` records a phase's start and completion as **logical
instants supplied by the caller** — not wall-clock durations. A phase that took
40ms of real time is a performance measurement; a phase that ran at tick instant
T is what replay has to reproduce.

`OrchestrationTickRecord` carries the tick number, the three timestamps, the
status, the current phase, the phase records, the market timestamp, whether the
tick ran during warm-up, five counters, an error string and free-text notes.

## 7. Where OBSERVE is recorded, and why

OBSERVE is recorded as opened *and closed* at the top of `_tick_body`, not around
`_observe()` itself. The reason is `tick_time`: the tick's one logical instant is
`market.created_at`, which does not exist until `_observe()` returns. A phase
stamped from a clock read taken before that would not survive replay — the exact
class of divergence P2-14/P2-15 closed.

So the record says what is true: OBSERVE happened, and it happened at the instant
the snapshot it produced carries.

A tick that raises inside `_observe()` produces no tick record at all, because no
logical time exists to stamp one with. That is recorded here as a known
limitation rather than papered over with a clock read.

## 8. Tick failure

`fail_tick` marks the record FAILED, stamps `completed_at`, stores the exception's
type and message, and closes whichever phase was open with `ok=False`.

Then `tick()` re-raises the exception it caught — the same object, by a bare
`raise`. The registry is a witness, not a handler. A framework that turned a
raised tick into a tidy note would be making a recovery decision nobody asked for,
and the caller would never learn the tick did not run.

`begin_tick` likewise leaves a previous tick still marked RUNNING exactly as it
is. That state is evidence — it means a tick neither completed nor was recorded
as failed — and quietly closing it would erase the one trace of whatever went
wrong.

## 9. Consensus purpose

The platform has always asked two different questions of the same machinery,
against two different thresholds: *should we open this?* and *should we stay in
it?* Until now the difference lived only in which method the caller happened to
call.

`ConsensusPurpose` names it: ENTRY and CONTINUATION. Neither threshold moves.
ENTRY is still measured against `entry_threshold` by
`ConsensusEngine.entry_allowed`; CONTINUATION is still measured against
`exit_threshold` by `continuation_allowed`. The purpose is a label on the record,
not an input to the comparison.

## 10. `ConsensusEvaluationRecord` and the `allowed` field

Every number on the record is copied. The one field that could have been derived
— `allowed` — is not.

`allowed` is what `entry_allowed` or `continuation_allowed` actually returned,
passed down from the call site that invoked it. Deriving it from `agreement` and
a threshold would be re-deciding, and a record that re-decides can disagree with
the decision it is recording.

The orchestrator calls the engine **once** and reuses the answer for both the
branch and the record, so the two cannot diverge even in principle:

```python
entry_allowed = self.consensus.entry_allowed(result)
self._record_consensus(..., allowed=entry_allowed)
if not entry_allowed:
    await self._reject(record, "CONSENSUS_BELOW_THRESHOLD")
```

On the incomplete path `allowed` is `None`, not `False`: `entry_allowed` was
never called there, and writing down an answer nobody asked for would be the
record deciding. Incompleteness is already visible on `complete` and
`missing_agents`.

Thresholds appear on the record so a reader can see what the agreement was
measured against. They are carried, never applied.

## 11. `OpinionReference`

A compact reference to each opinion a consensus actually used: agent, subject,
creation and expiry instants, quality, signal, confidence, abstention flag and
model version.

Not the opinion object itself. A record holding live `AgentOpinion` references
would keep a mutable view of state that has since moved on, and a record of what
was used has to describe the moment it was used.

The references are built inside `_consensus_with_opinions` from the very dict the
engine was handed, so the record cannot describe a different input set than the
decision used. `_consensus_for` still exists and still returns exactly what it
returned before; it now delegates.

## 12. Barrier query surface

`ResponseBarrier` is deliberately short-lived: it forgets a correlation id the
moment its wait resolves. Correct for a synchronisation primitive, useless for
inspection. Phase 8 adds three read-only views:

* `snapshot(correlation_id, now_ms) -> BarrierSnapshot | None`
* `pending_ids() -> list[str]`
* `all_pending_snapshots(now_ms) -> list[BarrierSnapshot]`

None of them registers, records, completes or forgets anything, so calling one
cannot change what a concurrent `wait` returns. `now_ms` is supplied by the
caller, never read from the clock.

`snapshot` returns `None` for an id that was never registered *and* for one
already forgotten — indistinguishable on purpose. The barrier keeps no history,
and inventing one would make a completed wait look outstanding forever.

`expect`, `record`, `wait`, `forget`, `responded`, `outstanding` and
`late_responses` are byte-for-byte unchanged.

## 13. `CoordinationRegistry`

`apps/orchestrator/coordination.py`. Holds tick records, consensus request
records, consensus evaluation records and decision traces, in memory, keyed by
id, with lifetime counters that no compaction disturbs.

Mutations: `begin_tick`, `enter_phase`, `complete_phase`, `complete_tick`,
`fail_tick`, `note_tick`, `count_tick`, `register_consensus_request`,
`record_barrier_result`, `record_barrier_snapshot`, `record_consensus`,
`fail_consensus_request`, the `link_*` family, `update_trace_state`, `compact`.

Queries: `get_tick`, `current_tick`, `recent_ticks`, `get_consensus_request`,
`request_for_correlation`, `get_consensus_evaluation`, `pending_requests`,
`trace_for_opportunity`, `get_trace`, `metrics`.

**Every mutation takes `now_ms` explicitly.** Nothing in the module reads a
clock, so a replayed session records the instants the original recorded.

Metadata calls return `None` rather than raising when there is no open tick or no
trace. That is not laziness: a bookkeeping call that raised because a record was
missing would let the record break the thing it records.

## 14. `DecisionTrace`

One trace per opportunity — the unit the platform's whole lifecycle is organised
around; any other key would need translating at every step.

The trace holds **identifiers, not copies**: consensus evaluation and request ids,
an intent id, a risk decision id, execution plan ids, order ids, reconciliation
run ids, a trade reference, and a mirror of the opportunity's state. Phase 6's
`ExecutionRegistry` owns plan truth and Phase 7's `ReconciliationRegistry` owns
run truth; duplicating either here would create a second version that could drift
from the first.

`reconciliation_run_ids` is left empty in the tick path rather than invented.
Reconciliation is periodic and platform-wide, not per-opportunity, so most traces
will carry none — and an invented relationship is worse than an empty field.

`update_trace_state` mirrors `OpportunityRecord.state` onto the trace after each
transition. The record stays authoritative: a mirror that disagrees is stale,
never right.

## 15. Agent directory

`apps/orchestrator/agent_directory.py`. A phone book, not a switchboard.

`AgentDescriptor` holds an id, service name, version, subject scope
(OPPORTUNITY / SYMBOL / GLOBAL / UNSPECIFIED), cadence (FAST / SLOW /
EVENT_DRIVEN / UNSPECIFIED), a mirrored `required_by_default` flag, a mirrored
weight and a description. No callable, no address, no credential, no transport.
You cannot invoke an agent through this object — a directory that could dispatch
would be a second message path competing with the bus.

`AgentDirectory` offers `register`, `register_descriptor`, `forget`, `get`,
`all`, `ids`, `required`, `by_scope`, `by_cadence`, `contains` and `snapshot`.

**`ConsensusConfig.required_agents` remains the authority.** The directory
mirrors it for display. If the two ever disagree, the configuration is right and
the directory is stale. `AgentDirectory.snapshot` takes the authoritative list as
an argument and copies it verbatim rather than reconciling — a display that
quietly "corrected" configuration would hide precisely the drift worth seeing.

Registration is metadata only, so a missing registration cannot break trading: an
unregistered agent still publishes opinions, is still tracked by the barrier, and
is still weighed by the consensus engine. It is simply absent from a display.

`wiring.py` registers the eight agents this build constructs, mirroring
`settings.consensus.weights` and `required_agents` into each descriptor. Nothing
in `wiring.py` writes to `settings`.

## 16. `AgentEndpointDescriptor` — a note about a shape

Nothing constructs or uses one. It records where a future remote participant
would live, and deliberately does not become a transport.

The `EventBus` is already the platform's message transport. Adding a second one
would give the system two ways for an opinion to arrive and two sets of ordering
guarantees to reconcile. A future distributed agent publishes `AgentOpinion` onto
the same bus, is tracked by the same `ResponseBarrier`, and is combined by the
same `ConsensusEngine`. All this descriptor would record is which process it
happens to run in.

## 17. Readiness — reporting only

`ComponentReadiness`, `CoordinationReadiness` and `PlatformReadinessSnapshot`
answer "is the platform in a fit state?" and **gate nothing**.

Warm-up still decides when trading may begin. The kill switch still decides when
it must stop. `ConsensusResult.complete` still decides whether a consensus
counts. Substituting a readiness model for any of them would replace three tested
decisions with one untested one.

`ready` is False whenever anything is unestablished, *including when nothing has
been checked*. Absence of evidence is not readiness.

`Orchestrator.coordination_readiness()` assembles the reasons a future shadow or
live start would have to clear: warm-up incomplete, a required agent unhealthy,
the kill switch engaged, UNKNOWN orders outstanding, reconciliation not clean.
It is called by nothing in the tick path.

## 18. Control plane — vocabulary only

`OrchestrationControlKind` names seven operator actions: PAUSE_NEW_ENTRIES,
RESUME_NEW_ENTRIES, REQUEST_RECONCILIATION, ENGAGE_KILL_SWITCH,
CLEAR_KILL_SWITCH, CANCEL_ALL, FLATTEN.

**Nothing here is executable.** No dispatcher, no route, no handler. The only
operator control that currently works is the existing kill-switch API, and this
phase does not touch it.

Naming an action is not implementing it, and deliberately so: a control model
that could be dispatched would be an unguarded path into the trading loop, built
before anyone decided who may use it or what it must check.
`OrchestrationControlRequest.requested_by` is a free-text label, not an identity
the platform authenticates — building an auth model before there is anything to
authorise would be inventing a security boundary nobody has specified.

## 19. Metrics

`CoordinationMetrics` is counters. No thresholds, no rates, no adaptive anything.

Explicitly *not* an input to consensus. No weight, threshold or decision anywhere
reads these. A platform that let its own hit rate move its consensus weights
would be optimising against its own history, which is a strategy decision and a
much later one. The Scorecard is likewise never read to modify weights.

## 20. `OrchestrationSnapshot`

One serializable view of the whole coordination layer, so a future dashboard, API
or operator surface can inspect tick state, agent state, consensus state,
workflow state and readiness **without reading orchestrator internals**.

Compact references and counts rather than embedded snapshots: a view that carried
every order and every fill would be sized by session history rather than by what
is currently happening. Execution and reconciliation appear as counts and a
boolean, sourced from `Veska.metrics()` and `Marin.last_result` — the registries
that own those facts.

`created_at` is supplied by the caller and never read from a clock.

## 21. Orchestrator query surface

Added, all read-only, none called from the tick path:

* `current_tick_record()`, `recent_ticks(limit)`
* `consensus_requests()`, `consensus_evaluations()`
* `trace_for_opportunity(opportunity_id)`
* `agent_directory_snapshot(now_ms)`, `barrier_snapshots(now_ms)`
* `coordination_readiness(now_ms)`, `coordination_snapshot(now_ms)`

`trace_for_opportunity` never creates. An opportunity the platform never worked
has no trace, and inventing an empty one to avoid returning `None` would make "we
have no record of this" indistinguishable from "this happened and produced
nothing".

## 22. Retention

`CoordinationRegistry.compact` releases finished coordination records and is
conservative by construction. **With the defaults it releases nothing.** The
framework supplies the hook and the safety rules; the policy belongs to a later
pass that has measured what retention actually costs.

Even when asked to release, these are absolute:

* the tick currently open is never released;
* a tick that is not terminal is never released;
* an open trace — one whose opportunity has not reached CLOSED or REJECTED — is
  never released;
* a consensus request that has not reached a terminal coordination state is never
  released, and neither is the evaluation it points at.

Dropping any of those is how a platform decides a question stopped existing
because it stopped tracking the answer. Lifetime counters are unaffected by
compaction, so a compacted session still reports what it did.

## 23. `CoordinationStore` — a seam with nothing behind it

An ABC with four methods: `put_tick`, `put_consensus_request`,
`put_consensus_evaluation`, `put_trace`. **No implementation exists, and none is
built here.**

In-memory is right for a paper session, and choosing a schema now would fix the
shape of queries nobody has written yet. The seam is real rather than decorative:
`CoordinationRegistry` accepts a store and writes through when one is present, so
a later pass adds a class instead of restructuring the registry. Reads still come
from memory — turning the registry into a cache needs an invalidation story, and
that belongs with whoever implements the backend.

## 24. Paper-only status

Unchanged and unchallenged by this phase. `Veska.__init__` still refuses any
executor whose `is_paper` is False. Phase 8 adds no credential model, no secret
loader, no authenticated REST, no private WebSocket, no signed request, no
wallet, no withdrawal path, no real balance lookup and no real order lookup —
not stubbed, not commented out, not "for later".

`ExecutionVenueGateway` remains what Phase 6 made it: an interface with no
implementation, no subclass and no construction site anywhere in the running
platform. Moving six value types out from under it changed none of that.

## 25. What was deliberately *not* built

* No live executor, and no step toward one.
* No generic phase dispatcher. It would have changed control flow.
* No second transport for agents. The bus is the transport.
* No control-plane dispatcher, route or handler.
* No adaptive weighting, no historical-P&L feedback into consensus.
* No automatic resolution of anything — UNKNOWN orders, open discrepancies and
  pending resolutions all stay exactly as Phases 6 and 7 left them.
* No persistence backend.
* No readiness gate wired into `Platform.start()`.

---

## VALIDATION DEFERRED

**Nothing in this phase has been validated.** The framework compiles, imports,
type-checks under the repository's gated mypy surface, passes Ruff, and was
exercised by hand (see *Construction checks* below) — none of which is evidence
that it is *correct*. Everything below is untested and must be treated as
unproven until a dedicated audit phase says otherwise.

### Consensus arithmetic and semantics

* **Consensus arithmetic** — the weighted mean itself, unchanged since before
  this phase, remains unvalidated by any Phase 8 work.
* **Agreement semantics** — what `agreement` means when contributions disagree in
  sign, and whether the value the record copies carries the meaning a reader will
  assume.
* **Negative-score semantics** — a strongly negative score with high agreement is
  a confident vote *against*; nothing here has been shown to represent that
  correctly.
* **Entry / continuation threshold correctness** — whether 0.60 and 0.45 are the
  right numbers against the achievable range. Phase 8 carries them onto records;
  it offers no evidence about them.
* **Missing required agents** — the platform treats a missing required agent as
  incomplete, hence a rejection. The recorded `missing_agents` set has not been
  shown to agree with the engine's own view in every path.
* **Degraded-agent weighting** — `degraded_weight_factor` and the grace window.
* **Abstention semantics** — the three-state distinction (missing / informative /
  abstaining) and whether `OpinionReference.abstain` preserves it faithfully.
* **Zero-confidence semantics** — an opinion with `confidence == 0.0` and what
  the record should say about it.

### Coordination and transport

* **Late responses** — an opinion arriving after `forget`. `late_responses` counts
  them; nothing validates that the count is complete or that the consequence is
  benign.
* **Duplicate responses** — the same agent recording twice for one correlation id.
* **Out-of-order opinions** — an opinion for a later tick arriving before an
  earlier one.
* **Wrong-correlation opinions** — an opinion whose correlation id names a
  different opportunity, or none.
* **Barrier timeout races** — a response landing in the same instant the deadline
  expires.
* **Barrier registration races** — `expect` and a response interleaving; the
  registration-before-publish ordering is asserted by construction, not proven.
* **Distributed bus behaviour** — every claim about remote agents is theoretical.
  This build has only run in-process, where `bus.drain()` happens to settle
  everything.
* **Agent failure recovery** — what the coordination records look like after an
  agent dies mid-request and returns.
* **Agent health / readiness correctness** — whether `coordination_readiness`
  reports what an operator would need, and whether its reason codes are complete.

### Tick, trace and registry

* **Tick phase ordering** — that the recorded phase sequence always matches what
  executed, including on every early-return path.
* **Tick failure recording** — verified by hand for one raise site; not proven for
  a raise inside `_observe()` (which produces no record at all, by design), inside
  `_publish_state`, or from a bus handler.
* **Decision-trace completeness** — whether every opportunity that reaches
  execution has a trace carrying every id it should.
* **Trace identity** — one trace per opportunity is asserted; behaviour under a
  re-detected opportunity id has not been examined.
* **Registry retention** — `compact` is untested beyond a hand check of its
  safety rules. The default releases nothing, which bounds the risk but does not
  validate the code.
* **Snapshot consistency** — whether `coordination_snapshot` can observe a
  half-updated state when called from outside the tick.

### Determinism

* **Logical-time equivalence** — every mutation takes `now_ms`; that no clock read
  leaked into a coordination path is asserted by inspection, not proven.
* **Replay equivalence** — whether a replayed session produces byte-identical
  coordination records. This is the single most important unvalidated property in
  the phase.

### Operations

* **Control-plane safety** — vocabulary only, so there is nothing to execute
  incorrectly today; the moment anything dispatches one of these, every question
  about authorisation, ordering and idempotency becomes live and none is answered
  here.
* **Startup readiness** — `PlatformReadinessSnapshot` describes a sequence
  `Platform.start()` does not perform.
* **Performance** — the per-tick cost of the coordination writes has not been
  measured.
* **Resource bounds** — the registry grows for the lifetime of the process under
  default retention. No bound has been established, and no measurement supports
  choosing one.

---

## Construction checks

Per the phase brief, **no tests were written, changed or run**, and no existing
test file was touched.

What was done, and disclosed here so it is not mistaken for validation:

* Ruff across `apps/`, `core/` and `execution/` — clean.
* `mypy --ignore-missing-imports core` (the repository's gated surface) — clean.
* Module import checks for every new and modified module.
* Three hand-run scratch scripts, kept outside the repository, that:
  1. built the platform and ran 30 ticks, confirming the six phases are recorded
     in canonical order with COMPLETE status and that the snapshot serializes;
  2. drove one crafted opportunity through `_evaluate_or_defer` with all three
     required agents answering, confirming the request record, barrier outcome,
     consensus evaluation (`allowed=True`, score and agreement matching the
     engine), opinion references, trace links and metrics;
  3. forced a raise inside `_settle`, confirming the *same* exception escapes
     `tick()`, the record reads FAILED with the open phase closed `ok=False`, and
     `_tick_time` is reset — and confirmed `compact()` releases nothing by
     default and never releases the open tick, an open trace or a pending
     request.

None of that is a test suite. It is a construction check, and the VALIDATION
DEFERRED list above stands in full.

## Testing status

**No Phase 8 tests exist.** The audit phase that validates this framework has not
been run. Treat every behaviour described in this document as constructed and
unproven.
