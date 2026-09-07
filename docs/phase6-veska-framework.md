# Phase 6 — VESKA execution framework

Construction of the execution framework: the structure, interfaces and wiring
that later passes will validate, remediate and eventually extend to live
venues.

- **Base SHA:** `1fb9a1bbd4eec558d8f59550cc1b411ae524cadf` (`phase5-rune-remediation-e2`)
- **Branch:** `phase6-veska-framework`
- **Mode:** **STRICT CONSTRUCTION ONLY.** Not an audit, not a validation pass,
  not a remediation pass.
- **Validation status:** **VALIDATION DEFERRED — FRAMEWORK CONSTRUCTION ONLY.**

---

## 1. The validated baseline

Phase 5 / RUNE is code complete, test validated and frozen. External CI on the
base SHA: 3087 passed / 2 skipped / 0 failed (Python 3.12); 1382 passed / 126
skipped / 0 failed (Python 3.11); backend contracts 747 passed / 1 tooling-only
skip; Ruff PASS; Mypy PASS; paper boundary 61 passed.

Nothing under `agents/rune/**`, `risk/limits/**` or `risk/kill_switch/**` is in
this diff. `CommittedExposure`, `RiskContext`, risk sizing, the recovery
reserve, the rolling error rate, kill-switch behaviour and every risk threshold
are untouched.

## 2. Construction-only philosophy

This phase frames the house. It creates places for things to live and states
what each place means; it does not prove that what already lives there behaves
correctly.

Three rules follow from that, and every decision below obeys them:

**No behaviour changed.** Not one fill, size, price, route, threshold or
lifecycle transition moves. Everything added is additive: new models with
defaults, new interfaces, new query methods, new state that nothing reads to
make a decision. If this build traded differently from the base SHA, the
construction would have failed at its own terms.

**No speculative safety rules.** It would have been easy to make the preflight
check enforce deadlines, or to make the TIF policy layer reshape the fill loop.
Both were declined. A safety boundary nobody has measured is a boundary nobody
can trust, and adding one now would answer a validation question by assertion
before the measurement exists.

**Nothing unresolved is ever discarded.** The retention hooks exist; the
deletion policy does not. An UNKNOWN order and its bookkeeping are never
eligible for collection, in any code path added here.

## 3. Execution architecture

```
RUNE  ──authorised intent + risk decision──►  VESKA
                                               │
        ┌──────────────────────────────────────┼──────────────────────────┐
        │                                      │                          │
   Plan Builder                        ExecutionRegistry             VenueRouter
   (build_plan)                        (plan lifecycle)              (routing)
        │                                      │
        └──────────────► Executor (interface) ◄┘
                              │
                              ├── PaperExecutor      ← the only implementation
                              │      └── OrderManager (order book of record)
                              │      └── FillSimulator
                              │
                              └── (a future live executor)
                                       └── ExecutionVenueGateway (interface only)
                                                └── (a future venue adapter)
```

Supporting modules, all pure:

| Module | Purpose |
| --- | --- |
| `execution/veska/policy.py` | What each order type and time-in-force *means* |
| `execution/veska/preflight.py` | Where construction-time plan checks go |
| `execution/veska/registry.py` | Plan records and plan-status derivation |
| `execution/gateway.py` | The future live-venue seam. Interface only |

## 4. Plan lifecycle

`ExecutionPlanStatus` is new, and is deliberately **not** a replacement for
`OrderStatus`. They answer different questions:

| | Unit | States |
| --- | --- | --- |
| `OrderStatus` | one order at a venue | CREATED, SUBMITTING, ACKNOWLEDGED, OPEN, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, EXPIRED, UNKNOWN |
| `ExecutionPlanStatus` | the plan those orders work | CREATED, SUBMITTING, WORKING, PARTIALLY_FILLED, CANCEL_PENDING, COMPLETE, CANCELLED, EXPIRED, UNKNOWN, FAILED |

A two-leg plan whose first leg has filled and whose second is still resting is
not describable by any single order's status. That gap is why the second
lifecycle exists.

`PLAN_TERMINAL_STATUSES` omits `UNKNOWN`, exactly as `TERMINAL_STATUSES` does
for orders. A plan holding unresolved truth has not finished.

### Status derivation

`derive_plan_status(record, orders)` is pure and lives in one place. Its
precedence:

1. **No orders yet** → keep the record's own status. A plan asked to submit
   whose orders have not appeared is not "complete".
2. **Anything UNKNOWN** → the plan is UNKNOWN. Unresolved truth dominates
   every other reading — the same fail-closed rule the order model applies.
