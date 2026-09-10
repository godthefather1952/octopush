# Phase 7 — MARIN validation and remediation record

## Status

**PHASE 7 BATCH A+B+C PATCHED / EXTERNAL REVALIDATION REQUIRED**

Repository: `godthefather1952/octopush`

Validation branch: `validate-phase7-marin`

Starting dependency SHA:

`aabef89646e2f22f72892d6fb9dd6f9059da1144`

Frozen audit checkpoint before remediation:

`609a581a78f9edea5185549dd40a61a8a20f26f1`

Draft validation PR: #2

Current production scope: **Phase 7 MARIN remediation only.**

No live venue adapter, credentials, private exchange connection, live execution,
risk-policy change, kill-switch change or CI change is part of this work.

---

## 1. Dependency note

Phase 6 A+B+C was externally revalidated before this branch. Phase 6 Batch D is
patched; its repository-wide Python 3.12 CI exhibited the long-running behavior
tracked separately. Phase 7 does not modify Phase 6 production semantics.

---

## 2. Frozen audit surface

Audit-only files:

- `tests/audit/marin_fixtures.py`
- `tests/audit/test_phase7_marin_sources.py`
- `tests/audit/test_phase7_marin_registry.py`
- `tests/audit/test_phase7_marin_resolution.py`
- `tests/audit/test_phase7_marin_legacy.py`
- `tests/audit/test_phase7_marin_integration.py`

The initial focused audit contained 40 tests.

Codespaces ran that audit with live PostgreSQL and Redis and produced:

- **32 passed**
- **8 failed**
- runtime **0.53s**

One failure was an audit-fixture clock setup error and was corrected without
production mutation. The other failures reproduced the frozen findings below,
including P7-7, which the first static pass had not identified.

The initial platform-start follow-up remained structurally safe:

- PAPER mode;
- SIMULATED feed;
- MARIN healthy;
- VESKA healthy;
- no exchange order-submission implementation.

---

## 3. Known repository baselines

These remain out of Phase 7 scope:

1. `tests/contract/test_packaging.py`
   `${TF_FEED:-simulated}` compose baseline.
2. Ruff I001 — `agents/marin/agent.py`.
3. Ruff I001 — `agents/marin/source.py`.
4. Ruff SIM102 — `agents/okapi/registry.py`.

The Python 3.11 floor before remediation was:

`1381 passed / 1 failed / 126 skipped`

with only the packaging baseline above.

---

# 4. Frozen findings and remediation state

## P7-1 — Multi-venue truth collapsed to one venue snapshot

Severity: **HIGH**  
Priority: **P1**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

`Marin.capture_snapshot()` accepted multiple venue sources but stored each into
one scalar `ReconciliationSnapshot.venue`, so the later venue overwrote the
earlier venue. Codespaces reproduced two configured sources retaining only
`{'B'}`.

### Remediation

`ReconciliationSnapshot.venues` is now the canonical ordered collection of all
successful venue captures. The old singular `venue` surface is a compatibility
property returning the first configured venue; it is no longer storage.

`ReconciliationRunRecord` now carries canonical `venue_snapshot_ids`, and the
registry preserves every venue id. The singular `venue_snapshot_id` remains a
compatibility mirror of the first venue id.

Expected invariant after revalidation:

- A and B both survive one capture;
- an unavailable B does not erase healthy A;
- all venue snapshot ids reach the run record.

---

## P7-2 — Readiness treated configured source identity as usable truth

Severity: **MEDIUM**  
Priority: **P1 before live-readiness integration**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

A configured execution source could capture `available=False` and
`readiness()` still returned `ready=True`, because object presence was treated
as availability.

### Remediation

Readiness now consults the latest matching `SourceHealth` by source kind/name.
EXECUTION and ACCOUNT count as available only when:

- a current capture exists;
- `SourceHealth.usable` is true;
- the corresponding snapshot object is present.

Configured-but-never-captured sources return explicit
`NO_EXECUTION_CAPTURE` / `NO_ACCOUNT_CAPTURE`. Captured-but-unusable sources
return `EXECUTION_SOURCE_UNUSABLE` / `ACCOUNT_SOURCE_UNUSABLE`.

Generic PAPER readiness still does **not** require a venue source. That policy
question remains deliberately separate. Future startup reconciliation continues
to require VENUE.

---

## P7-3 — Internal source kinds could authorize UNKNOWN resolution

Severity: **HIGH**  
Priority: **P1**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

The policy module defined VENUE/OPERATOR as authoritative, but
`apply_order_resolution()` accepted ACCOUNT and forwarded the requested status
to VESKA. Codespaces reproduced an ACCOUNT-sourced cancellation returning
`accepted=True`.

### Remediation

Every order-resolution attempt is still recorded, but MARIN now checks the
existing `is_authoritative()` policy before invoking VESKA.

EXECUTION, ACCOUNT and RECORDED evidence cannot authorize UNKNOWN resolution.
Invalid authority is recorded as `ResolutionStatus.REJECTED` and returns a
structured rejected `ExecutionCommandResult` without calling VESKA.

Authoritative VENUE/OPERATOR requests also require explicit non-empty evidence.
Missing evidence is rejected before application.

---

## P7-4 — Resolution exception could strand workflow in APPLYING

Severity: **MEDIUM**  
Priority: **P2**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

If `veska.resolve_unknown()` raised, the resolution record remained APPLYING.
Codespaces reproduced this exact state.

### Remediation

The VESKA call now has an explicit exception-finalization path:

- resolution status becomes FAILED;
- exception type/message is preserved in notes;
- the original exception is re-raised.

A normal VESKA rejection also remains FAILED.

---

## P7-5 — UNKNOWN could resolve to FILLED without fill economics

