# Phase 10 — LUMEN validation audit

**Status: AUDIT FROZEN. PRODUCTION UNCHANGED. REMEDIATION APPROVAL REQUIRED.**

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


# Frozen Phase 10 finding register

The superseded Python 3.12 full-suite run reached the Phase 10 audit block before
it was intentionally superseded by an audit-document-only commit. Its progress
showed **20 red assertions** through the relevant early suite region.

The immediately preceding Phase 9 checkpoint, which contains no Phase 10 audit
tests, already shows **one red assertion** in that same region. That is the
known packaging baseline. The delta is therefore:

```text
Phase 10 focused audit invariants: 59
New Phase 10 red invariants:       19
Known baseline red in region:       1
```

Those 19 new reds correspond exactly to the source-proven mechanisms below.
No additional production mechanism is needed to explain them.

## P10-1 — Optional intelligence persistence can become control flow

- **Severity:** HIGH
- **Priority:** P1
- **Invariant:** optional provenance/history persistence must never suppress an
  otherwise valid intelligence reading or provider call.
- **Production evidence:** `IntelligenceRegistry.register_evidence_bundle()`
  calls `store.put_evidence_bundle(...)` directly; `_persist()` calls
  `store.put_analysis(...)` directly. Neither boundary catches store errors.
  `Lumen.evaluate()` begins provenance recording before
  `provider.analyze(...)`.
- **Exposed by:**
  - `test_optional_bundle_store_failure_is_observational_only`
  - `test_optional_analysis_store_failure_is_observational_only`
  - `test_store_failure_cannot_suppress_an_otherwise_valid_lumen_read`
- **Impact:** an optional observability backend can stop LUMEN before it reaches
  the configured provider, or make successful in-memory lifecycle mutation
  appear to fail.
- **Remediation boundary:** make IntelligenceStore write-through best-effort and
  non-authoritative; preserve resident truth; log persistence failure; never
  add blocking retry or route decisions through storage.

## P10-2 — PUBLISHED history can precede actual bus publication

- **Severity:** HIGH
- **Priority:** P1
- **Invariant:** an analysis may be marked PUBLISHED only after its
  `AGENT_OPINION` event has actually been accepted by the event bus.
- **Production evidence:** `evaluate()` calls `_record_outcome()`;
  `_record_outcome()` completes the analysis and calls `link_opinion()`,
  which moves the record to PUBLISHED. Only afterwards does `run_once()`
  call `bus.publish(...)`.
- **Exposed by:**
  - `test_failed_bus_publish_cannot_leave_history_claiming_published`
- **Impact:** a bus failure can leave durable/resident provenance asserting that
  an opinion influenced the platform even though no opinion reached the bus.
- **Remediation boundary:** separate successful analysis completion from
  publication acknowledgement. Record COMPLETED after conversion; transition
  to PUBLISHED/link the opinion only after successful bus publication. Never
  fabricate a replacement opinion.

## P10-3 — RESPONSE_SCHEMA is carried but not enforced

- **Severity:** HIGH
- **Priority:** P1
- **Invariant:** provider output that violates the declared response schema must
  not become an `AgentOpinion`.
- **Production evidence:** provider responses carry arbitrary dict data into
  `_to_opinion()`. That method performs ad-hoc Python conversion rather than
  validating `RESPONSE_SCHEMA`: `bool("false")` becomes true; out-of-range
  attention/direction values are consumed; missing required
  `reason_codes` defaults to an empty list.
- **Exposed by:**
  - `test_schema_rejects_string_boolean_instead_of_reinterpreting_it`
  - `test_schema_rejects_out_of_range_readings`
  - `test_schema_rejects_a_missing_required_reason_codes_field`
- **Impact:** malformed or semantically invalid provider output can produce a
  weighted consensus opinion despite contradicting the schema LUMEN says it
  requested.
- **Remediation boundary:** validate response data once against the authoritative
  schema/typed contract before `_to_opinion()`. Invalid successful responses
  must take the existing malformed/no-publication path. Do not change the
  turbulence formula, prompt or schema to make bad data pass.

## P10-4 — One analysis samples multiple logical instants

- **Severity:** MEDIUM
- **Priority:** P1
- **Invariant:** the provider request, its evidence provenance and analysis
  record must describe one caller-visible logical instant.
- **Production evidence:** `evaluate()` reads `clock.now_ms()` into
  `last_call_ms`; `_context()` reads the clock again for `as_of_ms`, then
  reads the clock again while filtering each headline. The analysis/evidence
  registry uses `last_call_ms`.
- **Exposed by:**
  - `test_analysis_timestamp_matches_the_context_instant_sent_to_provider`
  - `test_headline_window_is_measured_against_the_payload_as_of_instant`
- **Impact:** provenance may describe a different time from the payload actually
  sent, and a headline on the one-hour boundary can be included/excluded merely
  because iteration performed another clock read.
- **Remediation boundary:** sample logical time once per evaluation and pass that
  instant through context construction/provenance. Do not add another wall
  clock or change slow-loop cadence.

## P10-5 — Analysis terminal history and counters are not idempotent

- **Severity:** MEDIUM
- **Priority:** P2
- **Invariant:** first terminal/completion/publication truth wins; re-applying
  the same observation must not rewrite history or increment lifetime counters.
