# Phase 12 — Shadow framework

**Status: VALIDATED / CLOSED at the Phase 12 code-and-contract boundary. FIELD SHADOW TEST PENDING.**

---

## 1. What shadow means

A shadow session runs the **entire platform** — market data, strategy, agents,
consensus, RUNE, VESKA planning, `PaperExecutor`, OKAPI, MARIN, LUMEN where
configured — against **real public market data**, and records what happened.

It answers one question: *what would Octopush have done in actual market
conditions?*

```
        public live feed
               │
               ▼
   TIDAL ▸ NORO ▸ ZEPHR ▸ LUMEN
               │
          ConsensusEngine
               │
              RUNE
               │
             VESKA
               │
        PaperExecutor  ──►  PaperAccount
               │                  │
             OKAPI              MARIN
               │
        ShadowObserver  ──►  ShadowRegistry     (observes; changes nothing)
```

## 2. What shadow does NOT mean

**Shadow is not a trading mode.** `TradingMode.PAPER` is the only mode this
build has, and a shadow session runs under it. Shadow is an
`OperationalProfile` — a different axis entirely.

**Shadow is not decision-only.** There is deliberately no "shadow executor"
that stops the lifecycle after planning. Stopping there would leave fills,
partial fills, hedging, exits, position management and MARIN untested against
live conditions — which is most of what a rehearsal is for.

**Shadow is not live.** No order reaches a venue. No authenticated endpoint is
contacted. No private channel is opened. The market data is public and
read-only; the execution is simulated.

## 3. Live public data + paper execution

```
SHADOW  =  public live market
         + the entire paper execution stack
         + extra observational records

NOT     =  public live market
         + no execution lifecycle
```

The feed is the existing `TF_FEED=live` read-only public adapters. **No new
venue adapter, no authenticated socket, no order channel** was added.

## 4. Trading mode remains PAPER

Under the SHADOW profile:

| | |
| --- | --- |
| `Platform.executor` | `PaperExecutor` |
| `Veska.executor` | `PaperExecutor` |
| `TradingMode` | `PAPER` |
| account | `PaperAccount` |

**No executor substitution happens anywhere.** `Veska.__init__` still refuses
any executor whose `is_paper` is False, and no such executor exists to refuse.

## 5. `ShadowDecisionRecord`

One record per rehearsed opportunity, in `core/models/shadow.py`.

**Identifiers, not copies.** Phase 8's registry owns the consensus evaluation,
Phase 6's owns the plan, the OMS owns the orders. This links them through the
identities those layers already mint — `opportunity_id`, `correlation_id`,
`intent_id`, `plan_id`, `client_order_id`, `hedge_id`, `run_id` — rather than
inventing parallel names. `shadow_decision_id` is the one new identifier, and
only because the registry needs its own key.

`ShadowDecisionStatus` records the rehearsal lifecycle: OBSERVED,
CONSENSUS_RECORDED, RISK_REJECTED, REJECTED, AUTHORIZED, PLANNED,
PAPER_WORKING, PAPER_COMPLETE, CLOSED, UNKNOWN, FAILED. `RISK_REJECTED` is
reserved for a RUNE rejection; `REJECTED` records non-risk terminal rejection. **It records the lifecycle; it does
not control it.** Every transition is driven by the platform's existing state
machine and copied down.

`UNKNOWN` carries the meaning it carries everywhere else here: some venue-side
truth is unresolved, it is not terminal, it is never assumed to mean failure,
and it is never auto-resolved. `SHADOW_TERMINAL_STATUSES` omits it, as every
other terminal set in this repository does.

## 6. `ShadowExecutionRecord`

What the paper stack did with one authorised plan: the orders, the fill ids,
planned and filled notional, fees, slippage.

**No separate fill simulation exists.** Every number is copied from what
`PaperExecutor` and `PaperAccount` produced. A second fill model could disagree
with the one whose P&L the account actually carries.

`execution_provenance` is `PAPER_SIMULATOR`, always — a *field*, not a comment,
so a future comparison tool cannot read these as venue fills by omission.

## 7. `ShadowRegistry`