3. **Anything still working** → CANCEL_PENDING if a cancel is in flight,
   PARTIALLY_FILLED if something traded, else WORKING.
4. **All terminal** → COMPLETE if anything filled; else EXPIRED, FAILED or
   CANCELLED according to what the orders did.

**Deferred:** whether COMPLETE is the right word for "filled 0.02 of 1.0 and
cancelled the rest", and how mixed terminal states should resolve. Stated as a
starting point, not as a proven rule.

## 5. Order lifecycle

Unchanged. `OrderStatus`, `ORDER_TRANSITIONS`, `TERMINAL_STATUSES`,
`PaperOrder.transition` and `PaperOrder.apply_fill` are exactly as Phase 5 left
them. `OrderManager` gained query methods and gained no new behaviour.

## 6. LIVE versus OUTSTANDING

The distinction this framework most needed a word for.

| Question | Predicate | UNKNOWN counts? |
| --- | --- | --- |
| Is this order known to be working? | `PaperOrder.is_live` | **No** |
| Could this order still turn out to have traded? | `PaperOrder.is_outstanding` | **Yes** |

`is_live` is `not is_terminal and status is not UNKNOWN` and is *correct for its
own question*: nobody knows whether an UNKNOWN order is working. The problem it
creates is that one predicate then answers two questions — "this order is
finished" and "nobody knows what this order did" — and a caller reading it as
the first has silently accepted the second.

`is_outstanding` is `not is_terminal`. The two differ only for UNKNOWN, and
that difference is the entire point. A caller deciding whether to *wait* wants
`is_live`; a caller deciding whether it is safe to *act as though this order is
finished* wants `is_outstanding`.

**No existing call site was migrated.** Which readers must move is a validation
question with real consequences, and answering it by search-and-replace during
a construction pass would be exactly the unmeasured change this phase avoids.
The vocabulary now exists so that the question can be asked precisely.

Query surfaces reflect the split throughout: `OrderManager.live_orders()` /
`outstanding_orders()`, `Executor.open_orders()` / `outstanding_orders()`,
`ExecutionSnapshot.open_order_ids` / `outstanding_order_ids`.

## 7. UNKNOWN

Treatment is unchanged and remains fail-closed:

- `UNKNOWN` is not terminal, in either lifecycle.
- The executor never advances, expires or cancels an UNKNOWN order.
- `OrderManager.compact` archives only terminal orders, so an UNKNOWN order is
  never eligible.
- `ExecutionRegistry.compact` likewise refuses anything not terminal, and an
  unresolved plan is never terminal.
- `PaperExecutor.compact_terminal_state` deliberately leaves `_pending` alone:
  an UNKNOWN order's arrival and cancel schedule are still needed if it ever
  resolves.

Nothing added here resolves an UNKNOWN order automatically. The only route out
is §11's explicit seam.

## 8. ExecutionRegistry

`execution/veska/registry.py`. One `ExecutionPlanRecord` per plan, owned by
VESKA.

**Responsibilities:** `register_plan`, `attach_order`, `attach_orders`,
`set_status`, `refresh`, `note`, `get`, `plan`, `all_records`, `active_plans`,
`unresolved_plans`, `terminal_plans`, `plans_for_intent`,
`plans_for_correlation`, `plan_for_order`, `compact`.

**Non-responsibilities:** it does not submit, cancel, size or fill anything, and
no execution decision reads it. It is state and observability infrastructure.

**No clock.** Every method that changes a timestamp takes `now_ms` explicitly,
for the reason the executor does: a record stamped from a clock read is a
record replay cannot reconstruct (P2-14).

**Registration is idempotent** by `plan_id`. Re-registering returns the record
already held rather than replacing it — a plan id names one execution attempt,
and starting a second under the same name is how a record loses the orders the
first one created.

`Veska.plans` is retained as a **read-only property view** over the registry so
every existing caller keeps working. New code should use `get_plan`,
`active_plans` and `unresolved_plans`, which answer questions about a plan's
*state* that the raw dictionary never could. Migration is deferred; nothing is
removed.

## 9. Executor query contract

`Executor` now has two explicit halves.

**Commands** — semantics unchanged by this pass: `submit`, `cancel`,
`cancel_all`, `poll`, and the new `resolve_unknown`.

**Queries** — new, and all delegating: `open_orders`, `outstanding_orders`,
`unknown_orders`, `all_orders`, `get_order`, `orders_for_plan`,
`execution_snapshot`.

**Identity** — `name`, `version`, `capabilities`. `PaperExecutor` is
`"paper"` / `"paper-executor-0.2"`.

