# Phase 6 — VESKA / paper-execution validation audit

**Status: PHASE 6 AUDIT SURFACE FROZEN / PRODUCTION REMEDIATION NEXT.**

**PRODUCTION CHANGES: NONE.**

This is the first validation pass after the full platform frame was built. It
inspects the Phase 6 execution boundary statically, constructs strict audit
tests and deterministic reproduction fixtures, and stops. External validation
runs the suite.

A failing audit test is expected and desirable where it proves a defect.
Nothing here has been fixed, weakened, skipped or xfailed.

---

## The question

> Does an authorised `ExecutionPlan` produce a deterministic, conservative,
> fully-accounted paper-order lifecycle without silently creating more risk
> than RUNE authorised, or treating unresolved venue truth as resolved?

Current finding inventory: **no, not yet.** Four findings are CRITICAL, nine HIGH.

## Baseline

| | |
| --- | --- |
| Repository | `godthefather1952/octopush` |
| Source branch | `phase11-12-paper-shadow-framework` |
| Source SHA | `73d377e7e2e84453ed8484adf0cdac47a9e202fb` |
| Audit branch | `validate-phase6-veska` |
| Production changes | **NONE** |
| Files added | `tests/audit/veska_fixtures.py`, `tests/audit/test_phase6_*.py` (16 modules), this document |

## External validation — CI #53

GitHub Actions run **#53** (run id `34161779859`) executed audit SHA
`50e139622352e55515947e17b482e4c0e4c43a7b`.

Full-suite result:

- **3224 passed**
- **215 failed**
- **2 skipped**
- **1 warning**

The raw 215 failures are **not** the Phase 6 defect count. One failure already
existed on the full-frame baseline, and 191 Phase 6 failures were blocked by an
audit fixture that constructed `OrderBookSnapshot` with the wrong schema.

At the moment CI #53 completed, the correct diagnosis was:

**AUDIT HARNESS CORRECTION REQUIRED BEFORE BEHAVIORAL RESULTS ARE AUTHORITATIVE.**

This commit corrects that harness surface. Current status is therefore:

**AUDIT HARNESS CORRECTED / EXTERNAL RERUN REQUIRED.**

### A. Valid product evidence

- Eighteen H22 numeric/schema assertions reached production models and failed
  independently of the broken market fixture. They establish P6-19 below.
- `ORDER_TRANSITIONS[SUBMITTING]` contains `ACKNOWLEDGED`, `REJECTED` and
  `UNKNOWN` only. It contains neither `CANCEL_PENDING` nor `CANCELLED`.
  This strengthens P6-1 and corrects the audit's earlier remediation note.
- P6-1 through P6-18 remain static findings except where this CI result
  explicitly corrects their supporting claim. Fixture-blocked behavior is not
  promoted to an external PASS or FAIL.

### B. Invalid / blocked audit results

- **191** Phase 6 tests failed before reaching execution because
  `veska_fixtures.venue_state()` passed `created_at` to
  `OrderBookSnapshot` and omitted required `exchange_ts` /
  `received_ts`.
- The non-paper VESKA guard test subclassed the `PaperExecutor` dataclass but
  did not set the instance field `is_paper=False`; it therefore constructed
  a paper executor and expected the wrong result.
- Three baseline-protection tests searched their own raw source strings and
  matched the forbidden phrases inside their own assertions/docstrings rather
  than finding actual forbidden syntax.

These are audit defects, not VESKA product defects.

### C. Pre-existing baseline failures

The source baseline
`73d377e7e2e84453ed8484adf0cdac47a9e202fb` already had:

- the `TF_FEED` compose contract failure because
  `${TF_FEED:-simulated}` is not a literal member of the old contract's
  `_FEEDS` check;
- `I001` in `agents/marin/agent.py`;
- `I001` in `agents/marin/source.py`;
- `SIM102` in `agents/okapi/registry.py`.

Those are deliberately unchanged and out of Phase 6 scope.

## External validation — CI #54 classification

GitHub Actions run **#54** (run id `34163556013`) executed audit SHA
`0a5d1f76e7bf45dfe40f94be38e85fa7e437bf6c`.

Python 3.12 full-suite result:

- **3347 passed**
- **92 failed**
- **2 skipped**
- **1 warning**

The 92 failures classify as:

- **1** known Phase 11/12 packaging baseline failure;
- **91** Phase 6 audit failures.

Of those 91 Phase 6 failures:

- **61** are attributable to production behaviour under the current audit;
- **30** are attributable to remaining audit-test / harness defects.

The 30 audit defects fall into four groups and are corrected by the audit-only
commit following this classification:

1. **Undrained `InMemoryEventBus` capture.** The Phase 6 harness subscribed a
   capture handler but read `published` without driving the real bus dispatch
   lifecycle. Those event-integrity failures did not establish missing product
   events.
2. **Incorrect replay fingerprint field.**
   `test_phase6_replay.py::_fingerprint` read `PositionState.average_price`;
   the production schema field is `average_entry_price`.
3. **Persistent `ExplodingBus` failure injection / stale publish ordinals.**
   The audit bus raised on every publication at or after the requested ordinal,
   so later consequences could not be observed, and two single-order tests
   injected failure before the event they claimed to measure.
4. **Two malformed production-probe invariants.** The probe hand-built fixed
   quantities while increasing expected prices, manufacturing its own
   approved-notional breach, and used VENUE_A's taker fee as an aggregate
   ceiling even though VENUE_B has a different fee schedule.

These are audit defects, not VESKA defects. Correcting them does not weaken any
valid Phase 6 invariant and changes no production module.

At CI #54, **P6-17 remained STATIC ONLY** because the event/state atomicity
failure injection was mis-targeted. That historical CI #54 classification is
preserved here; the subsequent Codespaces run supersedes it below.

At CI #54, **P6-18 was PARTIALLY CONFIRMED** because the submission-report
semantics were established while the event-count reproduction was blocked by
the undrained capture harness. The subsequent Codespaces run supersedes that
classification below.

The known baseline remains separate: the Phase 11/12 `TF_FEED` compose
contract failure plus Ruff `I001` in `agents/marin/agent.py` and
`agents/marin/source.py`, and `SIM102` in `agents/okapi/registry.py`.
None is changed here.

## External validation — Codespaces after audit-harness correction

The corrected audit commit
`9c6f92dc8173042e53b27510a1da370f3c6bd6c5`
was exercised in Codespaces on `validate-phase6-veska`.

The full-suite run from that command bundle ended with:

- **3241 passed**
- **101 failed**
- **98 errors**
- **1 skipped**
- **1 warning**

That full-suite headline is not the Phase 6 result. Redis-backed tests failed
because `127.0.0.1:6379` refused connections and PostgreSQL-backed tests failed
because `127.0.0.1:5432` refused connections. The pre-existing Phase 11/12
`TF_FEED` packaging failure also remained.