`apps/shadow/registry.py`. Registration, status, the `link_*` family, execution
records, checkpoints, queries, `snapshot`, `compact`, plus a `ShadowStore` ABC
with no implementation.

**No economic authority.** It decides nothing, sizes nothing, routes nothing and
gates nothing. Every mutation takes `now_ms`; nothing reads a clock.

Retention: `compact` releases nothing by default. Even when asked, an active
decision, an UNKNOWN decision, one linked to a hedge or a reconciliation run,
and one carrying checkpoints are all never released — a rehearsal that discarded
the cases it could not account for would be rehearsing only the easy ones.

## 8. `ShadowObserver`

`apps/shadow/observer.py`. It subscribes to events the platform already
publishes and writes what it learns into the registry. That is the whole of it.

**It is given a bus and a registry, and nothing else.** No orchestrator
reference, no executor reference, no account reference — so there is no path
through it to anything that trades. It never calls RUNE, VESKA,
`PaperExecutor`, OKAPI or LUMEN; never submits or cancels an order; never
touches the account or a strategy state.

Events read: `OPPORTUNITY_DETECTED`, `CONSENSUS_UPDATED`, `TRADE_INTENT`,
`RISK_PASS`/`RISK_FAIL`, `EXECUTION_PLAN`, `EXECUTION_REPORT`,
`PAPER_ORDER_CREATED`/`UPDATED`, `PAPER_FILL`, `STRATEGY_STATE_CHANGED`, plus
`HEDGE_INTENT` and the reconciliation events for shape. **No competing
execution path is introduced.**

A handler that fails is logged and counted, never re-raised into the bus: the
observer's failure mode is a gap in the record, visible through shadow
snapshot/readiness counters. Intentionally ignored, unattributable, and actual
handler-failure events have separate counters.

### Activation

The observer records only under `OperationalProfile.SHADOW`, and under PAPER it
is not attached to the bus at all — so an ordinary paper session neither
subscribes nor accumulates a rehearsal history it will never read.

**That activation is observational, not economic.** The observer subscribes
with `health_relevant=False`, so adding it does not change the EventBus rolling
error-rate window consumed by RUNE's `MAX_ERROR_RATE` gate. Its own delivery and
failure counters remain observable. Deterministic PAPER-vs-SHADOW validation
proves the economic output remains equal for identical inputs.

## 9. Paper fill vs real fill

**PAPER FILLS ARE NOT REAL VENUE FILLS.**

An authorised `ExecutionPlan` in a shadow session represents **what the platform
would have submitted**, subject to a live executor that does not exist
translating it. A `PaperFill` represents **the simulator's estimate of what
might have filled** — `FillSimulator`'s output, with its own assumptions about
queue position, latency and slippage, against a book nobody actually traded
into.

The distinction is carried in the models, not left to prose:

* `ExecutionProvenance.PAPER_SIMULATOR` on every execution record and order
  summary. `VENUE_REPORTED` exists and **nothing produces it**.
* `MarketDataProvenance` on every decision, execution, checkpoint and snapshot.
* `GET /api/shadow` states it on every response.

## 10. Market checkpoint seam

`ShadowMarketCheckpoint` copies observable market state at a caller-supplied
instant, via `ShadowObserver.capture_checkpoint(decision_id, market, now_ms,
horizon_ms=...)`.

**Nothing schedules these.** No timer, no automation, no horizon policy.

**No default horizons are baked in.** One second, five, thirty, a minute are all
common, and picking one here would smuggle a research decision into a
construction phase. `horizon_ms` records what a caller was measuring toward, and
nothing acts on it. Later validation chooses what is useful.

## 11. Outcome checkpoint seam

`ShadowOutcomeCheckpoint` records where the market went and what the paper
position was worth. `gross_move_bps` is arithmetic on two prices the caller
supplied.

**Nothing says whether an outcome was good.** No pass, no fail, no
classification, no score. A price moved and a simulated position had a value;
what that means about the decision is research, and research needs a hypothesis
this phase has not got.

P&L comes from the platform's single `PaperAccount` — the one source of
simulated P&L truth.

## 12. `ShadowExecutionComparison`

