# Phase 7 — MARIN validation audit

## Status

PHASE 7 AUDIT FROZEN / PRODUCTION REMEDIATION NEXT

Repository: godthefather1952/octopush

Validation branch: validate-phase7-marin

Starting dependency SHA:

aabef89646e2f22f72892d6fb9dd6f9059da1144

Audit head at freeze preparation:

f4ab36d4a70f4500d0adab4f6270468c96d95440

Draft validation PR: #2

Mode: AUDIT ONLY. No MARIN, model, policy, VESKA, orchestrator, risk, kill-switch,
CI, or live-execution production behavior was changed in this audit.

---

## 1. Dependency note

Phase 6 A+B+C was externally revalidated before this branch.

Phase 6 Batch D is patched but its final Python 3.12 repository-wide suite
remains abnormally long-running. Phase 7 therefore treats the exact starting SHA
above as its dependency checkpoint and does not modify Phase 6.

---

## 2. Audit surface

New audit-only files:

- tests/audit/marin_fixtures.py
- tests/audit/test_phase7_marin_sources.py
- tests/audit/test_phase7_marin_registry.py
- tests/audit/test_phase7_marin_resolution.py
- tests/audit/test_phase7_marin_legacy.py
- tests/audit/test_phase7_marin_integration.py

Total focused Phase 7 audit tests: 40

Coverage areas:

- legacy fill/cash/P&L/position reconciliation boundaries;
- sealed-history and compaction safety;
- source capture health and logical time;
- multi-venue truth preservation;
- readiness and startup requirements;
- discrepancy identity and lifecycle;
- registry counters and retention;
- UNKNOWN resolution authority;
- resolution failure handling;
- status-only FILLED resolution;
- reconciliation event/registry ordering;
- orchestrator MARIN cadence;
- paper-only boundary.

The broad legacy mismatch paths already covered by
tests/unit/test_reconciliation.py were not duplicated unnecessarily.

---

## 3. Validation execution evidence

Codespaces focused Phase 7 audit executed the frozen audit head with live
PostgreSQL/Redis backends.

Focused result:

- **32 passed**
- **8 failed**
- **0 skipped**
- **0 errors during collection**
- runtime **0.53s**

One failure was an audit-fixture clock setup error and has been corrected
without production mutation. Six failures reproduce P7-1 through P7-6 as
intended. The remaining failure exposed P7-7 below.

GitHub Actions PR run #100 / run id 34429490752 is also attached to this audit branch.

Completed gates:

- paper boundary: PASS;
- mypy(core): PASS;
- Python 3.11 floor: 1381 passed / 1 failed / 126 skipped;
- the sole Python 3.11 failure is the known out-of-scope packaging baseline:
  TF_FEED defaults to simulated through the compose expression;
- Ruff: exactly the three known repository baseline findings:
  - agents/marin/agent.py I001;
  - agents/marin/source.py I001;
  - agents/okapi/registry.py SIM102.

No audit-file Ruff finding remains.

The repository-wide Python 3.12 full-suite job is still in progress at the time
this inventory is frozen. Its backend contract precheck passed with real
PostgreSQL/Redis. The long-running full-suite behavior is tracked as validation
infrastructure state, not silently converted into a PASS.

The defect inventory below is frozen from explicit invariant tests plus the
direct production mechanisms each test targets. Production remediation may not
weaken or delete those invariants merely to make the suite green.

---

## 4. Confirmed findings

### P7-1 — Multi-venue truth collapses to one venue snapshot

Severity: HIGH

Priority: P1

Finding:

MARIN accepts a list of venue reconciliation sources, but
ReconciliationSnapshot contains only one venue field.

During capture, every successful VENUE source assigns its snapshot to that same
field. A later venue overwrites the earlier venue.

Audit invariant:

test_all_configured_venue_snapshots_survive_one_capture

Reproduction:

1. configure deterministic venue-A and venue-B sources;
2. capture both in one MARIN snapshot;
3. both SourceHealth entries exist;
4. only one VenueTruthSnapshot remains representable in the bundle.

Impact:

A reconciliation bundle can silently omit authoritative truth for one or more
venues. A multi-venue trading platform cannot safely reconcile an account it
cannot represent.

Remediation requirement:

The snapshot/run model must preserve venue truth per venue, not one scalar VENUE
slot. No venue may overwrite another.

Blocks Phase 7 freeze: YES.

---

### P7-2 — Readiness treats configured source identity as usable truth

Severity: MEDIUM

Priority: P1 before any live-readiness integration

Finding:

MARIN readiness checks whether execution_source and account_source objects are
configured. It does not consult the health of the most recent capture.

A configured execution source can explicitly report itself unavailable and
readiness can still return ready=True when the last legacy reconciliation was
clean and no other reason code is present.

Audit invariant:

test_configured_but_unavailable_source_is_not_ready

Reproduction:

1. attach an execution source whose capture reports available=False;
2. attach a healthy account source;
3. set a clean last reconciliation result;
4. capture the failed source;
5. call readiness;
6. readiness derives source availability from object presence rather than the
   failed SourceHealth.