Severity: **HIGH**  
Priority: **P1**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

Codespaces resolved an UNKNOWN order to FILLED while:

- `filled_quantity == 0.0`;
- order quantity was `1.0`;
- no quantity/price/fee/cash/position fill economics had been supplied.

### Remediation

The status-only resolution bridge no longer invents economic truth.

For FILLED, MARIN requires already-recorded fill events whose total quantity and
resident `filled_quantity` both reconcile to the full order quantity.

For PARTIALLY_FILLED, MARIN requires already-recorded partial fill economics,
strictly greater than zero and strictly less than total quantity, with resident
quantity matching the fill-event total.

If those economics are absent, the resolution is REJECTED and VESKA is not
called. A separate authoritative fill-recovery path would be required to add
missing economics in the future.

---

## P7-6 — Reconciliation workflow registry could outrun event acceptance

Severity: **MEDIUM**  
Priority: **P2**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

`Marin.run()` previously reconciled, compacted clean history and mirrored the
workflow into the registry before publishing the reconciliation event.
Codespaces injected publication failure and observed a committed registry run
whose completion event was never accepted.

### Remediation

`ReconciliationResult` remains the legacy safety answer and is computed first,
so an injected publication failure still leaves `last_result` available to the
orchestrator safety path.

The reconciliation event is now the commit boundary for derived workflow state:

1. compute legacy reconciliation result;
2. publish `RECONCILIATION_COMPLETE` or `RECONCILIATION_MISMATCH`;
3. only after accepted publication may clean-history compaction occur;
4. only after accepted publication may `mirror_result()` advance the registry;
5. heartbeat follows the committed workflow.

A failed publication therefore leaves registry/compaction uncommitted while
preserving the safety verdict in `last_result`.

---

## P7-7 — Workflow capture referenced nonexistent child snapshot IDs

Severity: **MEDIUM**  
Priority: **P1**  
Status: **REMEDIATED IN CODE — EXTERNAL REVALIDATION REQUIRED**

### Pre-remediation evidence

Codespaces reproduced:

`AttributeError: 'ExecutionSnapshot' object has no attribute 'snapshot_id'`

inside `begin_reconciliation()`.

### Remediation

The run record now uses the execution snapshot's existing canonical Envelope
identity, `event_id`, rather than inventing or dereferencing a nonexistent
parallel id.

Account, venue and recorded snapshot types keep their explicit snapshot ids.
Multi-venue run capture records all venue ids through `venue_snapshot_ids`.

---

# 5. Policy decision retained

## Venue requirement in generic readiness

Classification: **INCONCLUSIVE / POLICY DECISION**

Generic PAPER readiness may be true with healthy EXECUTION and ACCOUNT truth
and zero venue sources. This remains intentional because no authenticated venue
account source exists in this paper-only build.

`prepare_startup_reconciliation()` separately requires EXECUTION, ACCOUNT and
VENUE for a future live start.

Do not convert this policy question into a hidden live-mode implementation.

---

# 6. Preserved invariants

The remediation must continue to preserve:

- legacy fill/cash/P&L/position comparison logic and tolerances;
- warning-only UNKNOWN reconciliation semantics;
- no automatic discrepancy resolution;
- source exceptions represented as unavailable health;
- empty truth distinct from missing/unavailable truth;
- logical-time capture;
- discrepancy identity/deduplication and recurrence reopening;
- registry lifetime counters and conservative retention;
- open discrepancy and pending resolution retention protection;
- critical reconciliation remains visible to the orchestrator through
  `last_result` even if event publication fails;
- first/periodic orchestrator MARIN cadence;
- kill-switch input remains `reconciliation_ok` from the legacy answer;
- `VenueReconciliationSource` remains abstract;
- no credentials/network exchange implementation;
- PAPER-only execution boundary.

---

# 7. Remediation commits

## Batch A — Truth integrity

- `5193f403358f65324cd7427752b331dd62a16130` — truth models;
- `21f1598d7b8c0fb8f04f0e6eb896e0078467eb67` — multi-venue registry ids;
- `feef69127e97426b9bf8636e428b89a2ff692025` — MARIN capture/readiness/workflow identity;
- `a7982f2cdded93edf4312339ea7ea29fad3e8d43` — source-health fixtures;
- `e37d405906b57f752395b91335aa3758ebb91fc7` — source/readiness audit alignment;
- `6c1b8aa290419be077e03fe2fc317327a54f6a5c` — workflow identity audit alignment.

## Batch B — UNKNOWN resolution safety

- `0dbb9306bbe69175ca93b5f485c629947581657a` — resolution audit alignment;
- `ee25dff68058c33f28ffde558e27a5ae709f679d` — authority/evidence/economics/exception remediation.

## Batch C — Workflow durability

- `ee25dff68058c33f28ffde558e27a5ae709f679d` — event commit-boundary production change;
- `1a0fbdab22b964a3c8aeda510309beb2a136f102` — workflow durability audit alignment.

---

# 8. External revalidation required

No post-remediation Codespaces result is recorded yet.

The next focused run must execute all Phase 7 audit modules together and
classify every failure rather than treating red/green as sufficient.

Expected successful remediation shape:

- P7-1, P7-2 and P7-7 truth-integrity tests green;
- P7-3, P7-4 and P7-5 resolution-safety tests green;
- P7-6 workflow-durability tests green;
- legacy Phase 7 reconciliation tests green;
- paper boundary remains green;
- no new audit lint failures;
- known repository baselines remain separately classified.

Do not force an exact pass count if a new legitimate invariant surfaces.

Final Phase 7 status remains:

**PHASE 7 BATCH A+B+C PATCHED / EXTERNAL REVALIDATION REQUIRED**
