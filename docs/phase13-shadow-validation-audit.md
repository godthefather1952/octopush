# Phase 13 — Shadow validation audit

**Disposition: PHASE 13 AUDIT — COMPLETE / FINDINGS FROZEN.**
**P13-B1 — REMEDIATED IN CODE / FIELD VALIDATION REQUIRED (Batch A).**
**All other findings — NOT REMEDIATED.**

This is the first Phase 13 document in the repository. The audit sections below
are as originally frozen; a remediation-status section is appended at the end,
along with a correction to the roadmap label this document initially proposed.

**Roadmap note, stated up front because §3 below gets it wrong:** the official
roadmap is `PHASE 13 = Venue Expansion`, not the "Shadow Evidence Framework"
that §3 proposes. That proposal is withdrawn — see *Roadmap-label correction*
at the end. The findings themselves were derived from code and stand; the ones
concerning the shadow evidence layer are retained as follow-through findings
for whichever phase owns that work.

## 1. Checkpoints

| | |
|---|---|
| Starting branch | `remediate-phase12-live-feed-final` |
| Starting SHA | `6f4416b0932a1a8b447895c6a2a437ad91bbbdfa` |
| Audit branch | `audit-phase13-shadow-validation` |
| Working tree at start | clean |

## 2. Phase 12 closure, and what it does and does not license

Phase 12 is **VALIDATED / FIELD VALIDATED / CLOSED** at code checkpoint
`1571f086385aa20d707e02200bd691dcd4b941c3`, field session
`session-8e64e5b6f1de456aa497a654842928ed`.

Nothing in this audit reopens a Phase 12 finding. Phase 12 proved the SHADOW
framework runs safely against live public data. It explicitly did **not** claim
simulator realism, signal quality, outcome calibration or live readiness —
which is precisely the gap Phase 13 would begin to address.

## 3. Proposed Phase 13 contract

**Phase 13 = Shadow Evidence Framework.**

Phase 13 should make the platform able to *record*, over a meaningful sample,
four things per decision, and keep them attributable and reproducible:

1. what the platform decided, and from what observed market state;
2. what `PaperExecutor` counterfactually simulated;
3. what the observable public market did afterwards, at declared horizons;
4. how internally consistent (1)–(3) were.

It remains an **observational research layer**. It is not a second strategy,
risk engine, execution engine, account or trading authority, and it is not a
route to authenticated venue access.

### Non-goals (explicit)

- No claim that a simulated fill would have filled at a venue.
- No profitability, edge or signal-quality verdict.
- No calibration of the fill model against venue truth (impossible here).
- No "strategy validated" boolean and no live-readiness score.
- No new venue, no new instrument, no USD/USDT collapse.
- No `TradingMode.LIVE`, no live executor, no promotion switch.

### Is this the right boundary?

Yes, with one correction that dominates everything else. The roadmap's next
item is shadow validation, and the seams for it already exist. But the audit
found that the current live universe **cannot produce a single cross-venue
opportunity** (§13), so "collect a meaningful live sample" is not achievable as
stated. The narrowest coherent Phase 13 is therefore:

> Build and prove the evidence framework on deterministic/synthetic
> opportunities first, where correctness is checkable; treat a genuinely
> overlapping public venue as a separate, explicitly-sequenced prerequisite
> before any live sample is claimed to mean anything.

That is option **A then B** from the task's own list, and the audit's findings
support that order rather than the reverse.

## 4. Architecture and data flow, as it actually runs

```
venue adapters (public WS/REST)
   -> BookSnapshot / BookDelta / TradePrint events
   -> TIDAL  (LocalOrderBook per venue+symbol, quality, staleness)
   -> MarketState.venues{} keyed venue+symbol
   -> CrossVenueDetector.find_dislocation(symbol, states, reference)
   -> OPPORTUNITY_DETECTED
   -> NORO / ZEPHR / TIDAL / LUMEN opinions -> consensus (Phase 8 registry)
   -> RUNE risk decision
   -> VESKA plan -> PaperExecutor -> PaperOrder -> FillEvent -> PaperAccount
   -> OKAPI hedge, MARIN reconciliation
   -> Recorder -> event store (PostgreSQL) -> replay

ShadowObserver subscribes alongside, health_relevant=False,
writing ShadowDecisionRecord / ShadowExecutionRecord into ShadowRegistry.
```