Compares what the platform *expected* (from the intent and the plan) against
what the *simulator produced*. Both sides are the platform's own, so what it can
honestly show is internal consistency — whether the cost model and the fill
simulator agree with each other.

It is **not** a comparison against real venue fills, because no private venue
truth exists and inventing one side would make the whole thing meaningless.

## 13. `ShadowReadiness`

**Observational. It blocks nothing** — not startup, not a tick, not execution.

A SHADOW profile on the simulated feed still runs; it is simply not the
rehearsal the operator probably wanted, and readiness says so
(`FEED_NOT_PUBLIC_LIVE`) rather than refusing at configuration load, where it
would be a policy this phase may not set.

`private_execution_absent` is derived from the actual executor and venue
capabilities. Readiness also checks usable market state, recorder health,
coordination, RUNE, VESKA, MARIN, OKAPI and observer gaps. Mandatory false
conditions contribute reason codes. Readiness remains reporting-only.

## 14. `ShadowSnapshot`

Counts and **ids**, never records: lifetime decisions total, resident decisions,
authorized, rejected, plans, orders, fills, active and unknown decision ids,
checkpoint counts, observer-gap counters, and equity/P&L from the single
`PaperAccount`.

Under PAPER it reports `enabled: false` with empty counts.

**There is no second shadow ledger.** Two would eventually disagree and nobody
would know which to believe.

## 15. RUNE in shadow — unchanged

**RUNE remains the hard gate**, and is not bypassed because the money is
simulated. That is the point: shadow rehearses what the *live* decision process
would permit, and a rehearsal that skipped risk would be rehearsing a platform
nobody intends to run.

No gate, limit, threshold or sizing rule was touched.

## 16. VESKA in shadow — unchanged

VESKA creates the same paper plans and works them through `PaperExecutor`.
There is no "would submit" shortcut: the plan is already the normalised
representation of what the platform wanted to work, and the registry merely
records it.

## 17. OKAPI in shadow — unchanged

OKAPI keeps managing hypothetical residuals created by `PaperExecutor`, and
this is valuable: a shadow session rehearses partial-fill residual → hedge
request → paper hedge → post-hedge state against live market data. No hedging
economics changed.

## 18. MARIN in shadow — unchanged

MARIN keeps reconciling the paper OMS against the paper account. It does **not**
reconcile against real venue orders, because none exist. The registry may link
run ids for observability; **no new comparison was introduced**, and MARIN is
not taught about shadow.

## 19. LUMEN in shadow — unchanged

LUMEN runs normally when configured and remains optional. Its provider is
analytical only and holds no trading credentials. **No LUMEN semantics differ
between PAPER and SHADOW.**

## 20. `start-shadow.sh`

`scripts/start-shadow.sh`, with a root wrapper, following the repository's
existing tooling style. It sets exactly two variables:

```
TF_PROFILE=shadow
TF_FEED=live
```

then `exec`s `scripts/start-paper.sh`. Dependency checks, paper-mode
enforcement, compose, health polling and reporting are start-paper's, unchanged
— duplicating them would create a second launcher to keep in step with the
first.

It prints, before anything starts:

```
PUBLIC LIVE MARKET DATA   read-only exchange feeds
PAPER EXECUTION ONLY      PaperExecutor, simulated fills
NO REAL ORDERS            nothing reaches a venue
```

**It does not set `TF_MODE`, and it cannot.** `assert_paper_mode()` runs before
the machine is touched and refuses any other value from either the shell
environment or `.env`. There is no flag, argument or variable in this script
that changes that.

`status.sh` reports SHADOW as **`REAL PUBLIC DATA / SIMULATED EXECUTION`** —
never as LIVE TRADING. That mislabel is the one in this tooling that could
actually cost somebody money.

## 21. Session provenance

Every shadow record makes its sources explicit: market data from a public feed,
execution from `PaperExecutor`. **No record may label a paper fill as a real
fill**, and the provenance enums exist so that it cannot happen by omission.

`SessionManifest` states it too — `trading_mode: PAPER`,
`operational_profile: SHADOW`, `feed: live`, `paper_executor: True`,
`private_venue_access: False`, `real_order_submission: False`.

## 22. Future venue comparison

