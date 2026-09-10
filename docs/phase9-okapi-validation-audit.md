# Phase 9 — OKAPI validation audit

## Status

PHASE 9 AUDIT IN PROGRESS — PRODUCTION UNCHANGED

Repository: godthefather1952/octopush

Validation branch: validate-phase9-okapi

Starting dependency SHA:

07ef4d1a8a215bd819773295f1fda9724dfb4c79

Phase 8 remains PATCHED / VALIDATION PENDING. Phase 9 does not modify Phase 8.

## Audit-only surface

The initial Phase 9 audit adds deterministic adversarial coverage for:

- multi-venue delta aggregation and tolerance boundaries;
- hedge side, size and venue selection;
- selected-leg market-data provenance;
- non-finite desired-target handling;
- readiness and UNKNOWN visibility;
- optional HedgeStore failure isolation;
- hedge lifecycle persistence write-through;
- outcome metric idempotency;
- hedge/target query detachment;
- UNKNOWN active vs outstanding semantics;
- derived hedge status after partial/terminal execution;
- duplicate/ghost request suppression while a hedge is already in flight;
- target-mirror authority separation;
- point-in-time snapshot detachment;
- route-snapshot consistency;
- linkage identity into TradeIntent / plan / orders;
- layering and PAPER-only structural boundaries.

Initial focused files:

- tests/audit/phase9_fixtures.py
- tests/audit/test_phase9_okapi_measurement.py
- tests/audit/test_phase9_okapi_registry.py
- tests/audit/test_phase9_okapi_lifecycle.py
- tests/audit/test_phase9_okapi_snapshots.py
- tests/audit/test_phase9_okapi_integration_and_boundary.py

No Phase 9 production code has been changed by this audit.

## Classification rule

A red Phase 9 invariant is not automatically a production defect. Every failure
must be classified as one of:

- confirmed Phase 9 production invariant failure;
- stale/incorrect audit assumption;
- audit fixture/harness defect;
- environment/CI issue;
- known repository baseline;
- out-of-phase regression.

Only confirmed production failures will be frozen as P9 findings.

Known repository baselines remain out of scope:

- tests/contract/test_packaging.py — `${TF_FEED:-simulated}`;
- agents/marin/agent.py — Ruff I001;
- agents/marin/source.py — Ruff I001;
- agents/okapi/registry.py — Ruff SIM102 (pre-existing at the Phase 9 base).

The PAPER-only boundary may not be weakened during audit or remediation.