The shadow layer is `apps/shadow/observer.py` (529 lines),
`apps/shadow/registry.py` (714) and `core/models/shadow.py` (518).

## 5. Epistemic boundary — what shadow can and cannot validate

This is the single most important section, because Phase 13's value depends
entirely on not blurring these categories.

| Measurement | Classification |
|---|---|
| Opportunity creation / timestamp | OBSERVABLE FROM PUBLIC MARKET DATA |
| Reference price at decision time | OBSERVABLE |
| Market spread at decision time | OBSERVABLE |
| Post-decision price movement | OBSERVABLE |
| Markout at horizon | OBSERVABLE (given a declared horizon policy) |
| Adverse / favourable excursion | OBSERVABLE |
| Paper entry price | INTERNAL PAPER-SIMULATOR OUTPUT |
| Paper fill quantity / timing | INTERNAL PAPER-SIMULATOR OUTPUT |
| Simulated fees | INTERNAL (deterministic from `FeeSchedule`) |
| Simulated slippage | INTERNAL |
| Queue assumptions | INTERNAL (seeded random; see §8) |
| Partial-fill assumptions | INTERNAL (seeded random) |
| Hypothetical P&L | INTERNAL (PaperAccount) |
| Risk acceptance/rejection | INTERNAL (RUNE, authoritative for the platform) |
| Hedge behaviour | INTERNAL (OKAPI) |
| Reconciliation | INTERNAL (MARIN, against paper truth) |
| Internal consistency of cost model vs fill model | DERIVABLE FROM BOTH |
| Actual venue fill quantity | REQUIRES PRIVATE/AUTHENTICATED VENUE TRUTH |
| Actual venue fill price | REQUIRES PRIVATE VENUE TRUTH |
| Actual venue queue position | REQUIRES PRIVATE VENUE TRUTH |
| Actual venue rejection | REQUIRES PRIVATE VENUE TRUTH |
| Actual order latency | REQUIRES PRIVATE VENUE TRUTH |
| Actual private account state | REQUIRES PRIVATE VENUE TRUTH |
| Whether a hypothetical order *would have filled* | **CANNOT BE VALIDATED IN THIS BUILD** |

The last row is the load-bearing one. A public trade print at or through a
price is evidence that *someone* traded there; it is not evidence that *this*
hypothetical order, at its notional and its queue position, would have been
the one to trade. Phase 13 language must say **simulated fill**,
**counterfactual fill** and **observable markout**, never "the trade would
have filled".

## 6. Checkpoint and outcome seams — assessment

`core/models/shadow.py` defines four structures for exactly this purpose. Their
current state, traced by call graph rather than by name:

| Structure | Owner | Created by | Scheduled by | Persisted | Replayed | Bounded |
|---|---|---|---|---|---|---|
| `ShadowMarketCheckpoint` | ShadowRegistry | `ShadowObserver.capture_checkpoint` | **nothing** | no | no | **no** |
| `ShadowOutcomeCheckpoint` | ShadowRegistry | `ShadowObserver.capture_outcome` | **nothing** | no | no | **no** |
| `ShadowExecutionComparison` | — | **never constructed** | — | no | no | n/a |
| `ShadowVsVenueExecutionComparison` | — | **never constructed** | — | no | no | n/a |

Verified by grep across the repository: the only constructor call sites for the
first two are inside `observer.py` itself (lines 477, 514) plus one test; the
last two have **zero** construction sites anywhere, in production or test.

Identifiers and timestamps are sound where the structures exist:
`decision_id` links to `ShadowDecisionRecord.shadow_decision_id`, which carries
`opportunity_id`, `correlation_id`, `consensus_evaluation_id`, `intent_id`,
`risk_decision_id`, `execution_plan_ids`, `paper_order_ids`, `hedge_ids` and
`reconciliation_run_ids` — all described as "Existing platform identities.
Never re-minted." `created_at` is caller-supplied logical time.

Provenance is explicitly modelled: `MarketDataProvenance` on both checkpoints,
`ExecutionProvenance.PAPER_SIMULATOR` on the comparison, and
`venue_truth_available: bool = False` on the venue comparison. That is a
genuine strength — the models already refuse to let paper output be mistaken
for venue truth.