The short test summary contains **73 Phase 6 audit failures**. Classification:

- **70** are attributable to production behaviour;
- **3** remain attributable to the audit event-capture lifecycle.

The three remaining audit-side failures are:

- `TestEveryStateChangeIsPublished::test_a_fill_publishes_both_the_fill_and_the_order_update`;
- `TestEventTimestamps::test_a_fill_event_carries_the_fills_own_instant`;
- `TestPayloadIntegrity::test_a_fill_payload_reconstructs_the_fill`.

All three return a real fill from `Veska.poll()` and then inspect subscriber-
captured `PAPER_FILL` events without draining the real `InMemoryEventBus`
after that assignment-form poll. They require one final audit-only correction.

**P6-17 is EXTERNALLY CONFIRMED.** The corrected exact-Nth `ExplodingBus`
reproductions reached the intended publications. A failed `PAPER_FILL`
publication left the paper account mutated without a successfully published
fill event, and a failed `PAPER_ORDER_UPDATED` publication left the order moved
from SUBMITTING while the corresponding update was absent.

**P6-18 is EXTERNALLY CONFIRMED.** The corrected event capture removed the
previous blocker: `TestExecutionReportSemantics` no longer fails. Its three
tests establish that submit returns an incomplete, fill-empty report, that no
second `EXECUTION_REPORT` is produced as the order lifecycle advances, and
that the original report remains fill-empty after actual fills occur.

**P6-20 remains PROPOSED FINDING / EXTERNALLY CONFIRMED / NOT REMEDIATED.**
The Codespaces run again reproduced acceptance of a hand-built unconfigured
venue plan rather than fail-closed rejection.

## Final audit-only rerun

Command:

`./test.sh tests/audit`

Result on audit head `15a61a667fe6edffa523e54a05a3c787ef72e29f`:

- **1557 passed**
- **70 failed**
- **1 skipped**
- **0 known audit/harness failures**
- **0 Phase 11/12 baseline failures in this audit-only run**

Classification of the 70 failures:

- **69** confirmed production-behaviour invariant failures;
- **0** audit/harness failures;
- **0** baseline/out-of-phase failures;
- **1** inconclusive policy classification: H14 / P6-15 latency double counting.

The three assignment-form `PAPER_FILL` capture defects from the previous
Codespaces run are absent from the final short test summary. No
`test_phase6_event_integrity.py` test fails.

The remaining red tests map to the frozen production finding inventory. They
include partial multi-leg atomicity, cancel-before-ack, deadline enforcement,
idempotency, logical-time/replay divergence, passive-fill behaviour, entry
sizing, unknown-venue execution, resource retention, numeric safety,
IOC/FOK/POST_ONLY/GTC semantics, UNKNOWN capacity, and supplied-instant
resolution semantics.

H14/P6-15 remains intentionally **INCONCLUSIVE** as to policy intent: the audit
externally measured 0.2800 bps realised slippage when the book had already
moved by the configured 0.1400 bps latency drift, proving double application,
but the repository does not establish whether that extra pessimism is an
intentional stress model or an execution-model defect.

**PHASE 6 AUDIT SURFACE FROZEN.**

This freezes the validation surface, not Phase 6 production. Confirmed
production findings remain intentionally failing and unremediated.

Next:

**PHASE 6 BATCH A — EXPOSURE INTEGRITY**

- P6-1 — cancel-before-ACK
- P6-2 — partial multi-leg execution / stranded accepted leg
- P6-3 — idempotency / resubmission overwrite
- P6-4 — UNKNOWN capacity/risk accounting
- P6-10 — explicit ENTRY sizing authorization bypass

## Audit surface

Inspected: `execution/veska/**` (engine, executor interface, policy, preflight,
registry), `execution/paper/**` (executor, simulator, account),
`execution/oms/**`, `execution/router/**`, `execution/costs.py`,
`execution/gateway.py`, `core/models/execution.py`,
`core/models/opportunity.py`, `core/models/venue_execution.py`, and the Phase 6
integration points in `apps/orchestrator/orchestrator.py`.

Not audited: MARIN reconciliation policy (Phase 7), OKAPI hedging economics
(Phase 9), general Phase 12 behaviour. Phase 12 appears only where it touches
the execution path (H35).

### An oracle already in the repository

`execution/veska/policy.py` states what the platform means by IOC, FOK,
POST_ONLY and GTC, and says of itself:

> "It is **not** an enforcement layer, and this construction pass deliberately
> does not wire it into `PaperExecutor`'s fill loop. Whether the executor's
> behaviour matches these definitions is exactly what a later validation pass
> exists to determine."

This is that pass. The TIF findings below compare the executor against
`policy`'s own predicates, so a disagreement is the repository contradicting
itself rather than the audit imposing a preference.

---

## Execution invariants under test

1. One economic action carries one timeline (H1).
2. A cancellation request never disappears without a trace (H2).
3. A time-in-force means what `policy.py` says it means (H3, H4, H5).
4. UNKNOWN is outstanding, and never resolves itself (H6, H7, H20, H30).
5. A partially submitted plan leaves no unmanaged order (H8).
6. One `client_order_id` names one order (H9).
7. No new risk begins after an expired deadline (H10).
8. Risk-increasing size is bounded by `approved_notional` (H11, H12).
9. No taker fill beats the plan's slippage budget (H13).
10. Economic truth and recorded truth do not diverge silently (H17).
11. Nothing claims resolution over unresolved truth (H26, H28).
12. Execution is reproducible from logical time alone (H1, H36, H37).
13. The paper boundary is structural and unmoved by Phases 7–12 (H33–H35).

---

## Static findings

Ranked by demonstrated consequence, highest first.

---

### P6-1 — a cancel requested before acknowledgement is silently lost

| | |
| --- | --- |
| **Severity** | **CRITICAL** |
| **Area** | `execution/paper/executor.py` — `cancel`, `poll` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** A cancellation requested before a venue acknowledges an order
cannot vanish. The outcome may be "cancel on arrival" or an explicitly
modelled race — never "as though cancel was never requested".

**Evidence.**

```python
async def cancel(self, client_order_id, now_ms):
    order = self.oms.get(client_order_id)
    if order is None or not order.is_live:
        return
    if order.status in (OrderStatus.CREATED, OrderStatus.SUBMITTING):
        # Not yet acknowledged; the venue has nothing to cancel, so the
        # order resolves once it arrives.
        self._pending.setdefault(
            client_order_id, _Pending(ack_at=now_ms)
        ).cancel_at = now_ms
        return
```

`cancel_at` is recorded and the order's **status is not changed**. `poll` reads
`cancel_at` only inside `if order.status is OrderStatus.CANCEL_PENDING:`, and
by the time the order arrives `poll` has already moved it
SUBMITTING → ACKNOWLEDGED → OPEN. The branch is skipped, `cancel_at` is never
read again, and control falls through to `_attempt_fill`.

