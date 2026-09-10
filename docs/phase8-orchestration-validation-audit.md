# Phase 8 — orchestration validation audit

## Status

**PHASE 8 FAIL — REMEDIATION REQUIRED**

**PHASE 8 AUDIT FROZEN / PRODUCTION REMEDIATION NEXT**

Repository: `godthefather1952/octopush`

Validation branch: `validate-phase8-orchestration`

Starting dependency SHA:

`9a1d5051711e92b68a1f96e6b6ad5f3c8317d84b`

Audit-only checkpoint before this document:

`70952f0708b5b6e0f6046b893f292c6026431d58`

Draft validation PR: **#3**

No Phase 8 production code was modified while the inventory below was built.

---

## 1. Dependency note

Phase 7 MARIN A+B+C is patched at the starting SHA above and remains externally
validation-pending by user choice. Phase 8 treats that exact SHA as its dependency
checkpoint and does not reinterpret Phase 7 findings.

---

## 2. Audit surface

Audit-only files:

- `tests/audit/phase8_fixtures.py`
- `tests/audit/test_phase8_tick_and_persistence.py`
- `tests/audit/test_phase8_consensus_coordination.py`
- `tests/audit/test_phase8_barrier_and_directory.py`
- `tests/audit/test_phase8_trace_and_retention.py`
- `tests/audit/test_phase8_integration_and_boundary.py`

Focused Phase 8 audit surface: **45 tests**.

Coverage:

- canonical tick phase order and failure propagation;
- tick/phase lifecycle idempotency;
- optional coordination-store failure behavior;
- persistence write-through completeness;
- consensus request/evaluation identity and counters;
- consensus snapshot detachment;
- barrier read-only observation;
- agent-directory snapshot semantics;
- decision-trace linking and retention;
- layering (`core` must not import `execution`);
- PAPER-only boundary.

---

## 3. Validation execution evidence

GitHub Actions PR run **#142** / run id `34444198702` executes the frozen audit
checkpoint.

Completed gates:

- paper boundary: **PASS**;
- mypy(core): **PASS**;
- Python 3.11 floor: **1381 passed / 1 failed / 126 skipped**;
- the sole Python 3.11 failure is the known out-of-scope packaging baseline
  `${TF_FEED:-simulated}`;
- Ruff: exactly the three known repository baselines:
  - `agents/marin/agent.py` I001;
  - `agents/marin/source.py` I001;
  - `agents/okapi/registry.py` SIM102.

No Phase 8 audit-file Ruff finding remains.

The Python 3.12 repository-wide full-suite job passed its real PostgreSQL/Redis
backend precheck and is still long-running at inventory freeze time. The
findings below are frozen because each is directly demonstrated by a deterministic
invariant and an unambiguous production mechanism. The long-running repository
job is recorded as validation infrastructure state, not silently converted into
PASS.

---

## 4. Frozen findings

### P8-1 — Tick and phase terminal metadata is not idempotent

Severity: **MEDIUM**

Priority: **P1**

Affected invariants:

- `test_complete_tick_is_idempotent_for_lifetime_metrics`
- `test_fail_tick_is_idempotent_for_lifetime_metrics`
- `test_complete_phase_cannot_rewrite_finished_history`

Production mechanism:

- repeated `complete_tick()` overwrites `completed_at` and increments
  `ticks_completed` again;
- repeated `fail_tick()` overwrites terminal metadata and increments
  `ticks_failed` again;
- repeated `complete_phase()` rewrites the already-finished phase's completion
  instant, verdict and detail.

Impact:

Historical coordination truth and lifetime metrics can change when the same
terminal fact is recorded again. Coordination is observability-only today, so
this does not directly authorize trading, but it undermines deterministic audit
history and replay comparison.

Remediation requirement:

Terminal tick/phase writes must be idempotent. The first terminal fact wins;
repeating it must return the existing record without changing counters,
timestamps, verdicts or detail.