Phase 12 deliberately left the seams unscheduled, and said so. Phase 13 is
where the scheduling has to be designed, and that is the bulk of the work.

## 7. Execution-model assessment

`execution/paper/simulator.py` is substantially richer than a touch-fill toy.
Its own docstring lists what it models, and the code confirms: depth (walks the
book via `walk_book`), latency (`latency_adjusted_levels`), queue position
(`queue_ahead_fraction`), vanishing liquidity, partial fills
(`max_partial_fraction`), cancel races (`cancel_wins_race`), maker/taker fees,
and marketable vs passive paths. All randomness comes from a seeded generator,
so replay reproduces fills.

The Phase 13-relevant consequence: `maker_fill_probability`,
`queue_ahead_fraction`, `max_partial_fraction` and the vanishing-liquidity rate
are **model parameters that have never been calibrated against venue truth, and
cannot be in this build**. They are principled and conservative, but they are
assumptions. Any Phase 13 statistic about fill rates, partial-fill frequency or
realised slippage inherits their uncertainty, and must be reported as
conditional on them.

## 8. Attribution and causality assessment

The identity chain is in good shape. `Event` carries `id`, `type`, `ts_ms`,
`source`, `sequence`, `correlation_id`, `causation_id` and `schema_version`.
Market states carry both exchange and received timestamps, and
`MarketState.source_data_timestamp_for(legs)` deliberately takes the oldest
observation among *the venues actually behind those legs* rather than a global
timestamp — the Phase 9 P9-1 lesson, already applied.

Duplicate and out-of-order delivery are already handled in the shadow layer:
Phase 12's P12-H1 made fill observation resolve `client_order_id -> execution`
with a known fill id treated as a complete no-op, and P12-H3 made decision and
execution transitions monotonic so a terminal record cannot be resurrected by
stale delivery. Phase 12's tests cover both.

What Phase 13 adds is a **new** attribution risk that does not exist today:
outcome checkpoints are attributed to a `decision_id` at a *later* instant than
the decision. Nothing currently prevents a checkpoint captured after a resync,
a reconnect, or a stale-data window from being recorded as if it were clean
follow-through. That is finding P13-H3.

## 9. Replay and persistence assessment

`ShadowRegistry` holds everything in memory. `ShadowStore` is an abstract seam
with **no implementation anywhere in the repository**, so `store` is always
`None` and `_persist_decision` / `_persist_execution` are no-ops. There is no
shadow `EventType`, so no shadow evidence reaches the recorder or the event
store, and replay reproduces none of it.

The canonical event log, by contrast, is durable and replayable, and Phase 2
established deterministic replay. Most of what Phase 13 wants — market states,
opportunities, consensus, risk decisions, plans, orders, fills — is *already*
in it. Reconstructing evidence from the canonical log (option A) is therefore
strongly preferable to minting a second ledger (option C/D), and avoids the
source-of-truth duplication the project has repeatedly refused elsewhere.

The gap: outcome checkpoints at horizons are a *derived* artefact of the log
plus a horizon policy. They can be recomputed from the log offline, provided
the policy is recorded with the run. That is the cheapest correct design and it
is what the findings recommend.

## 10. Resource-bound assessment

| Structure | Status |
|---|---|
| `ShadowRegistry.decisions` | bounded only by `compact()`, which is opt-in |
| `ShadowRegistry.market_checkpoints[decision_id]` | **unbounded** — list append, never trimmed |
| `ShadowRegistry.outcome_checkpoints[decision_id]` | **unbounded** — list append, never trimmed |
| `ShadowRegistry` executions / `_by_order` / `_by_plan` | released only with their decision |
| Recorder pending buffer | bounded — `max_pending_events = 50_000`, fails closed |
| Recorder flush | bounded — size or `flush_interval_ms = 1_000` trigger |
| Event store (PostgreSQL) | intentionally persistent; grows with session |
| Book deltas / trade prints in the log | intentionally persistent |
| `LocalOrderBook` storage | bounded — `max_book_levels_per_side` (Phase 12 P12-F3) |
| Bus queues | bounded by existing backpressure |

Two compounding problems, together finding P13-H2. First, the checkpoint lists
have no cap at all. Second — and worse — `compact()` refuses to release any
decision that *has* checkpoints:

```
and not self.market_checkpoints.get(record.shadow_decision_id)
and not self.outcome_checkpoints.get(record.shadow_decision_id)
```

That rule is correct for Phase 12, where checkpoints are rare and precious. In
Phase 13, where *every* decision would carry checkpoints, it makes `compact()`
a permanent no-op and `ShadowRegistry` growth monotonic for the life of the
session. A multi-hour research run is exactly the case that breaks it.

## 11. Failure-semantics assessment

The existing layer is honest about failure: Phase 12's P12-H5 separated events
seen, intentionally ignored, unattributable, and genuine handler failures, and
surfaces them in `ShadowSnapshot` / `ShadowReadiness`. Observer failures are
isolated from trading and logged at WARNING.

Phase 13 introduces failure modes the current model has no vocabulary for: a
horizon that elapses after the process exits, a checkpoint missed because the
venue was disconnected, a symbol that went stale mid-horizon, a decision
compacted before its outcome resolved. `ShadowOutcomeCheckpoint` has no
"missing", "partial" or "contaminated" representation — every field is either a
value or `None`, and `None` is indistinguishable between "not measured" and
"measured as nothing". That is finding P13-H4.

The requirement is absolute and already matches the project's instincts
elsewhere: a missing outcome must never be silently rendered as zero, as a
loss, or as a win, and must never be fabricated.

## 12. Reporting assessment

`ShadowSnapshot` already exposes `market_checkpoints` and `outcome_checkpoints`
counters and a `ShadowReadiness` with reason codes, and Phase 12's P12-H4
ensured readiness cannot contradict itself. Truthfully derivable today, or
nearly so: opportunities observed, decisions attributable, simulated fills,
sample duration, per-symbol coverage, observer failure and unattributable
counts, reconnect incidence (from venue stats).

Not derivable without new capture: decisions with complete horizons, incomplete
outcomes, observable markouts, missing-data rate.

The prohibition stands and the existing code already respects it: no single
"validated" boolean, no live-readiness score. Phase 13 should expose facts and
let a human draw the conclusion.

## 13. Current live opportunity feasibility — **structurally impossible**

This is the finding that shapes the whole phase, and it is proven from code,
not inferred from the Phase 12 field output.

`strategies/cross_venue/detector.py::find_dislocation` filters to states whose
`s.symbol == symbol`, then:

```python
if len(usable) < 2:
    return None
...
if buy_state.venue == sell_state.venue:
    return None
```

`MarketState.states_for(symbol)` is `[s for s in self.venues.values() if
s.symbol == symbol]` — exact canonical symbol, no normalisation, no aliasing.

Evaluating the shipped live configuration directly:

```
BTC-USD    contributors=1  ['VENUE_B']
BTC-USDT   contributors=1  ['VENUE_A']
ETH-USD    contributors=1  ['VENUE_B']
ETH-USDT   contributors=1  ['VENUE_A']
symbols with >=2 venue contributors: 0
```

So **no canonical symbol has two venue contributors, and none can**. The
detector can never return a `Dislocation`, therefore no opportunity, no
consensus, no risk decision, no plan, no paper order, no paper fill — and
therefore no `ShadowDecisionRecord` with anything to measure.

This is not a defect. It is the correct post-TIDAL-C3 configuration: Binance
settles in USDT, Coinbase in USD, and the detector's own docstring says
"comparing `BTC-USDT` against `BTC-USD` prices the stablecoin basis as a
bitcoin edge". The honest consequence is simply that **a live strategy sample
cannot be collected with the current two-venue universe**, and no amount of
running the rehearsal longer will change that.

Frozen as **P13-B1**.

## 14. Venue-expansion readiness assessment

The repository's claim that adding a venue is "a config block and an entry in
`venues/registry.py`" is **substantially true** for production code. Grepping
`VENUE_A|VENUE_B` across `apps/ core/ agents/ execution/ strategies/ venues/
storage/ replay/ risk/` returns only:

- `core/config/settings.py` — the config blocks themselves (expected);
- `venues/venue_a/parser.py: VENUE = "VENUE_A"` and
  `venues/venue_b/parser.py: VENUE = "VENUE_B"`;
- `simulation/market.py` — synthetic venue specs.

