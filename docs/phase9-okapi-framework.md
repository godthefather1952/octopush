# Phase 9 — OKAPI hedging framework

**Status: FRAMEWORK CONSTRUCTED. NOT VALIDATED.**

Phase 9 is a strict construction pass. It adds a way for the platform to say
what became of each request to close a residual — which gap was measured, which
hedge was proposed, which trade intent it became, which plan worked it, which
orders came out, and whether any of them is still unresolved.

It changes no economics. Not one tolerance, side, size, venue choice, urgency
or gate moved.

> **The line this phase must not cross.** ``Okapi.desired_delta`` is the
> authority for intended exposure. ``PortfolioState.net_delta_by_symbol()`` is
> the authority for actual exposure. ``DeltaReport`` is the authority for the
> difference. A registry that recomputed any of them would give the platform
> two answers to "how much are we unhedged?", which is the same as having none.

---

## 1. Current OKAPI behaviour — preserved exactly

Unchanged by this phase, in full:

| Concern | Where it lives, still |
| --- | --- |
| Intended exposure | ``Okapi.desired_delta``, a `dict[str, float]`; zero for every symbol, set at wiring |
| Setting a target | ``set_desired_delta`` |
| Reading a target | ``target(symbol)``, defaulting a missing symbol to `0.0` |
| Actual exposure | ``PortfolioState.net_delta_by_symbol()`` |
| Residual | ``actual - desired``, computed in ``delta_reports`` |
| Within tolerance | ``abs(unhedged) <= settings.hedge_tolerance_notional`` |
| Total | ``total_unhedged``, the sum of absolute residuals |
| Hedge side | long residual sells, short residual buys |
| Hedge size | ``abs(unhedged_delta)`` |
| Urgency | ``min(1.0, abs(residual) / max_unhedged_notional)`` |
| Venue | ``_hedge_venue``: lowest ask for a buy, highest bid for a sell, usable venues only |
| RUNE's gate | ``hedge_available(symbol, market)`` |
| Health | ``_heartbeat``: DEGRADED above ``max_unhedged_notional`` |
| In-flight suppression | ``Orchestrator.working_hedges`` and ``_hedge_in_flight`` |

The diff over `agents/okapi/agent.py` removes **zero lines**. Everything in
this phase is additive.

## 2. Target architecture

```
                STRATEGY
                   │  desired_delta (authority)
                   ▼
                 OKAPI
        delta_reports ──► residual ──► HedgeIntent
                   │                       │
                   │  (mirror, no branch)  │
                   ▼                       ▼
           HedgeRegistry              orchestrator
                   │                       │  TradeIntent(HEDGE)
                   │  ids only             ▼
                   ├──────────────────►  VESKA  ──► orders
                   │                                  │
                   └──────────────────────────────►  MARIN
                          link_reconciliation_run
```

Every arrow out of `HedgeRegistry` is an identifier. It owns no plan, no order
and no reconciliation truth.

## 3. Hedge targets

`HedgeTarget` records what `desired_delta` says, plus the metadata a dict
cannot carry: when it was set, by which `HedgeTargetSource`, why, and whether
it is still maintained.

`HedgeTargetSource` has four values and only `STRATEGY` is reachable. `RISK`,
`RECOVERY` and `OPERATOR` name futures the architecture should not have to be
reshaped to accept, and none has a caller.

`HedgeTargetRegistry` holds them. It is a mirror, and the asymmetry matters:

* `set_target` writes to the registry only. It does **not** call
  `set_desired_delta` — a mirror that wrote through could change what the
  platform hedges against, which is exactly what it must not be able to do.
* `get_target` returns `None` for an unrecorded symbol, where `Okapi.target`
  returns `0.0`. Those are different questions. "What exposure do we intend?"
  has zero as an honest answer; "what has been recorded?" does not.
* Symbols dropped from `desired_delta` are not deactivated by `mirror`.
  `desired_delta` has no concept of retiring a target, and inventing one here
  would report a retirement that never happened.

**Where the mirror is refreshed, and why there.** `mirror_targets(now_ms)` is
called from `delta_snapshot` and `okapi_snapshot`, both of which take a logical
instant from their caller. It is *not* called from `set_desired_delta`, which
has no `now_ms` to hand — giving it a clock read would put a wall-clock
timestamp into the hedging path.

## 4. Delta snapshots

`DeltaSnapshot` carries the `DeltaReport` objects `delta_reports` produced,
the target mirror, and `SymbolDeltaSummary` restatements of the same numbers.

`total_unhedged` on the snapshot is the sum of those reports' absolute
residuals — the same expression `Okapi.total_unhedged` evaluates, applied to
the reports already in hand. It is computed that way rather than by calling
`total_unhedged(portfolio)` because that method takes no `now_ms` and would
re-measure at a second instant; the snapshot and the health heartbeat must not
be able to report two different numbers for one moment.