**Retention** — `compact_terminal_state(unsealed_fills=...)`.

`PaperExecutor` implements every one of them by delegating to `OrderManager`.
No execution state is duplicated: the OMS remains the single book of record,
and a second copy that could fall out of step with it is precisely what this
contract exists to prevent.

The split matters because everything that will later want to *understand*
execution — reconciliation, an operator surface, a plan-level control — needs
the query half and must never reach into an executor's private state to get it.

## 10. ExecutionSnapshot

`ExecutionSnapshot` is the canonical view of what execution believes at a
logical instant, and the surface Phase 7 will consume.

Contents: compacted `OrderSummary` rows, `open_order_ids`,
`outstanding_order_ids`, `unknown_order_ids`, `active_plan_ids`,
`unresolved_plan_ids`, `counts_by_status`, `ExecutionMetrics`, and the OMS's
lifetime totals (`fills_applied`, `orders_created`, `duplicate_fills`,
`illegal_transitions`, `archived_orders`).

`OrderSummary` exists rather than embedding whole `PaperOrder` objects because
a snapshot carrying every fill and every history entry would be sized by
trading history rather than by open state.

**Timing.** `created_at` is supplied by the caller and never read from a clock,
so replay can ask "what did execution believe at logical time T?" and get a
deterministic answer.

**It is not reconciliation.** Nothing here compares execution's view against
anything else. `veska.execution_snapshot(now_ms)` composes the executor's
order-level view with VESKA's plan-level view so a reader never has to combine
the two by hand.

## 11. UNKNOWN-resolution seam

```python
await executor.resolve_unknown(client_order_id, authoritative_status, now_ms)
```

Returns an `ExecutionCommandResult`. Refuses an order that is not UNKNOWN.
Never invents a status.

The caller supplies `authoritative_status` because the caller is the one
holding evidence. This executor has none — the paper venue *is* this object,
and it learns nothing by being asked twice.

**Nothing in this build calls it.** It exists so that when reconciliation does
have a venue's answer, it applies it through a defined door rather than
reaching into the OMS. `Veska.resolve_unknown` forwards it and refreshes the
affected plan, adding no judgement of its own.

## 12. ExecutorCapabilities

What an executor *claims* to implement — framework metadata, not proof.

`PAPER_CAPABILITIES` describes the current implementation rather than an
aspiration. `supports_market` is **False** because the router never emits a
MARKET order and that fill path has never been exercised. `supports_fok` is
**False** because nothing in the executor distinguishes fill-or-kill from any
other marketable instruction. Declaring either True would be a claim this build
has not earned.

`supports_ioc` and `supports_post_only` are True: the router emits both and the
executor accepts both. Whether it *honours* their semantics is a validation
question this pass does not answer — a capability is a statement of intent.

## 13. Time-in-force policy layer

`execution/veska/policy.py` — pure, stateless, clock-free. One canonical
location for what IOC, FOK, POST_ONLY and GTC mean:

`is_immediate`, `can_rest`, `requires_full_fill`, `must_not_take`,
`expects_maker_fee`, `crosses_by_construction`, `describe`.

**Deliberately not wired into the fill loop.** Whether `PaperExecutor`'s
behaviour matches these definitions is exactly what a later pass exists to
determine; changing the loop now would answer that question by assertion and
move the goalposts before the measurement. What this module establishes is that
there is now a single place to change if the answer turns out to be no.

## 14. ExecutionRole

`ExecutionRole` — ENTRY, EXIT, HEDGE, FLATTEN — states why an order exists in
risk terms, with `is_risk_reducing` as the coarse test.

Until now that distinction was *inferred* from whether `OpportunityLeg.quantity`
happened to be set: an exit and a hedge carry an explicit quantity because
closing a position means closing that quantity, and an entry does not. The role
says it instead of leaving it to be deduced.

Carried on `TradeIntent.execution_role` (default ENTRY) and copied onto
`ExecutionPlan.execution_role` and `ExecutionPlanRecord.execution_role`.

**Wired at three construction sites:**

| Site | Role |
| --- | --- |
| `_build_intent` (ordinary strategy entry) | `ENTRY` |
| `_submit_exit` | `EXIT` |
| `_hedge` (OKAPI hedge) | `HEDGE` |

`is_exit` is **kept and unchanged**, and remains authoritative for every
existing reader. The role says the same thing with more resolution — an exit
and a hedge are both "not an entry" and are not the same activity. Nothing was
migrated off `is_exit`.