No agent, no risk rule, no execution path and no strategy hardcodes a venue
name or a venue count. RUNE accounts per-venue generically. The detector takes
`list[VenueMarketState]` of arbitrary length and picks best-ask/best-bid, so a
third contributor is handled without change.

Real obstacles found, none of them blocking:

1. **Adapter modules are bound to a venue name.** `parser.VENUE` is a module
   constant, so `binance_public` cannot serve two differently-named venues.
   A third venue needs its own adapter package even when the protocol is
   identical. Finding P13-M3.
2. **Test-fixture coupling.** 91 test files reference `VENUE_A`/`VENUE_B`.
   These are fixtures, not production constraints, but they set the cost of
   changing the default universe.
3. **Two-leg economics.** RUNE and OKAPI reason in legs generically, but the
   cross-venue strategy is by construction a two-leg trade; a third venue adds
   *choice* of leg pair, not three-leg execution. No code change needed, but
   the selection policy (best pair vs all pairs) is undecided — research policy.

**This audit recommends venue expansion as a prerequisite for live sampling. It
does not implement, choose or name a venue.** The repository names no intended
third venue, so selecting one requires external research and a separate
authorisation, subject to: public and unauthenticated, genuinely identical
instrument, no credentials, no order path.

## 15. Safety boundary — re-verified

| Property | Verified state |
|---|---|
| `TradingMode` members | `PAPER` only — no `LIVE` member exists |
| Executor | `PaperExecutor` only |
| Venue adapter capabilities | `public_market_data=True`, `authenticated=False`, `order_submission=False`, enforced by `venues/registry.py::build_adapter` |
| Private venue connectivity | `NOT_IMPLEMENTED` |
| Live executor | `NOT_IMPLEMENTED` |
| Credential boundary | `NOT_IMPLEMENTED` |
| Live reconciliation | `NOT_IMPLEMENTED` |
| Deployment authorization | `NOT_IMPLEMENTED` |
| Promotion endpoint | none — `/api/shadow` and `/api/pre-live` are GET-only |
| Env var enabling live execution | none |
| Hidden authenticated venue client | none |
| Private order channel | none |

One clarification so the grep result is not misread: `agents/lumen/provider.py`
accepts an `api_key` for `ClaudeProvider`. That is a *model* provider for the
optional LUMEN intelligence agent, not an exchange credential, it reaches no
venue, and the default provider is `NullProvider`. It is not a venue-access
path and does not weaken this boundary.

**No CRITICAL safety violation found.** Phase 13 as scoped can be executed
entirely inside the existing PAPER boundary.

## 16. Frozen findings