`summarize_delta_report` is a pure adapter. `SymbolDeltaSummary.needs_hedge`
mirrors the condition `build_hedges` applies and is never consulted by it.

## 5. Residual cause

`ResidualCause` has ten values. **This build assigns exactly one:
`UNCLASSIFIED`.**

OKAPI measures a residual from portfolio delta alone, and a signed number
carries no history. A residual of +4,000 looks identical whether it came from a
partial entry fill, an exit that left a stub, a cancel race, or a mark that
moved. Inferring a cause from the residual's size or sign would produce a field
that reads like evidence and is not.

`HedgeRegistry.set_cause` exists so a later phase — one holding the fill and
order history to attribute honestly — has somewhere to put the answer. Nothing
calls it.

## 6. Hedge request lifecycle

`HedgeRequestStatus`: DETECTED, PROPOSED, SUBMITTING, WORKING,
PARTIALLY_FILLED, CANCEL_PENDING, COMPLETE, CANCELLED, UNKNOWN, FAILED.

This is the status of the *request*, not of the orders working it. The two can
legitimately disagree: a request whose single order is UNKNOWN is not finished,
and a request whose orders all filled is.

`HedgeRequestRecord` links the request to the `HedgeIntent` it copied, the
`TradeIntent` the orchestrator built, the plan ids, the order ids and any
reconciliation runs. Every economic field on it — side, venue, notional, both
deltas, urgency, reason codes — is copied off the intent. `residual_delta` is
derived as `current_delta - target_delta`, which is the definition
`delta_reports` used to produce the intent, not a second opinion about it.

## 7. ACTIVE vs OUTSTANDING

The platform already draws this distinction for orders (`is_live` vs
`is_outstanding`) and for plans (`is_active` vs `is_unresolved`). Hedging now
draws it too, for the same reason.

| | Question | UNKNOWN? |
| --- | --- | --- |
| `is_active` | *Is this working?* | **No** — nobody knows that it is |
| `is_outstanding` | *Could this still change what we hold?* | **Yes** |

They differ on exactly one status, and conflating them is how a platform
decides an order it cannot see stopped existing.

`HEDGE_TERMINAL_STATUSES` deliberately omits UNKNOWN, exactly as execution's
terminal sets do.

## 8. UNKNOWN hedges

An UNKNOWN hedge is not resolved, not terminal, and never auto-resolved. Only
an explicit caller, holding authoritative evidence, may move one out of
UNKNOWN — the same rule Phase 6 established for orders and Phase 7 for
discrepancies.

`derive_hedge_status` (in `agents/okapi/policy.py`) reads Phase 6's
`ExecutionPlanRecord` view and offers a reading, ordered most cautious first:

1. no plans → PROPOSED
2. **any** plan UNKNOWN → UNKNOWN, whatever the others say
3. any plan still active → CANCEL_PENDING, PARTIALLY_FILLED or WORKING
4. all terminal → COMPLETE if anything traded, CANCELLED if all were
   cancelled or expired, else FAILED

Unresolved beats resolved, in one direction only. **Nothing decides from this
helper** — no cancel, no resubmission, no risk action reads it, and no caller
applies it automatically. The treatment of genuinely mixed cases (one plan
filled, another cancelled) is VALIDATION DEFERRED; the ordering above is a
construction choice, not a proven rule.

## 9. Venue-candidate metadata

`HedgeVenueCandidate` records what each venue looked like: bid, ask, quality,
and whether it was usable under the same test `_hedge_venue` applies.

`HedgeRouteSnapshot.selected_venue` **copies** what `_hedge_venue` returned.
`Okapi.route_snapshot` runs no comparison of its own — a buy hedge still wants
the lowest ask and a sell hedge the highest bid, decided in exactly one place,
and a second implementation could quietly disagree with the first. `None` means
the existing selector found no usable venue, which is the same condition that
makes `build_hedges` skip the symbol.

## 10. HedgeIntent linkage

`Okapi.build_hedges` is unchanged through to its `return`. Immediately before
it returns — after every economic value is fixed —
`_mirror_hedge_intents(intents, now)` copies each intent into the registry. The
returned list is untouched and is still exactly what the orchestrator works.

Registration is idempotent by `HedgeIntent.hedge_id`.

## 11. VESKA plan and order linkage

In `Orchestrator._hedge`, after `veska.execute(plan, ...)` and after
`working_hedges` is updated, `_link_hedge` attaches:

* `TradeIntent.intent_id`
* `plan.plan_id`
* every `order.client_order_id` from the report
* status SUBMITTING — what just happened, the plan handed to VESKA

The lookup goes through `HedgeIntent.hedge_id`, which the orchestrator already
uses as the correlation id on the trade intent, the plan and every order. A
missing registry record is a silent no-op: a bookkeeping call that raised
because a mirror was absent would let the record break the thing it records.