`ShadowVsVenueExecutionComparison` is a shape for a comparison that **cannot yet
be made**. Nothing populates one, and nothing can: it needs a venue's report of
what actually filled, which requires an authenticated executor this build does
not have.

Its venue-side fields are all `None` and `venue_truth_available` is `False`. It
is written down so that when a live executor eventually exists, "how close was
the simulator?" has a defined shape rather than being invented under pressure —
and so the absence of the venue side is visible rather than assumed.

## 23. No promotion-to-live switch

**NO SHADOW-TO-LIVE RUNTIME SWITCH EXISTS.**

There is no flag, endpoint, environment variable, config field or code path that
turns a shadow session into a live one. It must not be possible for one session
to flip something and begin submitting real orders, and it is not — because
there is no live executor to submit them with.

A future live session must be **constructed separately**: a different executor,
a different composition root, a separately controlled project with its own
authorisation. Not a flag on this one.

## 24. Future live-readiness bridge

`PreLiveReadinessSnapshot` (Phase 11) records the gap honestly:

```
paper_framework ............... NOT_VALIDATED
shadow_framework .............. NOT_VALIDATED
private_venue_connectivity .... NOT_IMPLEMENTED
live_executor ................. NOT_IMPLEMENTED
credential_boundary ........... NOT_IMPLEMENTED
live_reconciliation ........... NOT_IMPLEMENTED
deployment_authorization ...... NOT_IMPLEMENTED
```

`GET /api/pre-live` returns it. Nothing reads it to permit anything.

---

## VALIDATION STATUS

Phase 12 was audited at `1acd9c58b5885c4be44be1529a5831d8e821815c` and remediated on `remediate-phase12-shadow`.

The last production-code-changing commit is `42baa7ffa02036fa38de762c2f2dad10fbfabdce`. The validated code-and-test checkpoint is `60190fca1ebce95a79fac7ad6f06f49e7662dbdf`.

Eight frozen findings were remediated: P12-C1, P12-H1 through P12-H5, and P12-M1 through P12-M2.

Permanent validation lives in `tests/unit/test_phase12_shadow.py`: 16 test functions / 17 pytest cases. Coverage includes risk-health neutrality at and above the RUNE threshold, duplicate/delayed fill ownership, lifecycle monotonicity, generic-vs-risk rejection truth, APPROVED_REDUCED semantics, detached registry reads, compaction counters, observer failure visibility, same-time snapshot/API purity, the PAPER-only boundary, and deterministic PAPER-vs-SHADOW economic equivalence.

### Automated evidence

At `60190fca1ebce95a79fac7ad6f06f49e7662dbdf`:

* Python 3.11 unit + contract: **1399 passed, 126 skipped, 1 known baseline packaging failure**. All 17 Phase 12 pytest cases pass.
* PAPER boundary job: **PASS**.
* mypy on `core/`: **PASS**.
* Ruff: exactly the three pre-existing findings (`agents/marin/agent.py` I001, `agents/marin/source.py` I001, `agents/okapi/registry.py:360` SIM102); no Phase 12 lint regression.
* Python 3.12 backend precheck: **PASS**. The subsequent full-suite step entered the repository's already-established long-running condition; Phase 12 does not claim that baseline issue is fixed.

The deterministic equivalence test drives the same seeded market and logical clock under PAPER and SHADOW, exercises RUNE, and compares opportunity state, risk decisions, OMS orders, fills, portfolio state, OKAPI hedge records, MARIN result and kill-switch state. Economic summaries are equal; only observational shadow data differs.

### Remaining field validation

Code-and-contract closure does **not** establish simulator realism against real venue fills or live-deployment readiness. A field SHADOW rehearsal against real public market data remains pending, including reconnect/staleness behavior, real-market timestamp quality, long-session retention/resource behavior, and shutdown/session-finalization under an actual SHADOW run.

No authenticated venue executor, private order channel, credential boundary, or shadow-to-live promotion switch was added. `PreLiveReadinessSnapshot` remains conservative.

## Testing status

**PHASE 12 REMEDIATED / AUTOMATED VALIDATION PASS / FIELD SHADOW TEST PENDING.**