| ID | Severity | Classification | Area | Finding | Evidence | Consequence | Remediation direction |
|---|---|---|---|---|---|---|---|
| P13-B1 | HIGH | BLOCKER / PREREQUISITE | Strategy universe | No canonical symbol has two venue contributors, so the cross-venue detector can never produce a live opportunity | `find_dislocation` requires `len(usable) >= 2` on exact `s.symbol`; live config yields 4 symbols × 1 contributor each | A live strategy sample is structurally unobtainable; a long rehearsal produces zero decisions to measure | Sequence a genuinely overlapping public-data venue as a prerequisite, or prove the framework on deterministic opportunities first. Never by collapsing USD/USDT |
| P13-H1 | HIGH | PHASE 13 FINDING | Capture scheduling | `capture_checkpoint` and `capture_outcome` have no caller anywhere | grep: only definitions plus one test; no scheduler, timer or tick hook | The evidence seams exist but produce nothing; there is no sample | Schedule capture through the existing observational path, driven by logical time, `health_relevant=False` |
| P13-H2 | HIGH | PHASE 13 FINDING | Retention | Checkpoint lists are unbounded, and `compact()` refuses to release any decision holding checkpoints | `market_checkpoints.setdefault(...).append(...)`; compaction guard excludes records with checkpoints | Under Phase 13 every decision has checkpoints, so compaction becomes a no-op and memory grows monotonically | Bound per-decision checkpoints; separate "unresolved evidence" from "resident forever"; allow release once horizons resolve and evidence is durable |
| P13-H3 | HIGH | PHASE 13 FINDING | Attribution | Outcome checkpoints carry no marker for reconnect, resync or stale-data contamination during the horizon | `ShadowOutcomeCheckpoint` has only `market_data` provenance; no per-horizon quality field | A markout measured across a disconnected or stale window is indistinguishable from a clean one, biasing results | Carry an explicit per-horizon data-quality/continuity verdict derived from existing venue and TIDAL state |
| P13-H4 | HIGH | PHASE 13 FINDING | Failure semantics | No representation for a missed, partial or unresolvable outcome; `None` conflates "not measured" with "measured as nothing" | Every outcome field is value-or-`None`; no status enum | A missing outcome can silently become zero, or be dropped from a sample, biasing any aggregate | Add an explicit outcome status (resolved / pending / missed / contaminated) and require aggregates to exclude and report non-resolved cases |
| P13-H5 | HIGH | PHASE 13 FINDING | Persistence / replay | Shadow evidence is resident-only: `ShadowStore` has no implementation, no shadow `EventType`, nothing reaches recorder or replay | grep: `ShadowStore` referenced only by its own module and `__init__`; no shadow event type | A meaningful sample is destroyed on process exit; a research run cannot be reproduced or re-analysed | Prefer reconstruction from the canonical event log plus a recorded horizon policy, over minting a second ledger |
| P13-M1 | MEDIUM | PHASE 13 FINDING | Comparison seams | `ShadowExecutionComparison` and `ShadowVsVenueExecutionComparison` are never constructed | zero construction sites repository-wide | The internal-consistency comparison Phase 13 needs does not exist yet; the venue comparison correctly cannot | Populate the internal comparison from intent/plan vs PaperExecutor; leave the venue comparison unpopulated and visibly so |
| P13-M2 | MEDIUM | RESEARCH POLICY REQUIRED | Horizons | No horizon policy exists; `horizon_ms` is recorded but nothing acts on it | `ShadowMarketCheckpoint` docstring states no defaults are baked in | Without a declared, recorded policy, results are not comparable across runs and the measure can bias the answer | Make horizons configuration recorded with the run; decide price measure separately (see §18) |
| P13-M3 | MEDIUM | PRE-EXISTING BASELINE | Venue expansion | Adapter packages bind a venue name via `parser.VENUE` | `venues/venue_a/parser.py:26`, `venues/venue_b/parser.py:19` | A third venue on an identical protocol still needs its own adapter package | Parameterise the venue name from config rather than a module constant |
| P13-M4 | MEDIUM | PHASE 13 FINDING | Model uncertainty | Fill-model parameters are uncalibrated and uncalibratable in this build | `maker_fill_probability`, `queue_ahead_fraction`, `max_partial_fraction`, vanishing-liquidity rate, all seeded random | Any fill-rate or slippage statistic inherits unquantified model uncertainty | Report such statistics as explicitly conditional on the model parameters, and record the parameters with the sample |
| P13-L1 | LOW | DOCUMENTATION STALENESS | README | README is not updated for Phase 12 closure or Phase 13 | out of scope for this audit by instruction | Minor reader confusion | Refresh in a later documentation task |
| P13-L2 | LOW | PHASE 13 FINDING | Terminology | Nothing mechanically prevents a future report from saying "would have filled" | models are careful; prose is unguarded | Epistemic overreach could enter a report | Adopt fixed vocabulary — simulated fill, counterfactual fill, observable markout — and guard it in tests as other phases do |

**Counts:** 0 CRITICAL · 5 HIGH (one of which is the blocker) · 4 MEDIUM ·
2 LOW · 1 BLOCKER/PREREQUISITE (P13-B1, also HIGH).

## 17. Blockers and prerequisites

**P13-B1** is the only blocker, and it gates the *field-validation* purpose of
Phase 13, not the framework work. Everything in §16 except P13-B1 can be built
and proven without it, using deterministic and synthetic opportunities where
the expected answer is known.

## 18. Research-policy questions — undecided, and not decidable from the code

1. Which horizons? (1s / 5s / 30s / 1m / 5m are illustrative only.)
2. Which price measure at a horizon — nearest observation, first observation
   after the horizon, mid, microprice, or TWAP? Each biases differently: mid
   flatters a spread-crossing strategy, microprice encodes queue imbalance,
   TWAP smooths the very dislocation being measured.
3. Do horizons belong in production runtime or in offline research tooling
   reading the event log?
