# Phase 6 — VESKA / paper execution audit

An audit of the complete paper execution boundary: `execution/veska/**`,
`execution/router/**`, `execution/paper/**`, `execution/oms/**`,
`execution/costs.py`, their schemas, and the orchestrator seams that hand work
to them and take it back.

- **Base SHA:** `1fb9a1bbd4eec558d8f59550cc1b411ae524cadf` (`phase5-rune-remediation-e2`)
- **Branch:** `phase6-veska-audit`
- **Production changes:** **NONE.** No file outside `tests/audit/**` and
  `docs/` is in the diff.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Audit tests are expected to fail where they state an invariant this build does
not yet hold. Nothing was skipped, xfailed, weakened, or tuned to obtain green.

---

## 1. The question

Not "can `PaperExecutor` produce fills" — it can, and the existing unit suite
proves it. The question is:

> Does an authorised `ExecutionPlan` produce a conservative, deterministic,
> venue-realistic and fully-accounted order lifecycle, without ever creating
> more exposure than the plan permits or silently treating unresolved venue
> truth as resolved?

Two of the four words carry most of the findings. **Venue-realistic** turns out
to be where the simulator is *optimistic* rather than pessimistic: an IOC that
rests like a GTC and a POST_ONLY that takes liquidity both hand the strategy
fills a real venue would never grant. **Fully-accounted** is where a live order
can exist that no part of the platform can name.

## 2. Phase 5 baseline

Frozen at CI #44 on the base SHA: 3087 passed / 2 skipped / 0 failed (Python
3.12); backend contract pre-check 747 passed / 1 tooling-only skip; Python 3.11
1382 passed / 126 skipped / 0 failed; Ruff PASS; Mypy core PASS; paper boundary
61 passed. Nothing under `agents/rune/**`, `risk/limits/**` or
`risk/kill_switch/**` was touched by this pass.

## 3. What is intentional and is not attacked

The audit distinguishes conservative simulation from incorrect venue
semantics. These are intended properties, asserted as controls rather than
challenged:

- paper executor only; no private or authenticated exchange path;
- explicit logical `now_ms` at every execution entry point, and no clock read
  inside `PaperExecutor` (the P2-14 invariant, re-verified and holding);
- seeded fill simulation, adverse latency modelling, vanishing liquidity,
  partial fills, cancel races, duplicate-fill idempotency;
- `UNKNOWN` as unresolved venue truth, never as failure;
- risk-reducing exits and hedges bypassing entry edge/consensus gates;
- exits closing the ACTUAL position quantity, via `OpportunityLeg.quantity`;
- `RISK_LIMIT_BREACH` remaining the live safety backstop.

## 4. Findings

Ranked most severe first. A hypothesis becomes a finding only where the
evidence confirms it.

---

### P6-1 — a cancel requested before acknowledgement is silently discarded

| | |
| --- | --- |
| **Severity** | CRITICAL |
| **Area** | Cancellation / kill-switch interaction |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** A cancellation request must never vanish. An order may still
fill after a cancel is requested — that is a race a real venue has — but it
must not behave as though no cancellation was ever asked for.

**Evidence.** `PaperExecutor.cancel` splits on status. For `CREATED`/`SUBMITTING`
it records an arrival time and returns:

```python
self._pending.setdefault(client_order_id, _Pending(ack_at=now_ms)).cancel_at = now_ms
return
```

The order stays `SUBMITTING`. `poll` consumes `cancel_at` only inside
`if order.status is OrderStatus.CANCEL_PENDING:` — and the pre-ack path never
sets that status. So on the next poll the order transitions
`SUBMITTING → ACKNOWLEDGED → OPEN`, the cancel branch is skipped entirely, and
`_attempt_fill` runs against a book it may cross.

**Minimal reproduction.** `tests/audit/test_veska_cancel_semantics.py::TestCancelBeforeAcknowledgement::test_a_pre_ack_cancel_must_not_silently_disappear`
— submit at T, cancel at T+1, poll at T+35 (`ack_at`) with a crossing book.