Impact:

The API named readiness can report known-good state while a required internal
truth source is not currently usable. It is observability-only today, which
limits current economic impact, but it is unsafe to wire into live startup in
this form.

Remediation requirement:

Readiness must distinguish configured from recently captured-and-usable source
truth and fail closed on unavailable/incomplete required sources.

Blocks Phase 7 freeze: YES.

---

### P7-3 — Internal source kinds can authorize UNKNOWN resolution

Severity: HIGH

Priority: P1

Finding:

agents/marin/policy.py defines authoritative source kinds as VENUE and OPERATOR.

Marin.apply_order_resolution accepts any ReconciliationSourceKind and forwards
the resolution to VESKA without checking that policy.

Audit invariant:

test_internal_account_source_cannot_authorize_unknown_resolution

Reproduction:

1. create an UNKNOWN order;
2. call apply_order_resolution with source=ACCOUNT;
3. MARIN records ACCOUNT as authoritative_source;
4. MARIN forwards the status to VESKA.

Impact:

One internal platform opinion can be promoted into authoritative evidence for
an order whose venue truth is explicitly unknown. That defeats the purpose of
the UNKNOWN state and the policy module's authority distinction.

Remediation requirement:

RESOLVE_ORDER must require an authoritative source kind and sufficient explicit
evidence before calling VESKA. Internal EXECUTION/ACCOUNT/RECORDED source kinds
must fail closed.

Blocks Phase 7 freeze: YES.

---

### P7-4 — Resolution exception can strand workflow in APPLYING

Severity: MEDIUM

Priority: P2

Finding:

apply_order_resolution records a resolution, moves it to APPLYING, and awaits
veska.resolve_unknown.

If VESKA raises instead of returning a normal rejected result, there is no
exception-finalization path and the resolution remains APPLYING.

Audit invariant:

test_resolution_exception_becomes_terminal_failed_record

Impact:

The registry can permanently describe a failed resolution as still in flight.
That can block retention and mislead operators/recovery tooling about whether an
UNKNOWN order is actively being resolved.

Remediation requirement:

Exception paths must record FAILED with the exception evidence, then re-raise the
original exception if propagation is still desired.

Blocks Phase 7 freeze: YES.

---

### P7-5 — UNKNOWN can be resolved to FILLED without fill economics

Severity: HIGH

Priority: P1

Finding:

MARIN's resolution bridge accepts OrderStatus.FILLED as a status-only answer.

The Phase 6 UNKNOWN resolution surface can transition the order to FILLED
without any accompanying fill quantity, price, fee, cash, or position evidence.

Audit invariant:

test_filled_resolution_requires_fill_economics

Reproduction:

1. submit a non-marketable GTC paper order;
2. force it to UNKNOWN;
3. call MARIN apply_order_resolution with authoritative_status=FILLED and only
   text evidence;
4. the order can become FILLED while filled_quantity remains below quantity and
   no ledger economics were supplied.

Impact:

The platform can accept a terminal FILLED venue claim without the economic facts
needed to reconcile that claim. Legacy MARIN may detect the contradiction later,
but the resolution itself has already accepted an incomplete truth statement.

Remediation requirement:

A FILLED or PARTIALLY_FILLED authoritative resolution must carry authoritative
fill economics and reconcile/apply them atomically, or the status-only bridge
must reject those statuses and require a separate fill-recovery workflow.

Blocks Phase 7 freeze: YES.

---

### P7-6 — Reconciliation workflow registry can outrun event acceptance

Severity: MEDIUM

Priority: P2

Finding:

Marin.run performs reconcile, optional clean compaction, and mirror_result before
publishing RECONCILIATION_COMPLETE / RECONCILIATION_MISMATCH.

If publication fails, in-memory workflow state has advanced while the
reconciliation event never reached the bus.

Audit invariant:

test_clean_run_does_not_commit_registry_before_event_acceptance

Impact:

This is not an economic mutation like Phase 6 PAPER_FILL, and the exception is
loud. However, the in-memory reconciliation workflow and the durable event
history can disagree about whether a run occurred. The registry is currently
memory-only, so replay/observability cannot reconstruct that run from the event
stream.

Remediation requirement:

Define the canonical commit boundary for reconciliation workflow state. Either
publish before committing the mirrored workflow, or make the registry
persistence/event relationship explicit and recoverable.

Blocks Phase 7 freeze: YES.


---

### P7-7 — Workflow capture references nonexistent child snapshot IDs

Severity: MEDIUM

Priority: P1

Finding:

`Marin.begin_reconciliation()` captures standard internal execution/account
snapshots and then attempts to read `snapshot_id` from those child models.

The normal `ExecutionSnapshot` and account snapshot types are event/envelope
models and do not expose that attribute. Codespaces reproduced:

`AttributeError: 'ExecutionSnapshot' object has no attribute 'snapshot_id'`

Audit invariant:

`test_begin_reconciliation_stops_at_capturing`

Reproduction:

1. attach the repository's normal execution/account reconciliation sources;
2. call `begin_reconciliation(now_ms)`;
3. capture succeeds;
4. run-record attachment dereferences a nonexistent child `snapshot_id`;
5. the workflow raises before returning its CAPTURING run.

Impact:

The new Phase 7 reconciliation-workflow path cannot successfully begin with the
platform's standard internal sources. The current orchestrator still uses the
legacy `run()` path, so this does not break today's periodic paper
reconciliation, but it blocks startup/manual workflow adoption.

Remediation requirement:

Run records must reference an identifier that the captured child snapshot
models actually provide (for example their canonical event id), or those
snapshot models must gain one explicit shared snapshot identity contract.
Do not introduce parallel ambiguous ids.

Blocks Phase 7 freeze: YES.

---

## 5. Policy decision — venue requirement in generic readiness

Classification: INCONCLUSIVE / POLICY DECISION

Current behavior:

A clean paper reconciliation with healthy EXECUTION and ACCOUNT sources can
return readiness.ready=True with zero venue sources.

The audit records this behavior in:

test_current_paper_readiness_does_not_require_venue_source

This is not classified as a defect because:

- readiness is observability-only today;
- paper execution has no external private venue account to query;
- prepare_startup_reconciliation separately and explicitly requires EXECUTION,
  ACCOUNT, and VENUE for a future live start.

Before readiness becomes a live execution gate, the product must decide whether
there are separate PAPER and LIVE readiness contracts or one readiness API whose
required source set is parameterized.

---

## 6. Confirmed passing invariants

The audit surface confirms or preserves the following design properties:

- source exceptions become explicit unavailable SourceHealth rather than
  disappearing;
- empty venue truth remains distinguishable from an unavailable source;
- source capture uses supplied logical time;
- healthy and unavailable venue health records can coexist;
- no reconciliation yet means not ready;
- missing execution/account configuration is explicit;
- UNKNOWN orders prevent readiness;
- startup reconciliation requires venue truth;
- discrepancy source-pair identity is commutative;
- repeated discrepancies deduplicate and update occurrences;
- different entities remain distinct;
- a resolved discrepancy reopens if observed again;
- begin_reconciliation stops at CAPTURING;
- mirror_result preserves mismatch severity and does not mutate its input result;
- repeated identical run-status writes do not double-count counters;
- open discrepancies protect run retention;
- pending resolutions protect run retention;
- settled terminal runs can compact while lifetime metrics survive;
- no discrepancy is automatically resolvable under current policy;
- suggested resolution actions are pure;
- a normal VESKA resolution rejection is recorded as FAILED;
- cash tolerance boundaries remain fail-closed above the configured epsilon;
- lifetime fill-count divergence remains detectable;
- UNKNOWN remains a reconciliation warning until resolved;
- critical reconciliation prevents normal fill/order compaction;
- the seal boundary excludes a still-live order;
- a critical result remains in last_result and registry even if event publication
  raises;
- the orchestrator reconciles on first/periodic protected ticks;
- between runs it uses the last reconciliation answer;
- reconciliation_ok reaches the kill-switch input;
- MARIN source code contains no exchange network/credential implementation;
- VenueReconciliationSource remains abstract;
- paper-only boundary remains green.

---

## 7. Known repository baselines

Do not remediate as Phase 7 audit work:

1. tests/contract/test_packaging.py
   compose TF_FEED expression defaults to simulated.

2. Ruff I001
   agents/marin/agent.py

3. Ruff I001
   agents/marin/source.py

4. Ruff SIM102
   agents/okapi/registry.py

The two MARIN import-order findings predate this audit and are not Phase 7
behavior findings.

---

## 8. Recommended remediation batches

### Batch A — Truth integrity

- P7-1 multi-venue truth preservation
- P7-2 readiness source usability
- P7-7 workflow snapshot identity contract

Goal:

One reconciliation snapshot must faithfully state every required account of
truth and readiness must fail closed on missing/incomplete truth.

### Batch B — UNKNOWN resolution safety

- P7-3 authoritative source enforcement
- P7-4 exception terminalization
- P7-5 fill-economics requirement

Goal:

No UNKNOWN order leaves UNKNOWN unless the supplied evidence is authoritative,
complete for the claimed outcome, and fully recorded.

### Batch C — Workflow durability

- P7-6 reconciliation registry/event commit boundary

Goal:

Registry/event history must have one explicit, reconstructible workflow truth.

Do not begin a live venue implementation as part of these batches.

---

## 9. Freeze rule

The audit invariants above are now the Phase 7 defect contract.

During remediation:

- do not weaken, delete, skip or xfail a red invariant;
- do not make an internal source authoritative merely to satisfy a test;
- do not auto-resolve UNKNOWN;
- do not add live exchange credentials or order routing;
- do not collapse multiple venue truth back to one slot;
- do not make readiness optimistic on unavailable/incomplete required sources;
- do not invent fill economics for a FILLED resolution.

Final audit classification:

PHASE 7 FAIL — REMEDIATION REQUIRED

Frozen next state:

PHASE 7 AUDIT FROZEN / PRODUCTION REMEDIATION NEXT