4. What is a meaningful sample? Architecture acceptance (does capture work,
   is it attributable, does it survive restart, is it bounded) is answerable
   here. Statistical acceptance — how many opportunities, across which
   regimes, volatility and spread conditions, to say anything — is not, and
   must not be invented.
5. With three or more venues, is the trade the best pair, or all pairs?
6. How should winners/losers be counted when the fill itself is a model
   output rather than an observation?

Each is marked **RESEARCH POLICY REQUIRED** and must be decided explicitly
rather than defaulted into by an implementation.

## 19. Recommended remediation order

Derived from the findings, smallest safe sequence first:

- **A — Prerequisite sequencing.** Record P13-B1 as gating live sampling.
  Decide, as a separate authorised task, whether to add an overlapping
  public-data venue. Do not collect a "live sample" before this resolves.
- **B — Evidence capture.** P13-H1 scheduling, P13-H3 contamination marking,
  P13-H4 outcome status, P13-M1 internal comparison. Provable on deterministic
  opportunities.
- **C — Persistence and replay.** P13-H5, preferring reconstruction from the
  canonical log with a recorded horizon policy.
- **D — Bounded retention.** P13-H2, which must land before any long run.
- **E — Reporting.** Facts only; P13-L2 vocabulary guard.
- **F — Field validation.** Only after A, and only claiming what §5 permits.

B, C and D can proceed in parallel with A. F cannot.

## 20. Tests actually run

Read-only, no repository modification:

| Command | Result |
|---|---|
| `pytest -q tests/unit/test_phase12_shadow.py` | 17 passed |
| `pytest -q tests/replay tests/contract/test_replay_input_set.py` | 148 passed |
| `pytest -q tests/unit/test_venues.py tests/unit/test_live_venue_endpoints.py tests/audit/test_phase6_paper_boundary.py` | 104 passed |

No test failed, so nothing required classification. No test was created,
changed, skipped or removed.

## 21. Environment limitations

No Docker daemon, no PostgreSQL, no Redis, and no outbound reachability to any
exchange in the environment this audit ran in. Consistent with the task's
instruction, no live SHADOW session was started, no Docker service was started,
no database session was created or modified, and `reset-paper.sh` was not run.
The audit is therefore entirely static analysis plus local read-only tests,
which is sufficient for every finding above — each is proven from code, from
configuration evaluated in-process, or from existing passing tests.

## 22. Disposition

**PHASE 13 AUDIT — COMPLETE / FINDINGS FROZEN**

**PHASE 13 REMEDIATION — NOT STARTED**

No production, test, script, configuration, CI or Docker file was modified by
this audit. Phase 12 remains closed and unmodified. No venue was added, no
strategy behaviour changed, no authenticated or private exchange access
introduced, and USD and USDT remain distinct instruments.

---

# Roadmap-label correction

**The official roadmap reads:**

    PHASE 12 = Shadow Validation
    PHASE 13 = Venue Expansion
    PHASE 14 = Strategy Expansion

Section 3 of this document proposed relabelling Phase 13 as a "Shadow Evidence
Framework". That was a redefinition of the roadmap and is **withdrawn**. Phase
13 is **Venue Expansion**.

The findings themselves stand — they were derived from code and remain
accurate. P13-H1 … P13-H5, P13-M1 … P13-M4 and P13-L1 … P13-L2 are retained as
**follow-through findings** about the shadow evidence layer, to be scheduled
against whichever phase owns that work. They are not Phase 13 deliverables and
none of them is remediated here.

What the audit got right, and what it missed: it correctly proved from code
that the live configuration had no same-instrument overlap, and correctly
refused to manufacture one. It then reasoned that resolving this required a
*third venue*, and that was too narrow a search. A subsequent field probe found
the overlap available on a venue already configured.

# P13-B1 remediation — Batch A: live symbol overlap

## Audit finding

The production live configuration had no canonical symbol with two venue
contributors, so `find_dislocation` could never return a dislocation and no
live opportunity could exist. Proven from code, not inferred.

That finding was **correct for the configuration it inspected** and is not
rewritten here.

## Subsequent field discovery

A direct public protocol probe was run from the Dev Container at this
document's own checkpoint (`62ab561ab685c4e66cfc61543d708f352346796b`, clean
tree), against the **already-configured** endpoint
`wss://ws-feed.exchange.coinbase.com`, using the repository's own
`venues.venue_b.parser.subscribe_message()` to build the subscription and
`parse_message()` to read the replies, on the existing `level2_batch` /
`matches` / `heartbeat` channels.