**Consequence.** `Orchestrator._protect` answers a kill-switch
`cancel_all_requested` with `veska.cancel_all(tick_time)`. An order submitted
on the tick the switch engages is exactly an order still in `SUBMITTING`, so
the kill switch does not stop it: the platform opens exposure *after* being
told to stop. This is the P5-6 property ("a safety action cannot be undone by
state that predates it") failing at the execution boundary rather than in the
kill switch.

**Suggested remediation.** Transition to `CANCEL_PENDING` on the pre-ack path
too, and let `poll` resolve it on arrival under the existing
`cancel_wins_race` model — or, if a venue-realistic model prefers "cancel on
arrival always wins for an unacknowledged order", implement that explicitly and
document it. Either is defensible; dropping the request is not.

**Dependencies.** Interacts with P5-6. No RUNE change required.

---

### P6-2 — an ENTRY leg can bypass RUNE's approved notional

| | |
| --- | --- |
| **Severity** | CRITICAL |
| **Area** | Planning / risk hand-off |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** Ordinary entry execution may never exceed the notional RUNE
authorised. The quantity override exists for exits and hedges, which must close
*that quantity*.

**Evidence.** `Veska.build_plan`:

```python
quantity = (
    leg.quantity
    if leg.quantity is not None and leg.quantity > 0
    else notional / routing.expected_price
)
```

`intent.is_exit` is never consulted. `OpportunityLeg.quantity`'s own docstring
says "Entries leave this unset and are sized from the intent's notional" — the
rule exists and is documented, and nothing enforces it. `ExecutionPlan.notional`
is separately set from `approved_notional`, so an oversized plan advertises the
approved figure to every downstream reader.

**Minimal reproduction.** `tests/audit/test_veska_planning.py::TestRiskApprovedSizeCannotBeBypassed::test_an_entry_leg_quantity_cannot_exceed_the_approved_notional`
— approved 1,000; entry legs carrying 2.5 units at a 40,000 mid.

**Consequence.** Every Phase 5 gate — committed exposure, gross, venue,
position, net, leverage, unhedged — is enforced against `approved_notional`. A
planner that discards it discards all of them at once, silently, with the plan
header still reporting the approved number.

**Scope, honestly stated.** The shipped `CrossVenueDetector` leaves entry leg
quantities unset, so this is a latent gap rather than an observed breach: the
production probe (§10) measures how many entry legs deviate. It is CRITICAL
because the only thing standing between an approved size and an arbitrary one
is a convention no code checks.

**Suggested remediation.** Honour `leg.quantity` only when `intent.is_exit` is
true (or on an explicit hedge path), and fail planning loudly otherwise.

**Dependencies.** None. Must not change exit or hedge behaviour.

---

### P6-3 — a partially-submitted plan strands an unmanageable live order

| | |
| --- | --- |
| **Severity** | CRITICAL |
| **Area** | Submission atomicity / hand-off |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** A partially-submitted plan must never leave a live order that
the opportunity record cannot identify, manage or cancel.

**Evidence.** `PaperExecutor.submit` loops leg by leg, creating each order in
the OMS and publishing before moving to the next; there is no `try`, no
rollback, no compensating action anywhere in the method. The orchestrator
learns the ids only afterwards:

```python
report = await self.veska.execute(plan, self.tick_time)
record.order_ids = [order.client_order_id for order in report.orders]
```

If anything between the first accepted order and that assignment raises — a bus
publication failure is the concrete path — the caller gets no report, and
`record.order_ids` stays empty while live orders sit in the OMS.

**Minimal reproduction.** `tests/audit/test_veska_submission_atomicity.py::TestPartialSubmissionLeavesAnUnmanageableOrder`
— a two-leg plan whose second publication raises.

**Consequence.** The stranded order can acknowledge and fill. Worse, the record
is left in `EXECUTING` with no order ids, so the next `_advance_execution` sees
`live == []` and `filled == 0`, transitions to `CLOSED` and releases the
strategy reservation — while a real position is being opened.

**Mitigation that does exist.** `cancel_all` iterates the OMS rather than the
records, so an engaged kill switch does reach the order. That bounds the blast
radius; it does not make the order manageable in ordinary operation.

**Suggested remediation.** Either make submission atomic per plan (stage the
orders, publish after all legs are accepted, roll back on failure), or hand the
caller the ids as they are created rather than only in the return value.

---

### P6-4 — `UNKNOWN` is read as "resolved" at three orchestrator call sites

| | |
| --- | --- |
| **Severity** | CRITICAL |
| **Area** | Unresolved venue truth |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** An order whose venue-side state is unknown is never assumed to
have failed. It may block, require reconciliation, or be accounted as
unresolved — but never optimistically resolved.

**Evidence.** `PaperOrder.is_live` is `not is_terminal and status is not
UNKNOWN`. That is correct for its own name, and it makes `is_live` false for
two different situations: *finished* and *unknown*. Three call sites read the
second as the first.

| Call site | Expression | What follows |
| --- | --- | --- |
| `_advance_execution` | `live = [o for o in orders if o.is_live]` | `live == []`, `filled == 0` → `CLOSED`, reservation released |
| `_advance_exit` | `if any(o.is_live for o in orders): return` | residual present → a second full exit is submitted |
| `_hedge_in_flight` | `live = [...if order.is_live]` | `del self.working_hedges[symbol]` → a duplicate full hedge |

**Minimal reproduction.** `tests/audit/test_veska_unknown_state.py::TestOrchestratorCallSites`
— a real order driven to `UNKNOWN` with zero known fills, evaluated through the
same expression each call site uses.

**Consequence.** An entry order that times out closes its opportunity and frees
its budget while possibly resting at the venue. An exit that times out is
re-sent in full, so if the first later fills the position is closed twice. A
hedge that times out is duplicated.

**What is already correct, and bounds the finding.** P5-1's committed-exposure
snapshot keys off `not order.is_terminal`, so an UNKNOWN order still *reserves
notional*. The executor itself never advances, expires or cancels an UNKNOWN
order, and `compact` never archives one. The gap is in the orchestrator's
lifecycle decisions, not in the exposure arithmetic.

**Suggested remediation.** Give the three call sites an explicit "unresolved"
branch distinct from "terminal" — block the transition and route to
reconciliation. Do not change `is_live`, whose current definition is right.

**Dependencies.** MARIN owns resolution (Phase 7). This finding is about not
pre-empting it.

---

### P6-5 — IOC orders rest like GTC orders

| | |
| --- | --- |
| **Severity** | HIGH |
| **Area** | Time-in-force semantics |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** Immediate-or-cancel grants exactly one immediate executable
attempt on arrival; any unfilled remainder terminates at once.

**Evidence.** `PaperExecutor.poll` contains no reference to `TimeInForce` at
all. The only reader in the execution path is `simulator.is_marketable`, which
uses TIF to choose a *fill model*, not a lifetime. An IOC order therefore
follows the ordinary lifecycle and lives until `expires_at`
(`default_order_ttl_ms`, 5,000ms).

**Minimal reproduction.** `tests/audit/test_veska_tif_semantics.py::TestIocSemantics`
— A: no reachable liquidity at arrival; B: partial fill leaves a working
remainder; C: liquidity appearing 100ms later fills the remainder; D: the order
survives to its GTC time-to-live.

**Consequence.** This is optimism, not pessimism. The router emits IOC for
*every* aggressive entry leg, so on the shipped path the strategy is credited
with up to five seconds of fill opportunities per order that a real venue would
have cancelled on arrival. Paper fill rates, and every P&L number derived from
them, are systematically better than reality.

**Suggested remediation.** Terminate an IOC's remainder on the poll that first
evaluates it after acknowledgement — `FILLED` if fully filled, `CANCELLED` if
partially, `CANCELLED`/`EXPIRED` if not at all.

---

### P6-6 — a POST_ONLY order can take liquidity and be booked as a maker

| | |
| --- | --- |
| **Severity** | HIGH |
| **Area** | Time-in-force semantics / fee accounting |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** A post-only order must never remove liquidity. If the market has
moved through its limit by the time it arrives, a real venue rejects or
reprices it.

**Evidence.** `is_marketable` returns False for POST_ONLY (correct), so the
order reaches `fill_passive`, which handles the crossed case explicitly:

```python
crossed = (order.side is Side.BUY and opposing_touch <= order.limit_price) or ...
if not crossed: ...  # queue-progress branch
else:
    probability = self.config.maker_fill_probability
...
price = order.limit_price
fee = self._fee(quantity * price, fees, Liquidity.MAKER)
```

A crossed post-only order therefore fills at the full maker probability, at its
posted limit, and is charged the **maker** fee.

**Minimal reproduction.** `tests/audit/test_veska_tif_semantics.py::TestPostOnlySemantics`
— B: crossing at arrival must not trade; C: no crossing fill may be labelled
MAKER.

**Consequence.** Doubly optimistic: a fill that should not exist, priced better
than the market and charged at the rebate tier. The router's low-urgency path
emits POST_ONLY with `is_maker=True`, so ZEPHR's economics are quoted against
a fee the venue would not have granted.

**Suggested remediation.** On arrival, if a POST_ONLY order would cross, reject
it (or reprice to the passive touch) and record which. Never route it through
the maker-fee path.

---

### P6-7 — one execution action stamps an order from two clocks

| | |
| --- | --- |
| **Severity** | HIGH |
| **Area** | Logical time / replay fidelity |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** Every economic order transition caused by an execution action at
logical `T` must be reconstructible from `T`.

**Evidence.** `PaperExecutor` honours its contract — it reads no clock, and
`tests/unit/test_execution_time_fidelity.py` still passes. But it does not
stamp orders; `OrderManager` does, and every mutating operation there calls
`self.clock.now_ms()`: `create`, `transition`, `mark_unknown`,
`resolve_unknown`, `reject`, and the transition inside `apply_fill`. None of
them accepts a `now_ms` argument, so there is nowhere to pass `T` in.

One submission therefore produces `submitted_at = T` alongside `created_at =
clock`, and one fill produces `fill.created_at = T` alongside a `FILLED`
history entry stamped from the clock.

**Minimal reproduction.** `tests/audit/test_veska_time_fidelity.py` — the same
logical sequence run with the OMS clock at `T` and at `T ± 5,000`.

**Consequence, forensic.** The published event and the order's own history
carry different instants for the same transition. Two runs of one logical
sequence produce different recorded orders — which the recorder persists and
MARIN reconciles against.

**Consequence, economic.** `expires_at = clock + ttl_ms` is computed from the
clock and then compared against logical time in `poll`. A clock lagging the
logical instant can expire an order **on the poll that acknowledges it**,
consuming a five-second lifetime before the order existed; a leading clock
extends it past its own deadline.

**Suggested remediation.** Give the OMS's mutating operations an explicit
`now_ms`, exactly as `KillSwitch.engage`/`clear`/`evaluate` were given one in
P5-16, and have the executor thread its caller's instant through. The clock may
remain the fallback for genuinely operator-initiated actions.

**Dependencies.** Touches Phase 2's replay guarantees; the fix belongs with
whoever owns the OMS signature, not with RUNE.

---

### P6-8 — one `client_order_id` is not one order identity

| | |
| --- | --- |
| **Severity** | HIGH |
| **Area** | Idempotency |
| **Blocks Phase 6 validation** | **Yes** |

**Invariant.** A retried, redelivered or replayed submission is either
idempotent or explicitly rejected. It never silently overwrites.

**Evidence.** `OrderManager.create` ends with
`self.orders[order.client_order_id] = order` and performs no existence check.
`PlannedOrder` carries its own `client_order_id`, so resubmitting a plan
presents the same identity twice — and the second call replaces the first
object outright.

**Minimal reproduction.** `tests/audit/test_veska_submission_atomicity.py::TestPlanSubmissionIdempotency`
— resubmit a plan whose order is OPEN, or partially filled.

**Consequence.** Filled quantity resets to zero, fills already applied to the
account and published are erased from the order, history is discarded, and the
venue still holds the original order. `orders_created` counts two while
`orders` holds one, so even the counters disagree. Reconciliation loses the
evidence it would need to notice.

**Suggested remediation.** Refuse a `create` for an id already resident, or
return the existing order unchanged. Whichever, say so in the report's `notes`.

---

### P6-9 — trade-flow accrual credits the same prints on every market update

| | |
| --- | --- |
| **Severity** | HIGH |
| **Area** | Passive-fill realism |
| **Blocks Phase 6 validation** | No (measurement realism, not safety) |

**Invariant.** No new trade flow means no new queue progress.

**Evidence.** `_accrue_trade_flow` runs on every `update_market` and does
`pending.traded_through += (buy_volume + sell_volume) * 0.01`.
`BookMetrics.buy_volume`/`sell_volume` come from TIDAL's rolling
`TradeFlowWindow`: they are the total still *inside* the window, not the volume
since the previous snapshot. The same prints are therefore credited once per
market update for as long as they remain in the window.

**Minimal reproduction.** `tests/audit/test_veska_passive_fills.py::TestTradeFlowAccrual`
— one burst of volume, then twenty republications with no new prints.

**Consequence.** `traded_through` drives the non-crossed branch of
`fill_passive`'s probability, so passive fills become easier in proportion to
how often the market updates rather than to how much traded. Paper maker fills
are systematically optimistic, and the bias scales with tick rate.

**Suggested remediation.** Credit the *increment* — track the previously seen
window total per venue/symbol and accrue the positive difference.

---

### P6-10 — `ExecutionPlan.deadline_ms` is carried but never enforced

| | |
| --- | --- |
| **Severity** | MEDIUM |
| **Area** | Plan lifetime |
| **Blocks Phase 6 validation** | No |

**Invariant.** At minimum, a stale plan must not begin NEW risk after its
absolute execution deadline.

**Evidence.** `TradeIntent.deadline_ms` is documented as "absolute deadline
after which the intent must not be executed"; `build_plan` copies it onto the
plan; `PaperExecutor.submit` contains no reference to it. Nor does the order
lifetime relate to it: `ttl_ms` is measured from submission, so an order can
acknowledge and fill arbitrarily far past the deadline.

**Minimal reproduction.** `tests/audit/test_veska_planning.py::TestDeadlineEnforcement`
— submission at deadline −1, +0, +1 and +60,000; and a fill 2,000ms past it.

**Mitigation that exists.** `_advance_execution` cancels still-live orders once
`tick_time` passes `record.intent.deadline_ms`. That covers entries whose
record is still tracked; it is not a property of the execution boundary, it is
one tick late, and it does not cover a plan submitted late in the first place.

**Suggested remediation.** Reject a plan at `submit` when `now_ms >
plan.deadline_ms`, with the reason in `notes`. Equality should remain
permitted, matching every other inclusive boundary in the platform.

---

### P6-11 — `_pending` and `Veska.plans` grow with session length

| | |
| --- | --- |
| **Severity** | MEDIUM |
| **Area** | Resource bounds |
| **Blocks Phase 6 validation** | No |

**Invariant.** Active execution memory scales with current activity plus an
explicit bounded retention, not with total session lifetime — unless the
structure is a deliberate, durably bounded lifetime ledger.

**Evidence.** `OrderManager` does this correctly: `compact` archives terminal
orders into `ArchivedOrders` aggregates once reconciliation has released their
fills. Beside it:

- `PaperExecutor._pending` has no `del`, no `pop`, no `clear`. An entry
  survives its order being archived out of the OMS entirely.
- `Veska.plans` retains every whole `ExecutionPlan`, with every `PlannedOrder`
  inside it, keyed by `plan_id`, for the life of the process.

**Minimal reproduction.** `tests/audit/test_veska_resource_bounds.py` — 1,000
completed round trips with compaction after each.

**Consequence.** Neither is in the fast loop (`poll` walks `oms.orders`,
`_accrue_trade_flow` walks `live_orders()`), so this is footprint rather than
latency. But a structure whose size is a function of uptime does not have a
size, it has a schedule.

**Suggested remediation.** Delete a `_pending` entry when its order becomes
terminal *and* is compacted; give `Veska.plans` the `ArchivedOrders` treatment
(aggregates, or a bounded window) or a documented cap.

**Boundary this must not cross (H20).** UNKNOWN orders and their `_pending`
records are legitimately unbounded and must stay resident until something
authoritative resolves them. Collecting them would trade a memory question for
a safety one.

---

### P6-12 — an unconfigured venue is accepted with an invented latency

| | |
| --- | --- |
| **Severity** | MEDIUM |
| **Area** | Fail-closed routing |
| **Blocks Phase 6 validation** | No |

**Invariant.** An order naming a venue the platform is not configured for is
rejected explicitly, not executed against a made-up parameter.

**Evidence.** `PaperExecutor._latency` catches `KeyError` and returns 40.
`submit` uses it and accepts the order. The mitigation on the shipped path is
that `build_plan` reads `settings.venue(leg.venue).fees` first and raises — so
this is reachable only through a persisted or replayed plan that bypasses
planning.

**Consequence, and why it is not LOW.** The accepted order can never fill
(`_book_view` finds no venue state, so `_attempt_fill` returns before touching
`.fees`), so it opens no exposure. But it consumes order capacity and reserves
committed exposure until it expires — and `cancel` reads
`settings.venue(order.venue).cancel_latency_ms` **without** the fallback, so a
kill-switch `cancel_all` that touches such an order raises `KeyError` out of
`_protect`. A fail-open acceptance becomes a fail-hard cancel path.

**Minimal reproduction.** `tests/audit/test_veska_resource_bounds.py::TestUnknownVenueHandling`.

**Suggested remediation.** Reject at `submit` when the venue is not in
settings.

---

### P6-13 — state moves before its event is published, with no recovery path

| | |
| --- | --- |
| **Severity** | MEDIUM |
| **Area** | Event/state atomicity |
| **Blocks Phase 6 validation** | No — dependency recorded |

**Invariant.** The durable, replayable event history and the economic state do
not silently diverge.

**Evidence.** `_record_fill` applies the fill to the OMS and the account and
*then* publishes `PAPER_FILL`. `submit` creates the order and then publishes.
`poll` transitions and then publishes. The executor has exactly one `except`
(the venue-latency lookup); nothing wraps a mutation or a publication, and
there is no retry, outbox or republish path.

The recorder attaches as bus **middleware**, so it runs *inside* `publish`.
That is the right place — and it means a publication that fails is one the
recorder never saw. There is no after-the-fact route by which a lost
`PAPER_FILL` reaches the durable record.

**Minimal reproduction.** `tests/audit/test_veska_event_integrity.py::TestAFailedPublicationLeavesStateAhead`.

**Consequence.** A position and a cash balance that no event explains: invisible
to replay, to the dashboard's derived views, and to reconciliation.

**Dependency.** Whether this is a durability hole or an observability note
depends on the recorder and on MARIN. The audit records the mechanism; the
remediation belongs to whichever phase owns the recorder's guarantees.

---

### P6-14 — `max_partial_fraction` applies to taker fills only

| | |
| --- | --- |
| **Severity** | LOW |
| **Area** | Simulator configuration |
| **Blocks Phase 6 validation** | No |

`ExecutionConfig` documents it as "simulated fraction of an order that may fill
on one pass". `fill_marketable` applies it; `fill_passive` never reads it, and
sizes instead from `queue_ahead_fraction`. Under a 0.10 cap a single passive
evaluation can fill the whole order.

Scoped by the shipped default being `1.0`, which makes the cap inert in
production. This is a documentation-versus-behaviour finding: either apply it
to both paths or say it is taker-only.

**Reproduction.** `tests/audit/test_veska_passive_fills.py::TestMaxPartialFraction`.

---

### P6-15 — a fill's provenance is market-wide, not order-specific

| | |
| --- | --- |
| **Severity** | LOW |
| **Area** | Execution provenance |
| **Blocks Phase 6 validation** | No |

`_attempt_fill` stamps `source_data_timestamp` from
`self.market.source_data_timestamp` while the fill was computed from
`_book_view(order)`, which reads one venue and one symbol.
`MarketState.source_data_timestamp_for(legs)` exists for exactly this
distinction and is used by TIDAL, NORO, ZEPHR and the orchestrator (TIDAL-H4).

The market-wide value is the *oldest* across venues, so the error direction is
conservative for freshness — but the timestamp can belong to an entirely
unrelated instrument, which makes it useless as provenance. Severity is LOW
because no current safety path consumes `FillEvent.source_data_timestamp`; it
would rise if one ever did.

**Reproduction.** `tests/audit/test_veska_passive_fills.py::TestFillProvenance`.

---

### P6-16 — `ExecutionReport` is a submission acknowledgement

| | |
| --- | --- |
| **Severity** | LOW |
| **Area** | Observability / documentation |
| **Blocks Phase 6 validation** | No |

The model's docstring says "What VESKA reports back after working a plan". It
is emitted once, immediately after `submit`, always with `complete=False` and
an empty `fills` list; `complete=True` appears nowhere in the executor or in
`Veska`. Nothing ever states a plan's outcome in the event stream.

Not a safety issue — order and fill events carry the truth — but the model name
and docstring describe a completion report the system does not produce.

**Reproduction.** `tests/audit/test_veska_event_integrity.py::TestExecutionReportSemantics`.

---

### P6-17 — a FOK order can be left partially filled

| | |
| --- | --- |
| **Severity** | LOW |
| **Area** | Time-in-force semantics |
| **Blocks Phase 6 validation** | No |

`TimeInForce.FOK` is accepted by `PlannedOrder` and `PaperOrder`, and
`is_marketable` routes it through `fill_marketable`, which produces partials
from book-depth exhaustion and from `max_partial_fraction`. Fill-or-kill admits
no third outcome.

LOW because the router never emits FOK, so this is reachable only through a
hand-built, persisted or replayed plan. The honest options are to implement it
or to refuse it; it is currently neither.

**Reproduction.** `tests/audit/test_veska_tif_semantics.py::TestFokSemantics`.

---

## 5. Hypothesis verdicts

| ID | Hypothesis | Verdict | Evidence |
| --- | --- | --- | --- |
| **H1** | OMS logical-time leakage | **CONFIRMED** (P6-7) | Six OMS operations read `self.clock.now_ms()`; none accepts `now_ms`. `expires_at` is computed from the clock and compared against logical time. |
| **H2** | Cancel before acknowledgement is lost | **CONFIRMED** (P6-1) | Pre-ack path sets `cancel_at` without `CANCEL_PENDING`; `poll` reads it only in that state. |
| **H3** | IOC rests instead of terminating | **CONFIRMED** (P6-5) | `poll` never reads `time_in_force`; IOC lives to `expires_at`. |
| **H4** | FOK can partial | **CONFIRMED** (P6-17) | FOK routes to `fill_marketable`, which partials on depth and on `max_partial_fraction`. Unreachable from the router. |
| **H5** | POST_ONLY can take | **CONFIRMED** (P6-6) | `fill_passive`'s `crossed` branch fills at the limit, labelled `MAKER`, at the maker fee. |
| **H6** | UNKNOWN treated as resolved | **CONFIRMED** (P6-4) | Three orchestrator call sites read `is_live` as "resolved"; executor and OMS handling are correct. |
| **H7** | UNKNOWN grants free order capacity | **CONFIRMED** | `open_orders()` is `oms.live_orders()`, which excludes UNKNOWN, and feeds RUNE's `MAX_OPEN_ORDERS`. Distinct from the notional side, which is already correct — see below. |
| **H8** | Multi-leg submission atomicity | **CONFIRMED** (P6-3) | No rollback in `submit`; ids assigned only after `execute` returns. |
| **H9** | Plan submission idempotency | **CONFIRMED** (P6-8) | `create` overwrites by `client_order_id` with no check. |
| **H10** | Deadline not enforced | **CONFIRMED** (P6-10) | `submit` contains no reference to `deadline`. |
| **H11** | Risk-approved size bypassable | **CONFIRMED** (P6-2) | `build_plan` never consults `is_exit`. |
| **H12** | Plan conservation | **REFUTED** | For entries without explicit quantities, per-leg planned notional equals `approved_notional` exactly, and N legs give N times it — verified at 1, 2 and 3 legs, duplicate venues and skewed prices. |
| **H13** | Slippage hard cap | **REFUTED** for router-shaped orders | `limit = expected_price × (1 ± budget)` and `fill_marketable` filters the book to it, so signed slippage is capped by construction; insufficient depth partials rather than overpaying. A `MARKET` order has no cap, but the router never emits one. |
| **H14** | Latency double-counted | **CONFIRMED as a mechanism; conservative in effect** | The same venue latency sets `ack_at` *and* drifts the arrival book. In a static market only the drift applies (the documented model); in a market that already moved, both do. Always adverse, never favourable — it suppresses fills and understates P&L, and cannot manufacture exposure. Classified as an over-pessimistic model rather than a safety defect; recorded so the choice is deliberate. |
| **H15** | Trade-flow accrual repeats | **CONFIRMED** (P6-9) | Rolling-window totals credited once per `update_market`. |
| **H16** | `max_partial_fraction` inconsistent | **CONFIRMED** (P6-14) | Applied in `fill_marketable` only. Inert at the shipped default of 1.0. |
| **H17** | Event/state atomicity | **CONFIRMED** (P6-13) | State mutates before publication; recorder is middleware inside `publish`; no recovery path. |
| **H18** | Fill source timestamp | **CONFIRMED** (P6-15) | Market-wide value stamped on a single-venue fill. |
| **H19** | Terminal resource bounds | **CONFIRMED** (P6-11) | `_pending` and `Veska.plans` have no eviction; `OrderManager` compaction is correct and is the control. |
| **H20** | UNKNOWN retention is legitimate | **CONFIRMED as intended behaviour** | `compact` takes only terminal orders; UNKNOWN orders and their `_pending` records stay resident. P6-11 must not be "fixed" by collecting them. |
| **H21** | `ExecutionReport` semantics | **CONFIRMED** (P6-16) | `complete=True` appears nowhere. |
| **H22** | Numeric / schema safety | **PARTIAL** | `PlannedOrder` and `ExecutionPlan` reject zero, negative and non-finite quantities, prices, TTLs and notionals through their field constraints. The audit found no meaningful hole in the plan schemas; the gap is `PaperOrder.limit_price`, which is optional and unconstrained beyond its type. Ranked LOW and not written up as a separate finding. |
| **H23** | Unknown venue fail-closed | **CONFIRMED, with a mitigating first failure** (P6-12) | `_latency` invents 40ms; `build_plan` raises first on the shipped path; `cancel` raises `KeyError` on the kill-switch path. |
| **H24** | Cancel / expiry / fill ordering | **REFUTED** | `poll`'s precedence is explicit and deterministic: cancel arrival, then fill, then expiry. Pinned structurally and behaviourally at coincident instants. A cancel in flight suspends fill evaluation entirely — recorded as a model choice, not a defect. |
| **H25** | Production-like probe | **BUILT** | `tests/audit/test_veska_production_behaviour.py`. Figures are reported by external validation; none are asserted here. |

### H7 in detail — capacity versus notional

The two halves of "committed exposure" behave differently for an UNKNOWN order:

| Input | Predicate | UNKNOWN counted? |
| --- | --- | --- |
| `RiskContext.committed_exposure` (notional, venue, position, net, unhedged) | `not order.is_terminal` | **Yes** — correct, from P5-1 |
| `RiskContext.open_orders` → `MAX_OPEN_ORDERS` | `oms.live_orders()` → `is_live` | **No** |

So uncertainty reserves notional but grants order-count headroom, and the more
orders that time out the more capacity the gate reports. No RUNE change is
proposed in this branch; the truth and the dependency are recorded.

## 6. Execution invariant inventory

What the boundary must guarantee, and where each is asserted:

| Invariant | Status | Where |
| --- | --- | --- |
| Planned per-leg notional equals the approved notional | **HOLDS** for entries without explicit quantities | `test_veska_planning.py` |
| No authorised leg is silently dropped | **HOLDS** — an unroutable leg fails the whole plan | `test_veska_planning.py` |
| Taker slippage never exceeds `max_slippage_bps` | **HOLDS** for router-shaped orders | `test_veska_slippage.py` |
| Insufficient depth partials rather than overpaying | **HOLDS** | `test_veska_slippage.py` |
| The latency model is never favourable | **HOLDS** | `test_veska_slippage.py` |
| Fill/cancel/expiry precedence is deterministic | **HOLDS** | `test_veska_cancel_semantics.py` |
| Execution is reproducible run to run | **Asserted** across ten lifecycle scenarios | `test_veska_replay.py` |
| Duplicate fills are idempotent | **HOLDS** (`_applied_fills`, `PaperOrder.apply_fill`) | baseline suite |
| Terminal orders leave memory; UNKNOWN ones do not | **HOLDS** | `test_veska_resource_bounds.py` |
| The executor reads no clock | **HOLDS** | `test_veska_time_fidelity.py` |
| A cancellation request is never discarded | **FAILS** (P6-1) | `test_veska_cancel_semantics.py` |
| An entry never exceeds its approved size | **FAILS** (P6-2) | `test_veska_planning.py` |
| No live order is unmanageable | **FAILS** (P6-3) | `test_veska_submission_atomicity.py` |
| UNKNOWN is never optimistically resolved | **FAILS** (P6-4) | `test_veska_unknown_state.py` |
| IOC gets one attempt | **FAILS** (P6-5) | `test_veska_tif_semantics.py` |
| POST_ONLY never takes | **FAILS** (P6-6) | `test_veska_tif_semantics.py` |
| One action, one timeline | **FAILS** (P6-7) | `test_veska_time_fidelity.py` |
| One `client_order_id`, one order | **FAILS** (P6-8) | `test_veska_submission_atomicity.py` |
| No new flow, no queue progress | **FAILS** (P6-9) | `test_veska_passive_fills.py` |

## 7. Audit files

| File | Covers |
| --- | --- |
| `tests/audit/veska_fixtures.py` | Builders: settings, books, venue states, the executor rig, plans |
| `tests/audit/test_veska_planning.py` | H10, H11, H12, H22 |
| `tests/audit/test_veska_time_fidelity.py` | H1 |
| `tests/audit/test_veska_tif_semantics.py` | H3, H4, H5 |
| `tests/audit/test_veska_cancel_semantics.py` | H2, H24 |
| `tests/audit/test_veska_unknown_state.py` | H6, H7, H20 |
| `tests/audit/test_veska_submission_atomicity.py` | H8, H9 |
| `tests/audit/test_veska_slippage.py` | H13, H14 |
| `tests/audit/test_veska_passive_fills.py` | H15, H16, H18 |
| `tests/audit/test_veska_event_integrity.py` | H17, H21 |
| `tests/audit/test_veska_resource_bounds.py` | H19, H20, H23, poll cost |
| `tests/audit/test_veska_replay.py` | Execution determinism across ten scenarios |
| `tests/audit/test_veska_production_behaviour.py` | H25 probe, paper-only boundary |

## 8. Paper-only boundary

Re-proved at the execution boundary rather than inherited: `Executor.is_paper`
is True, `Veska.__init__` refuses a non-paper executor structurally, and no
module under `execution/**` imports `requests`, `httpx`, `aiohttp`,
`websockets`, `socket` or `urllib` (checked from the AST, not by substring) or
mentions an API key, secret, private key, wallet, signature or HMAC. `submit`
talks to the OMS, the account and the bus, and to nothing else.

Nothing in this audit adds or proposes live trading.

## 9. Baseline preservation

No production file is in the diff. No existing test was modified — not one
assertion, threshold, seed, simulator default or market parameter. Every audit
scenario is built from its own fixtures.

Audit tests that state an invariant this build does not hold are expected to
fail under external validation. That is the deliverable, not a defect in the
suite.

## 10. Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing here was executed under a test runner and no application was started.
Every claim in this report is derived from the code as written, plus short
read-only static checks: each structural assertion was verified against the
real source with `inspect.getsource`, every audit module was imported to
confirm it collects, and the fixtures were constructed once to confirm the
books and plans they build have the shapes the tests assume. Those checks are
disclosed here rather than presented as test results.

The production probe's figures (§H25) are deliberately absent from this
document. They will be produced by external validation.