Blocks Phase 8 freeze: **YES**.

---

### P8-2 — Optional persistence can become trading control flow

Severity: **HIGH**

Priority: **P1**

Affected invariant:

`test_optional_store_failure_cannot_break_coordination_call`

Production mechanism:

`CoordinationRegistry._persist_*()` invokes the optional `CoordinationStore`
directly. A store exception escapes from metadata calls such as `begin_tick()`.
If persistence raises while `Orchestrator.tick()` is recording a failure, it can
also mask the original tick exception.

Impact:

The Phase 8 framework explicitly describes coordination as observability only,
yet an optional observability backend can stop or alter the trading control
path. This is a structural boundary violation even though no concrete store is
configured in the current paper build.

Remediation requirement:

Persistence failure must be fail-soft for trading control: retain resident
coordination truth, surface/log the store failure, and never replace the
business/tick exception. No decision may depend on store success.

Blocks Phase 8 freeze: **YES**.

---

### P8-3 — Persistence write-through omits tick mutations

Severity: **MEDIUM**

Priority: **P1**

Affected invariants:

- `test_note_tick_writes_through_when_store_is_configured`
- `test_count_tick_writes_through_when_store_is_configured`
- `test_reentered_phase_metadata_is_persisted`

Production mechanism:

`note_tick()` and `count_tick()` mutate the resident record without calling the
store. Re-entering an already-known phase updates `current_phase` and
`updated_at` but returns before persistence.

Impact:

With a future store configured, resident and persisted coordination history can
disagree even when the store itself is healthy.

Remediation requirement:

Every public mutation that changes a persisted record must write through exactly
once after the resident mutation.

Blocks Phase 8 freeze: **YES**.

---

### P8-4 — Consensus timeout lifetime counter double-counts repeated outcome

Severity: **LOW**

Priority: **P2**

Affected invariant:

`test_timeout_counter_is_idempotent_for_same_barrier_outcome`

Production mechanism:

Every `record_barrier_result(... timed_out=True, complete=False)` increments
`requests_timed_out`, even if the request was already `TIMED_OUT`.

Impact:

Lifetime coordination metrics can overstate timeouts. The barrier's actual
completion decision remains canonical and is not changed.

Remediation requirement:

Increment the lifetime timeout counter only on the transition into TIMED_OUT.

Blocks Phase 8 freeze: **YES**.

---

### P8-5 — Consensus evaluation snapshots shallow-alias mutable inputs

Severity: **MEDIUM**

Priority: **P2**

Affected invariants:

- `test_contribution_snapshot_is_detached_from_consensus_result`
- `test_opinion_reference_snapshot_is_detached_from_caller`

Production mechanism:

`consensus_evaluation_from_result()` uses shallow list copies for
`result.contributions` and `opinion_refs`. The contained Pydantic models are the
same mutable objects supplied by the caller.

Impact:

A historical consensus evaluation can change after it was recorded when a live
contribution/opinion object is later mutated. That makes decision traces
non-historical and weakens replay/audit evidence.

Remediation requirement:

Deep-detach contribution and opinion-reference records at snapshot creation.
Do not recompute scores, thresholds or `allowed`.

Blocks Phase 8 freeze: **YES**.

---

### P8-6 — Agent-directory snapshots alias live descriptors

Severity: **LOW**

Priority: **P2**

Affected invariant:

`test_directory_snapshot_is_detached_from_live_descriptor`

Production mechanism:

`AgentDirectory.snapshot()` passes `self.all()` directly into the snapshot.
`self.all()` returns the resident descriptor objects rather than detached copies.

Impact:

Changing live agent metadata can retroactively alter a previously captured
orchestration snapshot. This is metadata-only but violates snapshot semantics.

Remediation requirement:

Snapshot descriptors must be detached copies while preserving the authoritative
required-agent list exactly as supplied.