Requesting `BTC-USDT` and `ETH-USDT`, over 60 seconds:

| | BTC-USDT | ETH-USDT |
|---|---|---|
| snapshots | 1 | 1 |
| initial snapshot size | 1,234 bids / 1,288 asks | 523 bids / 796 asks |
| `l2update` | 610 | 319 |
| trade events | 1 | 4 |

Totals across both products: 120 heartbeats, 929 `l2update`, 2 `last_match`,
3 `match`, 2 snapshots, 1 `subscriptions` acknowledgement. **Zero** exchange
application errors, **zero** Octopush parser failures, probe RC 0.

So Coinbase genuinely lists and serves both products, the existing endpoint,
channels, adapter and parser all accept them unchanged, and no quote
substitution is involved anywhere.

The blocker's remediation is therefore narrower than the audit assumed:

    same existing venue + genuine overlapping instruments

rather than adding a third venue.

## Remediation

`VENUE_B` live configuration now carries the markets Coinbase actually lists:

```
symbols = ["BTC-USD", "ETH-USD", "BTC-USDT", "ETH-USDT"]
```

`VENUE_A` is unchanged at `["BTC-USDT", "ETH-USDT"]`.

Resulting contributor map:

| Symbol | Contributors |
|---|---|
| BTC-USDT | VENUE_A, VENUE_B |
| ETH-USDT | VENUE_A, VENUE_B |
| BTC-USD | VENUE_B only |
| ETH-USD | VENUE_B only |

The derived strategy universe is unchanged in content and free of duplicates:
`["BTC-USD", "BTC-USDT", "ETH-USD", "ETH-USDT"]`.

**USD and USDT are not collapsed.** Nothing was renamed or aliased, the USD
markets remain as real single-contributor markets, `venues/base/symbols.py` is
untouched, and `denormalize` still renders every symbol as itself. The overlap
exists because two venues genuinely list the same instrument — the only honest
way to obtain one.

Nothing else changed: not the Coinbase endpoint, adapter or parser, not
`book_depth_levels`, not the 50,000-level storage ceiling, not the 8 MiB
transport bound, not the detector, not RUNE, VESKA, PaperExecutor or any
threshold.

## Structural consequence, proved

`tests/unit/test_phase13_venue_overlap.py` drives the real
`find_dislocation`. With two venue states on `BTC-USDT` it now returns a
dislocation across `VENUE_A`/`VENUE_B`; with the single `BTC-USD` state it
still returns `None`; and a `BTC-USD` query with USDT states present *still*
returns `None`, because the detector groups by exact canonical symbol. The
TIDAL-C3 guard is re-proved under the new configuration rather than assumed.

## Resource note

The observed USDT snapshots are far smaller than the USD ones — 1,234/1,288
and 523/796 against BTC-USD's 21,109/21,203. The storage ceiling is per side
and per book, so doubling the subscription count does not approach it. No
bound was raised, lowered or unbounded.

## Status

**P13-B1 — REMEDIATED IN CODE / FIELD VALIDATION REQUIRED**

Automated tests prove structural overlap. They cannot prove that the combined
live feed stays stable with all six venue/symbol subscriptions running
together. The environment this batch ran in has no Docker, no PostgreSQL, no
Redis and no exchange reachability, so no field rehearsal was performed and
none is simulated.

The field rehearsal must establish: six connected, FRESH books with no
reconnect storm and no sequence gaps; two usable contributors for each of
BTC-USDT and ETH-USDT and one for each USD market; TIDAL healthy; NORO no
longer reporting zero valuation-ready symbols solely for want of a second
contributor; ZEPHR recognising the overlapping instruments as two-venue; and
none of the Phase 12 failure signatures returning (HTTP 451, code 1009,
message-too-big, `max_levels_per_side` overflow, clean-close 1000 loop,
`BOOK_RESYNC_REQUESTED` storm, sequence-gap storm, parser failures, Coinbase
subscription errors). An actual dislocation is **not** required — structural
eligibility and stable data are the criteria.

**PHASE 13 — VENUE EXPANSION IN PROGRESS**