The comment says the order "resolves once it arrives". It does not.

**Minimal reproduction.** Submit; call `cancel` while SUBMITTING; advance past
`ack_at` with fillable liquidity. The order fills.

**Expected audit test.**
`test_phase6_cancel.py::TestCancelBeforeAcknowledgement` — four tests, of which
`test_an_order_cancelled_before_ack_does_not_fill_afterwards` and
`test_cancel_all_before_ack_has_the_same_guarantee` are the load-bearing pair.

**Consequence.** The kill switch's `cancel_all` runs over
`oms.live_orders()`, which includes SUBMITTING orders. Every order cancelled
during the latency window survives the cancellation and can trade. A halted
platform does not stop what it believes it stopped.

**Suggested remediation.** The current state machine has no direct
`SUBMITTING → CANCEL_PENDING` or `SUBMITTING → CANCELLED` transition. A future
production remediation therefore has at least two viable designs: (A) add an
explicit legal pre-ack cancellation transition, or (B) retain the pending
cancel request while SUBMITTING and have acknowledgement processing consume it
deterministically before the order can work. This audit does not choose or
implement either design.

**Dependencies.** None. Independent of every other finding.

---

### P6-2 — a partially submitted multi-leg plan strands an unmanaged order

| | |
| --- | --- |
| **Severity** | **CRITICAL** |
| **Area** | `execution/paper/executor.py::submit`, `execution/veska/engine.py::execute`, `apps/orchestrator/orchestrator.py::_decide` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** A plan that fails partway through submission may not leave a
live order that no plan record, no opportunity record and no cancellation path
can reach.

**Evidence.** `submit` awaits a bus publication **inside** the per-order loop.
If that publication raises on leg two, leg one is already in the OMS, is
SUBMITTING, has an `ack_at`, and will fill on the next poll.

The exception propagates out of `submit`, so three downstream statements never
run:

* `Veska.execute` → `self.registry.attach_orders(...)` — the plan record's
  `order_ids` stays empty, so `cancel_plan` iterates nothing.
* `Veska.execute` → `self.refresh_plan(...)` — the record never reflects it.
* `Orchestrator._decide` → `record.order_ids = [...]` — the opportunity does
  not name it either.

**Minimal reproduction.** `ExplodingBus(fail_on_publish=3)`, a two-leg plan,
`veska.execute`. Leg A is outstanding; `veska.get_plan(...).order_ids == []`.

**Expected audit test.** `test_phase6_atomicity.py::TestPartialMultiLegSubmission`
— four tests, of which `test_cancel_plan_can_reach_the_accepted_leg` and
`test_the_accepted_leg_does_not_fill_unnoticed` are the load-bearing pair.

**Consequence.** An unmanaged live order at a venue, invisible to the plan
registry, the opportunity record and every cancellation surface. Its fills land
on an order nothing is tracking.

**Suggested remediation.** Attach order ids to the plan record as each order is
created rather than after `submit` returns, or make `submit` create every order
before publishing anything.

**Dependencies.** Interacts with P6-1: the stranded leg cannot be cancelled
even if it were reachable, because a pre-ack cancel is lost.

---

### P6-3 — resubmitting a plan overwrites live order state

| | |
| --- | --- |
| **Severity** | **CRITICAL** |
| **Area** | `execution/oms/__init__.py::create`, `execution/veska/engine.py::execute` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** One `client_order_id` names one order identity. A retry must be
idempotent or refused — never a silent overwrite.

**Evidence.**

```python
def create(self, *, ..., client_order_id=None, ...):
    ...
    if client_order_id:
        order.client_order_id = client_order_id
    order.history = [(now, OrderStatus.CREATED)]
    self.orders[order.client_order_id] = order      # unconditional
    self.orders_created += 1
```

An unguarded assignment. `Veska.execute` registers the plan idempotently
(`register_plan` returns the held record) but then calls
`self.executor.submit(plan, now_ms)` unconditionally, so registry idempotency
never reaches the OMS. `PaperExecutor.submit` likewise overwrites
`self._pending[client_order_id]`.

**Minimal reproduction.** Submit a plan with a pinned `client_order_id`; apply a
0.4 fill; submit the same plan again. `filled_quantity` is 0.0, `fills` is
empty, `history` is one entry, and the pending arrival schedule has restarted.

**Expected audit test.** `test_phase6_idempotency.py::TestPlanResubmission` —
four tests, of which `test_resubmission_does_not_discard_an_existing_fill` is
the load-bearing one.

**Consequence.** The OMS's belief about what it holds is reset to zero while
the paper account keeps the fills that already happened. The two ledgers
disagree in the dangerous direction — the platform thinks it has *less* on than
it does — and MARIN would report the divergence as a mismatch of unknown
origin.

`preflight_plan` already detects the within-plan form
(`DUPLICATE_CLIENT_ORDER_ID`) and nothing consults it (P6-9).

**Suggested remediation.** Refuse a create for a resident id, or return the
existing order. Either is a behavioural change and belongs to remediation, not
here.

**Dependencies.** Amplified by P6-9 (preflight unwired).

---

### P6-4 — UNKNOWN read as settled at three orchestrator sites

| | |
| --- | --- |
| **Severity** | **CRITICAL** |
| **Area** | `apps/orchestrator/orchestrator.py` — `_advance_execution`, `_hedge_in_flight`, `_advance_exit` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** `is_live` answers "is this known to be working?";
`is_outstanding` answers "could this still have traded?". A caller deciding
whether the platform is exposed must use the second. The model's own docstring
says exactly this.

**Evidence.** An AST sweep of `Orchestrator` finds `is_live` at three sites and
`is_outstanding` at none:

* **`_advance_execution`** — `live = [o for o in orders if o.is_live]`. An
  UNKNOWN entry order is not live, so the method falls through to
  `filled <= 0` → *"Nothing traded: there is no position to hedge or
  monitor"* → `StrategyState.CLOSED`.
* **`_hedge_in_flight`** — an UNKNOWN hedge is not live, so the guard returns
  False and a **second hedge for the same symbol** is submitted.
* **`_advance_exit`** — an UNKNOWN exit order is not live, so the exit is
  retried, up to `max_exit_attempts`.

**Expected audit test.**
`test_phase6_unknown.py::TestOrchestratorConsumers` (four tests, one of which
pins the site inventory so a new call site is a new finding) and
`TestExecutorQuerySurfaces`.

**Consequence.**
*Entry:* an opportunity is closed as "nothing traded" while an order that may
be resting at the venue can still fill — into a position nothing is managing.
*Hedge:* duplicate hedge exposure.
*Exit:* a duplicate exit, potentially reversing the position.

**Refutation recorded alongside.** RUNE's committed-exposure snapshot tests
**terminality**, not liveness, and its docstring explains why at length:
releasing an UNKNOWN order's reservation "would free budget against risk the
platform still carries". That surface is correct, is asserted by
`TestRiskReservationIsCorrect`, and is the reason this finding is scoped to
three sites rather than four.