**FLATTEN is defined but not yet wired.** `_flatten` reaches the same
`_submit_exit` path under an engaged kill switch, where FLATTEN would be the
more precise word. Distinguishing the two means threading a flag through
`_submit_exit`, which is a behavioural change this pass does not make. Recorded
as a deferred extension rather than done invasively.

**Nothing reads the role to make a decision.** Sizing, routing and fill
behaviour are all exactly as before.

## 15. Plan metadata

`ExecutionPlan.notional` **keeps its canonical meaning**: the per-leg notional
RUNE authorised, so a plan's gross consumption is `notional × len(orders)`
(P5-2). Reinterpreting it would break every existing reader, and this pass
breaks none.

Added beside it, all with defaults:

- `requested_notional` — what the orchestrator asked for, before risk sizing.
- `approved_notional` — what RUNE authorised. Equal to `notional`; carried
  separately so the pair reads unambiguously.
- `execution_role` — copied from the intent.
- `gross_notional` — a property, `notional × len(orders)`, so the P5-2 unit has
  a name instead of being recomputed at each call site.

## 16. Plan preflight seam

`preflight_plan(plan, capabilities=...) → PlanPreflight(ok, reason_codes,
details)`. Pure, and returned rather than raised: a structurally impossible
plan is a fact about the plan, and the check has no business ending a tick.

Checks only what is **unquestionably** required for a plan to be workable at
all: at least one order; a `client_order_id`, venue and symbol on each; positive
quantity and expected price; no duplicate id within one plan; and, when
capabilities are supplied, an order type and time-in-force the executor claims
to support.

**Deliberately absent:** deadline enforcement, per-role size rules, notional
conservation, venue reachability. Every one of those requires a judgement, and
this is where they will go once someone has evidence for them.

`Veska.preflight(plan)` offers it. **`execute` does not consult it** — turning a
new check into a submission gate is a behavioural change.

## 17. VESKA wiring

`build_plan` registers the plan. `execute` marks it SUBMITTING, submits,
attaches the returned order ids and refreshes the derived status. `poll`
refreshes the plans whose orders filled. `cancel` refreshes the affected plan;
`cancel_all` refreshes every active and unresolved one.

New plan-level control and query surface:

`cancel_plan(plan_id, now_ms)`, `resolve_unknown(...)`, `get_plan`,
`active_plans`, `unresolved_plans`, `plans_for_intent`,
`plans_for_correlation`, `refresh_plan`, `refresh_all_plans`,
`outstanding_orders`, `unknown_orders`, `capabilities`, `metrics`,
`execution_snapshot(now_ms)`.

`cancel_plan` selects by `is_outstanding` rather than `is_live`: leaving an
order out of a cancellation *because nobody knows what it is doing* would be
the wrong way round. It cancels what one plan owns and nothing else, which is
what distinguishes it from `cancel_all` — the kill switch's blunt instrument,
unchanged.

**All of this is observability and lifecycle state.** No fill decision reads
any of it.

## 18. Execution metrics

`ExecutionMetrics` — plain counters, no backend dependency, no thresholds, no
alerting, no rates: `plans_created`, `plans_submitted`, `plans_completed`,
`plans_cancelled`, `plans_failed`, `orders_created`, `orders_outstanding`,
`orders_unknown`, `fills`, `partial_fills`, `duplicate_fills`,
`illegal_transitions`, `cancel_requests`, `rejected_submissions`.

Exposed by `Veska.metrics()` and carried inside `ExecutionSnapshot`. Totals
only — what a healthy number looks like belongs to whoever later has evidence.

## 19. Resource lifecycle hooks

Three hooks exist; none has an aggressive policy.

- `ExecutionRegistry.compact(keep_terminal=True)` — at its default, releases
  nothing. Only terminal records are ever eligible, and an unresolved plan
  never is.
- `PaperExecutor.compact_terminal_state(unsealed_fills=...)` — delegates the
  order-side decision to `OrderManager.compact`, which already refuses anything
  not terminal or whose fills are still awaiting reconciliation. `_pending` is
  deliberately untouched.
- `OrderManager.compact` — unchanged.

The framework provides the hook and the safety rule. The retention policy is a
later pass's work, once someone has measured what retention actually costs.

## 20. Future MARIN consumption

Phase 7 will be able to ask:

```python
snapshot = veska.execution_snapshot(now_ms)     # plans + orders + metrics
snapshot = executor.execution_snapshot(now_ms)  # orders only
```

and, when it has an answer to apply:

```python
result = await veska.resolve_unknown(client_order_id, status, now_ms)
```

without touching `OrderManager.orders`, `PaperExecutor._pending` or
`Veska.plans`. No reconciliation decision is implemented, and none is implied.

## 21. Future live-executor seam