**`working_hedges` and `_hedge_in_flight` remain the authority.** Nothing in
`_hedge` branches on the registry. Whether those can eventually migrate is a
question for validation, not construction.

## 12. MARIN linkage

`HedgeRegistry.link_reconciliation_run(hedge_id, run_id, now_ms)` is the seam
MARIN will need to resolve an UNKNOWN hedge: reconciliation establishes what
account and execution truth actually agree on, and this is where that finding
attaches.

**No MARIN behaviour changes in this phase.** Nothing calls it, MARIN's
comparison logic is untouched, and MARIN is not taught about hedging. The
public query surface exposes hedge ids, plan ids and order ids so a future
reconciliation pass has what it needs.

## 13. OkapiSnapshot and OkapiReadiness

`OkapiSnapshot` carries targets, the current delta reports, the tolerance, and
**ids** for active, outstanding and unknown hedges — not the records. A
snapshot embedding every hedge with its orders and fills would be sized by
session history rather than by what is currently outstanding.
`hedge_available_by_symbol` copies what `hedge_available` answered.

`OkapiReadiness` reports and gates nothing. `ready` is False whenever anything
is unestablished, including when no market has been seen — absence of evidence
is not readiness. Reason codes name the blocker:
`NO_TARGETS_ESTABLISHED`, `NO_MARKET_STATE`, `HEDGE_VENUE_UNAVAILABLE`,
`UNKNOWN_HEDGES:n`, `UNHEDGED_ABOVE_LIMIT`.

## 14. RUNE boundary — unchanged

RUNE reads `okapi.hedge_available(symbol, market)` as its mandatory pre-entry
gate, exactly as before. It does **not** read `OkapiReadiness`, `HedgeRegistry`,
`HedgeTargetRegistry` or any snapshot. Substituting a readiness model for that
gate would replace a tested decision with an untested one.

No risk gate, limit, threshold or sizing rule was touched.

## 15. Responsibility boundaries

| Component | Owns |
| --- | --- |
| **Strategy** | what exposure is intended |
| **OKAPI** | measuring the residual, requesting a hedge |
| **RUNE** | hard risk: gates, sizing, the kill switch |
| **VESKA** | execution |
| **MARIN** | establishing execution truth |

**Kill-switch FLATTEN is not OKAPI hedging.** Flatten is emergency risk
reduction under a fired kill switch; hedging is ordinary maintenance of a
residual against tolerance. They run at different times, for different reasons,
under different authority, and merging them would make an emergency path
reachable through routine bookkeeping.

## 16. Retention

`HedgeRegistry.compact` releases nothing by default. There is no arbitrary
count limit, because no measurement supports choosing one.

Even when asked to release, these are absolute: an ACTIVE hedge is never
released; an OUTSTANDING hedge is never released (which covers UNKNOWN, since
UNKNOWN is outstanding); a hedge linked to a reconciliation run is never
released, because the run may still be establishing what happened to it.

Lifetime counters survive compaction, so a compacted session still reports what
it did.

## 17. Future live hedging

**NO LIVE HEDGE EXECUTION IS IMPLEMENTED.** Hedges are worked by
`PaperExecutor` through VESKA, and `Veska.__init__` still refuses any executor
whose `is_paper` is False. This phase adds no venue connectivity, no
credentials, no authenticated endpoint and no live hedge path.

## 18. No derivatives

Deliberately absent, and documented as future work rather than stubbed:
perpetuals, futures, options, cross-asset beta hedging, funding rates, borrow,
margin, liquidation and collateral. Every one of them changes what a "hedge"
means and what it costs, and none can be framed honestly before the economics
are specified.

---

## VALIDATION DEFERRED

**Nothing in this phase has been validated.** No test was written, changed or
run; no linter, type checker, replay or platform tick was executed. Everything
below is unproven and must be treated as such until a dedicated audit says
otherwise.

### Measurement

* delta arithmetic
* desired-target semantics
* zero-target semantics
* mark dependence — a residual measured against a moving mark
* multi-venue aggregation
* tolerance boundaries, including exact equality
* source timestamp correctness

### The hedge itself

* hedge side
* hedge size
* urgency
* hedge availability
* venue selection
* quality handling
* missing books
* partial fills
* exit residuals
* cancel races
* UNKNOWN hedges
* duplicate hedge prevention
* hedge-in-flight semantics
* over-hedging
* under-hedging
* post-hedge residual
* repeated hedge requests

### Integration

* MARIN interaction
* RUNE integration
* startup recovery
* live venue truth

### Framework properties

* logical-time equivalence
* replay equivalence
* registry identity
* snapshot consistency
* retention
* resource bounds
* performance

### Out of scope, and unframed

* multi-strategy netting
* derivative hedges
* funding, borrow, margin

## Testing status

**No Phase 9 tests exist.** Treat every behaviour described here as constructed
and unproven.