**Suggested remediation.** Use `is_outstanding` at all three sites, or hold the
opportunity in a distinct waiting state until the order resolves. Both change
behaviour and belong to remediation.

**Dependencies.** Depends on nothing; P6-5 shares its root cause.

---

### P6-5 — IOC rests until its TTL

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/paper/simulator.py::is_marketable`, `execution/paper/executor.py::poll` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** `policy.can_rest(TimeInForce.IOC)` is `False`. An IOC order gets
one executable attempt on arrival; any remainder terminates at once.

**Evidence.** `is_marketable` routes IOC to `fill_marketable`, which is
correct. Nothing then terminates the order. `poll`'s only remaining exit is

```python
if order.expires_at is not None and now_ms >= order.expires_at:
```

and `Veska.build_plan` sets `ttl_ms=self.settings.execution.default_order_ttl_ms`
on **every** order regardless of time-in-force. An IOC therefore rests for the
full TTL and can fill from liquidity that appears long after its single
attempt.

**Expected audit test.** `test_phase6_tif.py::TestIOC` — four tests covering
the brief's cases A–D.

**Consequence.** The router emits IOC for every crossing entry and exit leg, so
this is the shipped path. A trade the strategy intended to take-or-leave
becomes a resting order that can fill into a market that has already moved
away from the edge it was sized for. Every downstream number — realised
slippage, attribution, exit residual — is measured against an instruction the
platform did not actually give.

**Suggested remediation.** Terminate an IOC remainder after its first fill
attempt, using `policy.is_immediate`.

**Dependencies.** Shares a root cause with P6-6 and P6-7: `is_marketable`
classifies by instruction, and nothing enforces what the instruction means.

---

### P6-6 — POST_ONLY takes liquidity and is billed as a maker

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/paper/simulator.py::fill_passive`, `is_marketable` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** `policy.must_not_take(TimeInForce.POST_ONLY)` is `True`. A real
venue rejects or reprices a post-only order that would cross, precisely so it
cannot take.

**Evidence.** `is_marketable` returns False for POST_ONLY, so it reaches
`fill_passive`, which computes

```python
crossed = (order.side is Side.BUY and opposing_touch <= order.limit_price) or ...
if not crossed: ...probability scaled by traded_through...
else:           probability = self.config.maker_fill_probability
```

and then fills at `order.limit_price` with `liquidity=Liquidity.MAKER` and the
maker fee — **whatever `crossed` was**. The flag is set from the code path, not
from what the book did.

**Expected audit test.** `test_phase6_tif.py::TestPostOnly` (three tests) and
`TestGTCMakerFee`.

**Consequence.** Two errors compounding. The order removes liquidity it was
instructed never to remove, and it is billed at the maker tier — which is
strictly cheaper in the shipped fee schedules (asserted by
`test_the_maker_and_taker_fee_tiers_actually_differ`). Paper P&L is
systematically flattered on every low-urgency entry, which is the branch the
router takes whenever `urgency < 0.35`.

The same defect applies to a **crossing GTC limit**, which `is_marketable` also
classifies passive: `policy.expects_maker_fee(GTC)` is False precisely because
a GTC order can do either.

**Suggested remediation.** Derive the liquidity flag from `crossed`, and refuse
or reprice a crossing POST_ONLY order using `policy.must_not_take`.

**Dependencies.** Same root cause as P6-5 and P6-7.

---

### P6-7 — FOK is accepted and partially filled against a capability of `False`

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/paper/executor.py::PAPER_CAPABILITIES`, `execution/paper/simulator.py` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** `PAPER_CAPABILITIES.supports_fok` is `False`, and
`policy.requires_full_fill(TimeInForce.FOK)` is `True`. An unsupported
instruction must be refused, not executed under different semantics.

**Evidence.** `is_marketable` returns True for FOK, so it takes the ordinary
marketable path — which partially fills, capped by `max_partial_fraction` and
by available depth. Nothing anywhere compares the instruction against the
capability at submission time. `preflight_plan` does flag it
(`UNSUPPORTED_TIME_IN_FORCE`) and nothing calls preflight (P6-9).

**Expected audit test.** `test_phase6_tif.py::TestFOK` — four tests, plus
`TestCapabilityClaims::test_the_router_never_emits_an_unclaimed_instruction`.

**Consequence.** Bounded by reachability: the router emits only LIMIT/POST_ONLY
and LIMIT/IOC, so an FOK can reach the executor only through a hand-built or
replayed plan. That is why this is HIGH rather than CRITICAL. It is not
LOW, because `Veska.execute` accepts any plan it is handed — including one
rebuilt from a recording — and a capability claim that means nothing is a claim
a future live adapter will inherit.

The same argument applies to `supports_market=False` with `OrderType.MARKET`,
which additionally skips the limit-price filter in `fill_marketable` and so
would walk the book without a bound (P6-8's territory).

**Suggested remediation.** Reject an instruction the executor's own
capabilities disclaim, at submission.

---

### P6-8 — one economic action carries two timelines

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/oms/__init__.py` (clock reads) against `execution/paper/executor.py` (explicit `now_ms`) |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** An execution performed at logical instant T must be
reconstructible from T. This is the P2-14 property, which `PaperExecutor`'s
module docstring devotes twenty lines to.

**Evidence.** The executor honours it — a source scan finds no `now_ms()` call.
The `OrderManager` it writes through does not, and makes no such claim. An AST
sweep finds clock reads in `create`, `transition`, `mark_unknown`,
`resolve_unknown`, `reject` and `apply_fill`. So one submission at T produces:

| Field | Source |
| --- | --- |
| `submitted_at` | T (explicit) |
| `_pending.ack_at` | T + latency (explicit) |
| `created_at` | clock read |
| **`expires_at`** | **clock read + `ttl_ms`** |
| every `history` entry | clock read |

**`expires_at` is the one that costs money.** It is set from one clock and
compared against `now_ms` in `poll`, so an order's real lifetime is
`ttl_ms + (clock − T)` — longer or shorter than the TTL requested, by however
far the two have drifted. Under a live feed the clock advances while a tick
runs, which is exactly the scenario P2-14 was raised for.

**Expected audit test.** `test_phase6_logical_time.py` — three classes, of
which `test_the_ttl_is_measured_from_logical_submission_time` and
`TestReplayReconstructibility` are load-bearing.

**Consequence.** Order lifetimes are not reproducible under replay. An order
that expired in the original may still be working in the replay, or the
reverse — which changes fills, P&L and, downstream, kill-switch outcomes. It is
the same class of divergence P2-14 closed for `submitted_at`, left open one
layer down.

**Suggested remediation.** Thread `now_ms` through the `OrderManager` write
paths, as the executor already does.

---