Blocks Phase 8 freeze: **YES**.

---

### P8-7 — Closed-trace retention is incorrectly coupled to tick eviction

Severity: **LOW**

Priority: **P3**

Affected invariant:

`test_closed_trace_can_compact_even_without_terminal_ticks`

Production mechanism:

`CoordinationRegistry.compact()` returns immediately when no terminal tick is
eligible/excess. `_release_closed_traces()` therefore never runs in that case,
even when `keep_open_traces=False` explicitly requests closed-trace release.

Impact:

Closed traces and their terminal consensus history can remain resident
indefinitely unless a tick also happens to be evicted. This is a retention/resource
bug, not a decision bug.

Remediation requirement:

Closed-trace compaction must be independently evaluated whenever requested.
Open traces and nonterminal requests must remain protected.

Blocks Phase 8 freeze: **YES**.

---

## 5. Confirmed/preserved invariants

The audit surface preserves or directly verifies:

- canonical `OBSERVE -> SETTLE -> MEASURE -> PROTECT -> MANAGE -> SEEK` ordering;
- SEEK metadata remains inside the existing trading-allowed guard;
- tick failure is recorded and the same exception is re-raised;
- warm-up does not invent later phases;
- a previous unfinished tick remains visible when a later tick opens;
- open request re-registration reuses the current round;
- terminal rounds allow a new round on the same correlation id;
- entry and continuation consensus are distinct records;
- barrier response counts add only newly observed responders;
- consensus evaluation copies the engine's score/agreement/allowed answer rather
  than re-deciding;
- incomplete consensus can preserve `allowed=None`;
- request/evaluation/trace identities link correctly;
- ResponseBarrier observation is read-only and logical-time stamped;
- directory re-registration replaces metadata in place without duplicate IDs;
- required-agent snapshot lists are mirrored rather than re-derived;
- decision traces are one-per-opportunity and their links are idempotent;
- reading a trace does not create one;
- open traces and nonterminal ticks survive aggressive compaction;
- newer same-correlation consensus rounds are not orphaned when an old closed
  trace is released;
- `core/**` has no runtime dependency on `execution/**`;
- gateway venue value types re-export the core model classes;
- Phase 8 adds no credentials, private venue transport or order-routing path;
- paper-only boundary remains green.

---

## 6. Known repository baselines

Out of Phase 8 scope unless behavior changes:

1. packaging contract: `${TF_FEED:-simulated}` compose expression;
2. `agents/marin/agent.py` Ruff I001;
3. `agents/marin/source.py` Ruff I001;
4. `agents/okapi/registry.py` Ruff SIM102.

---

## 7. Remediation batches

### Batch A — Observability/control and persistence integrity

- P8-2 optional store failure must not become control flow;
- P8-3 complete write-through for resident tick mutations.

### Batch B — Idempotent historical truth

- P8-1 terminal tick/phase idempotency;
- P8-4 timeout counter transition idempotency.

### Batch C — Snapshot and retention integrity

- P8-5 deep-detached consensus inputs;
- P8-6 deep-detached agent-directory snapshots;
- P8-7 closed-trace retention independent of tick eviction.

No batch may change consensus thresholds, weights, required-agent policy,
barrier semantics, phase ordering, risk logic, execution behavior or PAPER-only
boundaries.

---

## 8. Freeze rule

The seven findings above are the Phase 8 remediation contract.

During remediation:

- do not weaken/delete/skip/xfail their invariants;
- do not make the coordination registry a decision authority;
- do not recompute consensus in the registry;
- do not change tick phase order;
- do not replace ResponseBarrier as the synchronization primitive;
- do not introduce live exchange connectivity;
- do not make optional persistence success a trading requirement.

Final audit classification:

**PHASE 8 FAIL — REMEDIATION REQUIRED**

Frozen next state:

**PHASE 8 AUDIT FROZEN / PRODUCTION REMEDIATION NEXT**