`execution/gateway.py` defines `ExecutionVenueGateway` and the transport-neutral
result models a venue adapter would normalise into: `VenueOrderAck`,
`VenueOrderSnapshot`, `VenueFillSnapshot`, `VenuePositionSnapshot`,
`VenueBalanceSnapshot`, `VenueGatewayCapabilities`.

**NO LIVE EXECUTOR IS IMPLEMENTED.** There is no subclass, no construction
site, and no import of this module anywhere in the running platform. There is
no authentication, no HTTP, no WebSocket, no signing, no key handling and no
exchange SDK — not stubbed, not commented out, not "for later". A seam carrying
a half-written credential path is not a seam, it is a liability.

The models are transport-neutral on purpose: a venue adapter's job would be to
normalise its own private truth *into* these shapes, so nothing above it ever
learns a venue-specific field name. That is what makes reconciliation possible
against more than one venue at a time.

A future implementation would have to answer questions this build has never had
to ask — how credentials are supplied, how a rejected signature surfaces, what
happens when the venue's clock disagrees with ours, how a partial network
failure is distinguished from a rejection. None is answered here, and none
should be guessed at.

## 22. Paper-only boundary

Unchanged and re-stated:

- `Veska.__init__` still raises `RuntimeError("this build accepts paper
  executors only")` for any executor whose `is_paper` is False.
- `PaperExecutor` is the only concrete `Executor`.
- `PAPER_CAPABILITIES.is_paper` is True.
- Nothing in `execution/**` imports a network client, and nothing added here
  changes that.

`ExecutionVenueGateway.is_paper` is False by definition — a gateway reaches a
real venue — and the composition root's guard is what keeps one from ever being
wired in. Nothing in this pass gives anybody a way around it.

## 23. What existing behaviour was preserved

**PaperExecutor** — latency modelling, book walking, partial fills, the queue
model, vanishing liquidity, cancel races, fees, slippage, TTL, UNKNOWN failure
injection and the logical-time entry points are all untouched. The fill loop
was not rebuilt or refactored.

**VenueRouter** — the urgency threshold, slippage limit arithmetic, maker/taker
choice and routing prices are byte-for-byte unchanged. Nothing was moved into
the policy layer, because no such move would have been behaviour-preserving.

**Cost model** — `walk_book`, `market_impact_bps`, `latency_cost_bps`,
`fee_bps` and `EdgeFrame` are untouched. Phase 4 ZEPHR economics remain frozen.

**Orchestrator** — the only changes are the three `execution_role` keyword
arguments and their import. The entry decision, risk checks, hedging decisions,
exit retries and kill-switch flow are unchanged.

---

## VALIDATION DEFERRED

Deliberately **not** proven by this pass. Each is a question a later validation
phase exists to answer with evidence:

- **IOC correctness** — whether an immediate-or-cancel order gets exactly one
  executable attempt.
- **FOK correctness** — whether fill-or-kill can be left partially filled.
- **POST_ONLY correctness** — whether a post-only order can take liquidity, and
  how such a fill is priced and charged.
- **Cancel-before-acknowledgement correctness** — what becomes of a cancel
  requested before the venue acknowledged the order.
- **UNKNOWN lifecycle correctness** — which call sites must read
  `is_outstanding` rather than `is_live`, and what an UNKNOWN order should do to
  order capacity.
- **Submission idempotency** — what a repeated `client_order_id` should do.
- **Multi-leg atomicity** — what a partially-submitted plan should leave
  behind.
- **Deadline enforcement** — whether `ExecutionPlan.deadline_ms` should be a
  submission gate, and where.
- **Slippage bounds** — whether the realised price is capped by the routed
  limit under every book shape.
- **Trade-flow queue realism** — whether passive queue progress tracks volume
  or update count.
- **Resource retention** — what `_pending` and the plan registry should
  actually retain, and for how long.
- **Replay equivalence** — whether the same logical sequence reproduces the
  same orders, timestamps and fills.
- **Performance** — the cost of the poll loop and the query surface under load.
- **Plan-status edge cases** — the treatment of mixed terminal states, and of a
  partially-filled plan whose remainder was cancelled.
- **Capability claims** — whether the executor honours every capability
  `PAPER_CAPABILITIES` advertises.

No tests were written for any of these, and none should be inferred from the
structures above. The framework's purpose is to make each of them testable in
isolation.

## Testing status

**VALIDATION DEFERRED — FRAMEWORK CONSTRUCTION ONLY.**

Nothing in this pass was executed under a test runner, no linter or type
checker was run, and no application was started. No test file was created or
modified.