### P6-9 — preflight is built, complete, and consulted by nothing

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/veska/preflight.py`, `execution/veska/engine.py::execute` |
| **Blocks Phase 6 validation** | No — it blocks nothing, which is the finding |

**Invariant.** A check that exists must either be called or be documented as
unreachable. Phase 6 documents the latter honestly — `Veska.preflight` says
"offered, not enforced" — so this is recorded as a *reachability* finding, not
a deception.

**Evidence.** `preflight_plan` correctly detects `EMPTY_PLAN`,
`MISSING_CLIENT_ORDER_ID`, `MISSING_VENUE`, `MISSING_SYMBOL`,
`NON_POSITIVE_QUANTITY`, `NON_POSITIVE_EXPECTED_PRICE`,
`DUPLICATE_CLIENT_ORDER_ID`, `UNSUPPORTED_ORDER_TYPE` and
`UNSUPPORTED_TIME_IN_FORCE`. `Veska.execute` contains no reference to it.

**Expected audit test.** `test_phase6_planning.py::TestPreflightContract` —
eleven tests: what it catches, the four things it explicitly does not check
(deadline, notional conservation, venue reachability), and that `execute` does
not consult it.

**Consequence.** Three findings above (P6-3's duplicate id, P6-7's FOK, the
MARKET case) are each already detected by code the platform ships and does not
run.

**Suggested remediation.** Wire it, once its failure mode is decided. Turning a
check into a submission gate is a behavioural change and belongs to
remediation.

---

### P6-10 — an ENTRY leg with an explicit quantity bypasses `approved_notional`

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/veska/engine.py::build_plan` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** Risk-increasing activity is bounded by what RUNE authorised.

**Evidence.**

```python
quantity = (
    leg.quantity
    if leg.quantity is not None and leg.quantity > 0
    else notional / routing.expected_price
)
```

`notional` is `decision.approved_notional`. The branch does not consult
`execution_role`, so an ENTRY leg carrying a quantity never sees the
authorisation at all.

**Minimal reproduction.** An ENTRY intent whose leg carries `quantity=1000.0`
at a price near 100, with `approved_notional=1000.0`, plans ~100,000 — a
hundred times the grant.

**Expected audit test.**
`test_phase6_planning.py::TestEntrySizingIsBoundedByAuthorisation` — five tests,
including two that record *why the branch exists* so remediation does not
remove it wholesale.

**Consequence and reachability.** The shipped `CrossVenueDetector` leaves entry
legs' `quantity` unset (asserted), so this is latent rather than live. It is
HIGH rather than CRITICAL for that reason — and it is not lower, because
nothing structural keeps it closed: any future detector, any replayed
opportunity, any hand-built intent reopens it, and the failure mode is
unbounded size on the risk-increasing path.

**Suggested remediation.** Bound an ENTRY leg's quantity by
`approved_notional / expected_price` regardless of what the leg carries, while
leaving EXIT and HEDGE exact.

---

