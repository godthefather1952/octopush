# Phase 10 — LUMEN validation audit

**Status: AUDIT IN PROGRESS. PRODUCTION UNCHANGED.**

## Checkpoint

- Repository: `godthefather1952/octopush`
- Audit branch: `validate-phase10-lumen`
- Phase 9 dependency checkpoint:
  `6f5cf33ddb019292a6853a04a5aef2e216807a25`
- Phase 9 state: `PATCHED / FOCUSED USER VALIDATION PENDING`
- Phase 10 framework: `FRAMEWORK CONSTRUCTED. NOT VALIDATED.`

This branch is audit-only until a frozen Phase 10 defect inventory is presented
and an explicit production-remediation prompt is approved.

## Safety boundary

The audit may not:

- introduce live execution or private venue access;
- add credentials, signing keys, private exchange channels or live order routing;
- make LUMEN required;
- change consensus weights or thresholds;
- move model/provider work onto the fast trading loop;
- add an external intelligence network source;
- re-invoke a provider during replay;
- weaken, skip, xfail or delete a valid invariant;
- modify CI merely to manufacture a green result.

## Focused audit surface

The Phase 10 audit currently contains **59 focused tests** plus shared fixtures:

- `tests/audit/phase10_fixtures.py`
- `tests/audit/test_phase10_lumen_provider_and_context.py` — 14
- `tests/audit/test_phase10_lumen_reading.py` — 11
- `tests/audit/test_phase10_lumen_lifecycle.py` — 9
- `tests/audit/test_phase10_lumen_registry.py` — 14
- `tests/audit/test_phase10_lumen_replay_and_boundary.py` — 11

### Areas covered

- exactly one provider call per evaluation;
- prompt/schema identity;
- request/context/provenance consistency;
- headline ordering, one-hour window and bounded body excerpts;
- narrow information-access boundary;
- credential non-disclosure;
- NullProvider and ScriptedProvider behavior;
- response-schema enforcement;
- turbulence, signal, confidence, TTL and reason-code semantics;
- lifecycle truth and publication ordering;
- unavailable vs failed vs malformed outcomes;
- lifecycle/counter idempotency;
- optional persistence isolation;
- registry and evidence snapshot detachment;
- provider-directory detachment;
- retention safety;
- LUMEN optional-agent contract;
- slow-loop/fast-loop separation;
- replayed external intelligence without provider reinvocation;
- no external information-source implementation;
- non-executable control vocabulary;
- PAPER-only architectural boundary.

## Pre-run production hypotheses

The following are **not yet frozen findings**. They are source-derived hypotheses
that the executable audit is designed to confirm or reject.

### H10-A — Optional intelligence persistence may become control flow

`IntelligenceRegistry.register_evidence_bundle()` and `_persist()` currently
write directly to the optional `IntelligenceStore`. A store exception appears
able to escape before or during `Lumen.evaluate()` and suppress a provider
call or an otherwise valid opinion.

### H10-B — Registry publication truth may precede actual bus publication

`Lumen.evaluate()` currently records the valid opinion as PUBLISHED before
`run_once()` publishes the `AGENT_OPINION` event. A bus failure may therefore
leave history claiming an opinion was published when it never reached the bus.

### H10-C — RESPONSE_SCHEMA may be descriptive rather than enforced

The provider abstraction carries `RESPONSE_SCHEMA`, but the common conversion
path appears to consume the returned dict with ad-hoc coercion. The audit probes
string booleans, out-of-range numbers and missing required fields.

### H10-D — Logical time may be sampled more than once for one context

`evaluate()` samples the clock for `last_call_ms`; `_context()` samples it
again for `as_of_ms` and again while filtering headlines. The audit checks
whether provenance and the provider's actual context can disagree at boundary
timestamps.

### H10-E — Lifecycle counters/terminal metadata may not be idempotent

Repeated completion or opinion linkage appears able to increment lifetime
counters and rewrite terminal metadata.

### H10-F — Registry/history query APIs may expose resident mutable objects

Analysis records, evidence bundles and latest opinion references appear to be
returned directly. The audit also checks whether bundle registration shallowly
aliases caller-owned evidence.

### H10-G — Provider-directory metadata may alias caller/read objects

Provider descriptors appear to be stored and returned without deep detachment.

These hypotheses remain provisional until audit execution establishes them.

## Existing repository baselines excluded from Phase 10 remediation

### Python 3.11 packaging baseline

`tests/contract/test_packaging.py::TestComposeMatchesTheConfiguration::test_the_feed_compose_selects_is_a_real_feed`

Known reason:

```text
TF_FEED: ${TF_FEED:-simulated}
```

### Ruff baselines

- `agents/marin/agent.py` — I001
- `agents/marin/source.py` — I001
- `agents/okapi/registry.py` — SIM102

These are not Phase 10 findings.

## Phase 9 CI dependency note

The Phase 9 Python 3.12 full-suite job did not report a test failure. Its backend
precheck completed successfully and the job was cancelled by the workflow host
at the six-hour mark. Phase 9 therefore remains:

`PATCHED / FOCUSED USER VALIDATION PENDING`

rather than being falsely marked frozen.

## Audit exit

After CI:

1. correct audit-only harness defects without weakening invariants;
2. classify every remaining red Phase 10 invariant by root cause;
3. freeze findings as `P10-1`, `P10-2`, ... with severity and priority;
4. define narrow remediation batches;
5. present the exact first remediation prompt;
6. stop before production mutation and request explicit approval.


## Initial PR validation checkpoint

Draft PR #5, candidate audit run #197:

- paper boundary: PASS;
- startup refusal of non-paper mode: PASS;
- mypy(core): PASS;
- Ruff: only the three documented repository baselines;
- Python 3.11: the known packaging baseline only
  (`1 failed / 1381 passed / 126 skipped`);
- PostgreSQL/Redis backend precheck: PASS;
- Python 3.12 full suite: running when this checkpoint was recorded.

No Phase 10 production file has been modified.