- **Production evidence:** repeated `complete_analysis()` increments
  `analyses_completed` and rewrites `completed_at`; repeated
  `link_opinion()` increments `opinions_published`; completion can rewrite
  the completion timestamp of an already UNAVAILABLE terminal record.
- **Exposed by:**
  - `test_complete_analysis_is_idempotent_and_first_completion_time_wins`
  - `test_link_opinion_is_idempotent_for_the_same_analysis`
  - `test_terminal_unavailable_history_is_not_rewritten_complete`
- **Impact:** historical metrics drift upward and terminal timestamps cease to
  represent the first established fact.
- **Remediation boundary:** make lifecycle mutations transition-aware and
  idempotent without inventing new lifecycle states.

## P10-6 — Intelligence history exposes resident mutable objects

- **Severity:** MEDIUM
- **Priority:** P2
- **Invariant:** public analysis/evidence/opinion reads and captured bundles are
  detached historical values.
- **Production evidence:** `get_analysis()`, `all_analyses()`,
  `recent_analyses()`, `get_bundle()`, `evidence_bundle_list()` and latest
  opinion queries return resident Pydantic objects; evidence registration
  shallow-copies the list but not nested evidence objects.
- **Exposed by:**
  - `test_get_analysis_returns_a_detached_historical_record`
  - `test_recent_and_all_analyses_return_detached_records`
  - `test_evidence_bundle_detaches_caller_owned_items_on_registration`
  - `test_get_bundle_returns_a_detached_bundle`
  - `test_evidence_bundle_list_returns_detached_bundles`
  - `test_latest_opinion_queries_do_not_expose_resident_mutable_refs`
- **Impact:** a dashboard/test/reader can silently rewrite provenance or
  publication history without going through the registry lifecycle.
- **Remediation boundary:** deep-copy mutable models on ingress where caller
  ownership persists and on every public read/snapshot. Internal mutation stays
  resident.

## P10-7 — Provider directory aliases mutable descriptor metadata

- **Severity:** LOW
- **Priority:** P2
- **Invariant:** provider-directory metadata is observational and must be
  detached from caller/read mutation.
- **Production evidence:** `IntelligenceProviderDirectory.register()` stores
  the supplied descriptor object and `get/all/active` return resident
  descriptors.
- **Exposed by:**
  - `test_provider_directory_copies_descriptors_in_and_out`
- **Impact:** external display/metadata code can silently change what provider
  appears active/configured.
- **Remediation boundary:** deep-copy descriptors in and out. Do not turn
  capabilities into provider routing or failover logic.

# Confirmed green Phase 10 boundaries

The same audit/source review found no new defect in these boundaries:

- one provider call per `evaluate()`;
- LUMEN remains outside `required_agents`;
- LUMEN is described as SLOW cadence;
- platform startup does not invoke or require LUMEN;
- RUNE's fast decision path does not call `RuneAI.assess()` or the provider;
- replay declares LUMEN as the external intelligence source and existing replay
  tests replay the recorded opinion;
- no new external `IntelligenceSource` implementation exists beyond
  `LocalHeadlineSource`;
- control kinds remain vocabulary with no LUMEN dispatcher;
- Claude provider descriptors/snapshots do not serialize the API key;
- turbulence formula, signal sign, confidence clamp, TTL bounds and reason-code
  behavior match the pre-Phase-10 contract;
- failed/malformed provider responses publish no fabricated neutral opinion;
- retention protects pending, FAILED, UNAVAILABLE and latest analyses;
- PAPER boundary and non-paper startup refusal pass.

# Frozen remediation batches

## Batch A — Provider/provenance control integrity

Fix:

- P10-1 optional persistence control-flow leakage;
- P10-2 premature PUBLISHED truth;
- P10-3 response-schema enforcement.

Expected primary surfaces:

- `agents/lumen/agent.py`
- `agents/lumen/registry.py`
- optionally a narrowly scoped validation helper/model in
  `agents/lumen/provider.py` or `core/models/intelligence.py` only if needed.

Must not change:

- SYSTEM_PROMPT;
- RESPONSE_SCHEMA semantics;
- turbulence/signal formula;
- confidence or TTL bounds;
- consensus weights/thresholds;
- provider selection/failover/retry policy;
- replay behavior;
- LUMEN optional status;
- PAPER-only boundary.

## Batch B — Logical-time and lifecycle determinism

Fix:

- P10-4 one-analysis/one-instant logical time;
- P10-5 terminal/counter idempotency.

Expected primary surfaces:

- `agents/lumen/agent.py`
- `agents/lumen/registry.py`

Do not alter cadence or use wall-clock time.

## Batch C — Snapshot/history detachment

Fix:

- P10-6 intelligence history aliasing;
- P10-7 provider-directory aliasing.

Expected primary surfaces:

- `agents/lumen/registry.py`
- `agents/lumen/providers.py`

No provider routing/failover behavior may be added.

# Audit classification

```text
PHASE 10 — LUMEN
AUDIT FROZEN / PRODUCTION REMEDIATION NOT STARTED

Focused invariants: 59
New Phase 10 red invariants: 19
Frozen production findings: 7

Paper boundary: PASS
Non-paper startup refusal: PASS
mypy(core): PASS
Ruff: BASELINE ONLY
Python 3.11: BASELINE ONLY
PostgreSQL/Redis backend precheck: PASS
Python 3.12 full suite: intentionally superseded after audit block executed;
                         repository-wide completion remains pending
```

Production remains unchanged on this branch at the time of audit freeze.