### P6-11 — the plan deadline is carried and never enforced

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/**` (no reader), `apps/orchestrator/orchestrator.py::_advance_execution` (cleanup only) |
| **Blocks Phase 6 validation** | No |

**Invariant.** No new risk may begin after an expired deadline.

**Evidence.** A scan of `execution/**` finds no comparison against
`deadline_ms` — only assignments. `ExecutionPlanRecord.deadline_ms`'s own
comment says "nothing enforces it here". The single enforcement anywhere is the
orchestrator cancelling still-**live** orders once `tick_time > deadline`,
which is cleanup of risk already taken (and inherits P6-4's blind spot).

**Expected audit test.** `test_phase6_deadlines.py` — three classes covering
`deadline − 1`, exactly `deadline`, `deadline + 1`, acknowledgement past the
deadline, a fill past it and a cancel race past it. The boundary cases are
*pinned* rather than judged; only "no new risk begins" is asserted as an
invariant.

**Consequence.** A stale plan — one rebuilt from a recording, or held across a
slow tick — can open a position priced against a market that has moved on.
MEDIUM because the orchestrator constructs plans and submits them in the same
tick, so the window is small in the shipped path.

---

### P6-12 — passive queue progress accrues from polling, not from prints

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/paper/executor.py::_accrue_trade_flow` |
| **Blocks Phase 6 validation** | No |

**Invariant.** No new prints means no new queue progress.

**Evidence.**

```python
def _accrue_trade_flow(self):
    for order in self.oms.live_orders():
        ...
        volume = state.metrics.buy_volume + state.metrics.sell_volume
        pending.traded_through += volume * 0.01
```

Called on **every** `update_market`. `buy_volume` and `sell_volume` are TIDAL's
*rolling-window* totals — a stock, not a flow — so the same prints are credited
on every update.

**Expected audit test.** `test_phase6_passive_fills.py::TestTradeFlowAccrual` —
four tests, including one that holds the market completely static across ten
updates and one that shows progress scaling with update count.

**Consequence.** `fill_passive` scales its fill probability by
`traded_through`, so a resting order becomes progressively more likely to fill
the longer the platform merely *looks* at a quiet market. The bias is
optimistic and unbounded in the number of market updates, which is a tick-rate
parameter rather than anything economic.

---

### P6-13 — `max_partial_fraction` governs only the marketable path

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/paper/simulator.py` — `fill_marketable` vs `fill_passive` |
| **Blocks Phase 6 validation** | No |

**Invariant.** One partial-fill policy, or two policies each documented as
such.

**Evidence.** `fill_marketable` applies the cap; `fill_passive` does not,
filling `remaining * (1 - queue_ahead_fraction * random())` — up to 100% of the
order in one evaluation. With the cap at 0.10 the two paths model materially
different execution.

**Expected audit test.**
`test_phase6_passive_fills.py::TestPartialFillCap` — four tests, including the
marketable baseline that makes the comparison meaningful.

**Consequence.** Passive execution is modelled more optimistically than
aggressive execution, on the branch the router takes for low-urgency entries.
Whether that is deliberate is exactly the classification this finding asks for.

---

### P6-14 — a fill inherits a market-wide source timestamp

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/paper/executor.py::_attempt_fill` |
| **Blocks Phase 6 validation** | No |

**Invariant.** A fill's provenance is its own venue's and symbol's.

**Evidence.** `source_ts = self.market.source_data_timestamp if self.market
else None` — the market-wide value, regardless of which venue and symbol the
order belongs to. The platform already has the correct helper,
`MarketState.source_data_timestamp_for`, added for TIDAL-H4 and used by the
orchestrator when building intents; the execution path does not use it.

**Expected audit test.**
`test_phase6_passive_fills.py::TestFillProvenance` — five tests, covering a
stale venue beside a fresh one and a stale symbol beside a fresh one.

**Consequence.** A fill on a venue whose data is minutes old is stamped with
the freshest venue's timestamp. Any downstream consumer that judges freshness
from a fill — reconciliation, attribution, a future data-age gate — is handed a
number that describes different data.

---

### P6-15 — the same latency is charged twice

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/paper/executor.py::poll` and `_attempt_fill` |
| **Blocks Phase 6 validation** | No — classification required |

**Invariant.** Latency is modelled once.

**Evidence.** `poll` refuses to act until `now_ms >= pending.ack_at`, where
`ack_at = submitted_at + venue.latency_ms` — during which the caller has
supplied whatever the market actually did. `_attempt_fill` then passes *the
same* `latency_ms` into `latency_adjusted_levels`, which moves the book
adversely by `latency_drift_bps_per_100ms × latency / 100`.

**Expected audit test.** `test_phase6_slippage.py::TestLatencyModel` — four
tests. Two isolate the effects: a static book (measuring the synthetic drift
alone) and a book that has *already* moved by exactly the drift (measuring
whether it is charged again).

**Classification requested.** CONFIRMED DOUBLE COUNT / INTENTIONAL STRESS MODEL
/ INCONCLUSIVE. The audit does not decide: the simulator's stated purpose is to
be "pessimistic in the right places", and deliberate double pessimism is a
legitimate choice — but an undocumented one is indistinguishable from a defect,
and the drift is applied unconditionally rather than as a stated conservatism.

---

### P6-16 — the fast loop and `_pending` scale with session history

| | |
| --- | --- |
| **Severity** | **MEDIUM** |
| **Area** | `execution/paper/executor.py` — `_pending`, `poll`, `_accrue_trade_flow` |
| **Blocks Phase 6 validation** | No |

**Invariant.** "The resident set is bounded by concurrent activity rather than
by session length" — `OrderManager`'s own claim.

**Evidence.** The OMS honours it: `compact` archives terminal orders whose
fills are sealed. `PaperExecutor._pending` does **not** — the executor's own
docstring says the records "are deliberately left alone in this construction
pass", and `compact_terminal_state` delegates only to the OMS. Meanwhile `poll`
iterates `list(self.oms.orders.values())` and `_accrue_trade_flow` calls
`live_orders()`, which is a full scan of the resident map.

**Expected audit test.**
`test_phase6_resource_bounds.py` — three classes. `TestGrowthAfterManyTerminalOrders`
measures the residue after 2,000 completed orders;
`TestFastLoopWorkDoesNotScaleWithHistory` records the per-tick cost.

**Consequence.** A long session accumulates one `_pending` record per order
placed, forever. Small individually; unbounded in aggregate, and on the fast
loop.

**Paired with H20.** `TestUnknownSurvivesEverything` proves that no compaction
setting releases an UNKNOWN order, its pending record, or an unresolved plan —
so this finding cannot be "solved" by deleting unresolved truth.

---

### P6-17 — a fill can move the ledger without ever being published

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/paper/executor.py::_record_fill` |
| **Blocks Phase 6 validation** | Yes |
| **Status** | **EXTERNALLY CONFIRMED — NOT REMEDIATED** |
| **Priority** | **P1** |

**Invariant.** Economic truth and durable, replayable truth do not diverge
without a recovery mechanism.

**External reproduction.** The post-CI-#54 Codespaces run exercised the
corrected exact-Nth failure injection. It reproduced both account/OMS mutation
before failed `PAPER_FILL` publication and order-status mutation before failed
`PAPER_ORDER_UPDATED` publication. The behavioural evidence now agrees with
the original static ordering evidence.

**Evidence.** `_record_fill` applies the fill to the OMS and to the
`PaperAccount`, and *then* publishes `PAPER_FILL`. The `Recorder` is bus
middleware, so an event that never reaches `publish` is never recorded. If the
publication raises — a transport failure, or `CascadeCapacityExceeded` from the
in-memory bus's hard ceiling — the account has moved and the history has not.

**Expected audit test.** `test_phase6_atomicity.py::TestFillStateVersusEventTruth`
— three tests, using a duck-typed `ExplodingBus` because `InMemoryEventBus`
enqueues rather than dispatching and so cannot model a publication that raises.

**Consequence.** A replayed session reconstructs the ledger from recorded
fills. A fill that changed the account without being recorded is a divergence
replay cannot close, and MARIN would surface it as a mismatch of unknown
origin.

**Rated HIGH, not CRITICAL.** The exception propagates out of `poll` → `_settle`
→ `tick`, so the failure is loud rather than silent. That is the difference,
and it is the only thing separating this from P6-2.

---

### P6-18 — `ExecutionReport` is a submission report wearing a lifecycle name

| | |
| --- | --- |
| **Severity** | **LOW** |
| **Area** | `core/models/execution.py::ExecutionReport` |
| **Blocks Phase 6 validation** | No |
| **Status** | **EXTERNALLY CONFIRMED — NOT REMEDIATED** |
| **Priority** | **P3** |

**Invariant.** A model's fields describe what it can carry.

**External reproduction.** After correcting the capture lifecycle,
`TestExecutionReportSemantics` completed without a failure in the Codespaces
run. The audit therefore externally establishes the submission-only report
semantics rather than merely inferring them from source.

**Evidence.** `ExecutionReport` has `fills: list[FillEvent]` and
`complete: bool`. `submit` returns it with `complete=False` and no fills,
always, and nothing ever produces a later report — exactly one
`EXECUTION_REPORT` event is published per plan.

**Expected audit test.**
`test_phase6_registry.py::TestExecutionReportSemantics` — three tests,
including one that shows `report.filled_notional` reading 0.0 while the plan
has actually traded.

**Consequence.** Observability and naming only. A consumer reading `complete`
or `filled_notional` from a report gets a permanently-zero answer. No safety
consequence: nothing branches on either field.

**Suggested remediation.** Rename to `SubmissionReport`, or populate a later
report. Either is cosmetic relative to everything above.

---

### P6-19 — non-finite / invalid execution values are accepted

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `core/models/opportunity.py`, `core/models/execution.py` |
| **Blocks Phase 6 validation** | Yes |

**Invariant.** Execution-domain values that participate in sizing, price,
fees, slippage or expiry must reject non-finite values and invalid negative
durations at the model boundary.

**External evidence.** CI #53 produced eighteen H22 failures that do not use
the broken `OrderBookSnapshot` fixture and therefore reached the real Pydantic
models. The following values were accepted when the audit required
`ValidationError`:

- `PlannedOrder.quantity = +inf`;
- `PlannedOrder.expected_price = NaN, +inf, -inf`;
- `PlannedOrder.limit_price = NaN, +inf`;
- `ExecutionPlan.notional = NaN, +inf, -inf`;
- `ExecutionPlan.max_slippage_bps = NaN, +inf`;
- `FillEvent.fee = NaN, +inf, -inf`;
- `FillEvent.slippage_bps = NaN, +inf, -inf`;
- `PlannedOrder.ttl_ms = -1`.

The same external run also established that `quantity = 0`, `quantity = -1`
and `quantity = NaN` are already rejected; this finding does not generalize
past the values above.

**Consequence.** Malformed persisted, hand-built or replayed execution objects
can carry non-finite numbers into arithmetic, comparisons, accounting or
execution. Because Phase 6 preflight exists but is not enforced (P6-9), schema
validation is a meaningful boundary rather than redundant defense.

**Expected audit test.** `test_phase6_slippage.py::TestNumericSafety`.
The harness now expects the precise Pydantic `ValidationError` rather than a
blind `Exception`.

**Suggested remediation.** Add finite-value and duration constraints to the
execution-domain Pydantic models in a later production-remediation pass.

**Dependencies.** Amplified by P6-9; independently confirmed by CI #53.

---

### P6-20 — an unconfigured venue is silently assigned fallback execution semantics

| | |
| --- | --- |
| **Severity** | **HIGH** |
| **Area** | `execution/paper/executor.py::_latency`, `execution/veska/preflight.py`, `execution/veska/engine.py::execute` |
| **Blocks Phase 6 validation** | Yes |
| **Status** | **PROPOSED FINDING / EXTERNALLY CONFIRMED — NOT REMEDIATED** |
| **Priority** | **P1** |

**Invariant.** A hand-built, persisted or replayed plan naming a venue that is
not present in configuration must not be executed using fabricated venue
semantics.

**Evidence.** The normal router cannot build a plan for a venue with no market
state, so the primary production planning path is structurally blocked.
However, `Veska.execute` accepts a plan it is handed directly.
`PaperExecutor._latency` catches an unknown venue and silently returns `40`,
while the current preflight contract does not check venue reachability and
execute does not consult preflight in any case.

CI #54 externally demonstrated the behavioural consequence: a hand-built plan
for an unconfigured venue was accepted rather than refused and was assigned
the silent 40ms fallback.

**Consequence.** Replayed, persisted or externally constructed execution input
can acquire timing semantics for a venue the platform does not actually know.
That turns malformed execution truth into apparently valid paper execution
rather than failing closed.

**Scope.** This does not overturn H23's router-side refutation: the shipped
router still cannot produce the unknown venue. P6-20 covers the distinct
hand-built/replayed execution surface.

**Suggested remediation.** Later production remediation should fail closed on
unconfigured venues at the execution boundary and/or enforce a preflight that
actually validates venue reachability. No remediation is made in this audit
commit.

---

## Hypothesis matrix

`STATICALLY CONFIRMED` means the code was read and the defect established by
inspection; the constructed test proves it under execution.
`TEST CONSTRUCTED — EXTERNAL RESULT REQUIRED` means the invariant is asserted
and the outcome is not knowable without running it.

Final audit-only validation is complete. Verdicts below incorporate the frozen external result; passing audit hypotheses are stated as refuted defect concerns rather than silently dropped.

| # | Hypothesis | Verdict | Finding | Test module |
| --- | --- | --- | --- | --- |
| H1 | OMS logical-time consistency | **STATICALLY CONFIRMED** | P6-8 | `test_phase6_logical_time.py` |
| H2 | Cancel before ack | **STATICALLY CONFIRMED** | P6-1 | `test_phase6_cancel.py` |
| H3 | IOC semantics | **STATICALLY CONFIRMED** | P6-5 | `test_phase6_tif.py` |
| H4 | FOK semantics | **STATICALLY CONFIRMED** | P6-7 | `test_phase6_tif.py` |
| H5 | POST_ONLY must not take | **STATICALLY CONFIRMED** | P6-6 | `test_phase6_tif.py` |
| H6 | UNKNOWN is outstanding | **STATICALLY CONFIRMED** (3 sites); **REFUTED** for RUNE's reservation surface | P6-4 | `test_phase6_unknown.py` |
| H7 | UNKNOWN order capacity | **STATICALLY CONFIRMED** | P6-4 | `test_phase6_unknown.py` |
| H8 | Multi-leg submission failure | **STATICALLY CONFIRMED** | P6-2 | `test_phase6_atomicity.py` |
| H9 | Plan submission idempotency | **STATICALLY CONFIRMED** | P6-3 | `test_phase6_idempotency.py` |
| H10 | Absolute deadline | **STATICALLY CONFIRMED** | P6-11 | `test_phase6_deadlines.py` |
| H11 | Approved size conservation | **STATICALLY CONFIRMED** (latent) | P6-10 | `test_phase6_planning.py` |
| H12 | Plan conservation | **EXTERNALLY REFUTED AS A DEFECT CONCERN — tested conservation invariants held** | — | `test_phase6_planning.py` |
| H13 | Slippage hard bound | **EXTERNALLY REFUTED FOR THE TESTED SUPPORTED BOUND — no hard-bound failure remained** | — | `test_phase6_slippage.py` |
| H14 | Latency double counting | **INCONCLUSIVE — externally measured double application; policy intent unresolved** | P6-15 | `test_phase6_slippage.py` |
| H15 | Passive trade-flow accrual | **EXTERNALLY CONFIRMED** | P6-12 | `test_phase6_passive_fills.py` |
| H16 | `max_partial_fraction` | **EXTERNALLY CONFIRMED** | P6-13 | `test_phase6_passive_fills.py` |
| H17 | Event/state atomicity | **EXTERNALLY CONFIRMED — corrected exact-Nth failure injection reproduced state/event divergence** | P6-17 | `test_phase6_atomicity.py` |
| H18 | Fill source timestamp | **EXTERNALLY CONFIRMED** | P6-14 | `test_phase6_passive_fills.py` |
| H19 | Resource retention | **EXTERNALLY CONFIRMED** (`_pending`) | P6-16 | `test_phase6_resource_bounds.py` |
| H20 | UNKNOWN retention | **STATICALLY REFUTED** — compaction refuses non-terminal orders, and UNKNOWN is not terminal | — | `test_phase6_resource_bounds.py`, `test_phase6_unknown.py` |
| H21 | Execution report semantics | **EXTERNALLY CONFIRMED — submission-only lifecycle semantics reproduced after capture correction** | P6-18 | `test_phase6_registry.py` |
| H22 | Numeric / schema safety | **EXTERNALLY CONFIRMED BY CI #53** | P6-19 | `test_phase6_slippage.py` |
| H23 | Unknown venue | **PARTIAL — router path refuted; hand-built/replayed path externally confirmed** | P6-20 | `test_phase6_planning.py` |
| H24 | Same-timestamp precedence | **EXTERNALLY REFUTED AS A DEFECT CONCERN — tested precedence remained deterministic** | — | `test_phase6_cancel.py` |
| H25 | Production-like probe | **EXTERNALLY REFUTED AS AN AUDIT BLOCKER — corrected probe completed without failure** | — | `test_phase6_production_probe.py` |
| H26 | Execution registry truth | **EXTERNALLY REFUTED AS A DEFECT CONCERN — registry truth invariants held** | — | `test_phase6_registry.py` |
| H27 | Registry identity | **EXTERNALLY REFUTED AS A DEFECT CONCERN — identity invariants held** | — | `test_phase6_registry.py` |
| H28 | Execution snapshot consistency | **EXTERNALLY REFUTED AS A DEFECT CONCERN — snapshot consistency held** | — | `test_phase6_snapshots.py` |
| H29 | Query vocabulary | **EXTERNALLY REFUTED AS A DEFECT CONCERN — query vocabulary held** | — | `test_phase6_snapshots.py` |
| H30 | `resolve_unknown` contract | **PARTIAL — resolution works, but supplied `now_ms` is not used for OMS terminal history** | P6-8 | `test_phase6_unknown.py` |
| H31 | Executor capability claims | **STATICALLY CONFIRMED** (FOK, MARKET) | P6-7 | `test_phase6_tif.py` |
| H32 | Preflight contract | **STATICALLY CONFIRMED** (unwired) | P6-9 | `test_phase6_planning.py` |
| H33 | Gateway / paper boundary | **STATICALLY REFUTED** — no implementation, no construction site, no transport import, no credential | — | `test_phase6_paper_boundary.py` |
| H34 | Later phases must not change Phase 6 | **STATICALLY REFUTED** — `PaperExecutor` construction is unguarded by profile or feed | — | `test_phase6_paper_boundary.py` |
| H35 | Shadow creates no second execution path | **STATICALLY REFUTED** — the observer holds only a bus and a registry and publishes nothing | — | `test_phase6_paper_boundary.py` |
| H36 | Determinism | **EXTERNALLY CONFIRMED AS A DEFECT — wall-clock skew changes execution truth** | P6-8 | `test_phase6_replay.py` |
| H37 | Replay execution equivalence | **EXTERNALLY CONFIRMED — nine scenarios plus multi-venue diverge under wall-clock skew** | P6-8 | `test_phase6_replay.py` |

**Refuted hypotheses, stated positively.** H20, H33, H34 and H35 were tested
adversarially and the implementation held. H6 is refuted for RUNE's
committed-exposure surface specifically. H23 is refuted for the reachable path.
None of these was dropped for being uninteresting.

---

## Audit modules

| Module | Hypotheses | What it isolates |
| --- | --- | --- |
| `veska_fixtures.py` | — | Harness, deterministic settings, hand-built books, `ExplodingBus`, `RecordingClock` |
| `test_phase6_logical_time.py` | H1 | Clock skew against explicit execution time |
| `test_phase6_cancel.py` | H2, H24 | Cancel before/after ack; same-instant precedence |
| `test_phase6_tif.py` | H3, H4, H5, H31 | IOC, FOK, POST_ONLY, GTC fee tier, capability claims |
| `test_phase6_unknown.py` | H6, H7, H20, H30 | UNKNOWN across queries, consumers, retention, resolution |
| `test_phase6_atomicity.py` | H8, H17 | Partial multi-leg submission; state vs event truth |
| `test_phase6_idempotency.py` | H9 | Resubmission, duplicate ids, duplicate fills, overfill |
| `test_phase6_deadlines.py` | H10 | The deadline boundary, one millisecond at a time |
| `test_phase6_planning.py` | H11, H12, H23, H32 | Sizing, conservation, unknown venue, preflight |
| `test_phase6_slippage.py` | H13, H14, H22 | Slippage bound sweep, latency isolation, numeric safety |
| `test_phase6_passive_fills.py` | H15, H16, H18 | Trade-flow accrual, partial cap, fill provenance |
| `test_phase6_event_integrity.py` | H17, H21 (support) | Which events execution emits, their instants, correlation ids and payload integrity |
| `test_phase6_registry.py` | H21, H26, H27 | Plan status derivation, identity, lookups, report semantics |
| `test_phase6_snapshots.py` | H28, H29 | Snapshot against the OMS; the five query surfaces |
| `test_phase6_resource_bounds.py` | H19, H20 | Growth after 2,000 orders, paired with retention safety |
| `test_phase6_replay.py` | H36, H37 | Nine scenarios × two wall clocks |
| `test_phase6_paper_boundary.py` | H33, H34, H35 | Gateway seam, profile combinations, shadow observer, baseline scope |
| `test_phase6_production_probe.py` | H25 | 120 plans through the real stack, fully tallied |

**No skips. No xfails.** Asserted from inside the suite by
`test_phase6_paper_boundary.py::TestBaselineProtection`.

---

## Fixture discipline

* **No production default is changed.** `audit_settings` calls `load_settings`
  exactly as production does.
* **`deterministic_settings` pins the dice, never the economics.** Only
  `liquidity_vanish_probability` and `maker_fill_probability` are overridden;
  depth, fees, latency, slippage budgets, partial-fill caps and queue modelling
  are the shipped values. Any call site needing a different value states which
  variable and why.
* **No existing test was touched.** `tests/audit/helpers.py`,
  `noro_fixtures.py` and `rune_fixtures.py` are untouched; the Phase 6 suite
  brings its own `veska_fixtures.py`.
* **No monkeypatching of what is measured.** Asserted by
  `TestBaselineProtection::test_the_audit_never_monkeypatches_execution_internals`.
* **No network.** The profile and feed cases in H34 construct settings and
  inspect wiring statically; nothing is started.

---

## Baseline protection

**PRODUCTION CHANGES: NONE.**

The commit contains only `tests/audit/**` and this document. No production
module, no CI configuration, no existing test and no other phase's
documentation was modified. `TestBaselineProtection` asserts the complementary
property from inside the suite: the audit package defines no production class,
never patches execution internals, and declares no skip or xfail.

---

## External validation requirements

For the final audit-harness cleanup, run exactly:

```bash
./test.sh tests/audit
```

Do not use a full-suite run as the authoritative validation for this cleanup.
The previous Codespaces full run was contaminated by unavailable Redis and
PostgreSQL services and by the separately tracked Phase 11/12 packaging
baseline.

The expected shape after adding the three missing event drains is approximately
**70 failing Phase 6 product invariants and zero known audit-harness failures**.
That count is an expectation, not a target: do not change an assertion merely
to make the result equal 70.

Classify every remaining failure as production behaviour, audit/harness defect,
known baseline/out-of-phase, or inconclusive. Freeze the audit surface only if
no credible audit/harness defect remains.

---

## What this audit does not establish

* Nothing about MARIN's reconciliation policy (Phase 7).
* Nothing about OKAPI's hedging economics (Phase 9).
* Nothing about Phase 12 beyond its non-interference with execution.
* Nothing about the bus's delivery, ordering or capacity semantics — validated
  in Phase 1 and not re-litigated.
* Nothing about the replay engine itself. H37 exercises the *property* replay
  depends on; the engine was validated in Phase 2 and is untouched.
* CI #53 supplied authoritative external evidence for H22/P6-19 and the SUBMITTING transition inventory. Fixture-blocked behavioral hypotheses still require a clean rerun.
