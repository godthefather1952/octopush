# Phase 5 — RUNE Hard-Risk Audit

RUNE is the last thing between a wanted trade and a submitted order. Its
question is narrow: *even if every strategy and analytical agent wants this
trade, is this exact proposed exposure safe to authorise now?*

This audit asks whether the present implementation deserves to be that
boundary. It changes no production code.

## Baseline

- **Source branch:** `phase34-lifecycle-causality`
- **Source SHA:** `2125519dfbbc62849cbc5b9c0e58936bccfff682` — externally
  validated: 2294 passed / 2 skipped / 0 failed (3.12 full suite), 1381 / 126 /
  0 (3.11 unit+contract), 747 backend contracts, 61 paper-boundary, Ruff PASS,
  Mypy core PASS, GitHub Actions SUCCESS.
- **Audit branch:** `phase5-rune-audit`
- **Production changes:** **NONE.**
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## External validation

| Run | Result |
| --- | --- |
| Baseline before the audit (`2125519`) | 2294 passed / 2 skipped / **0 failed** |
| Phase 5 audit, run #34 (`4a5c4fa`) | 2698 passed / **51 failed** / 2 skipped |
| Python 3.11 unit + contract, run #34 | 1381 passed / 126 skipped / **0 failed** |

Also PASS on run #34: Mypy core, paper boundary, backend contract pre-check,
and the entire pre-existing baseline suite. Ruff failed on four audit-only
issues (one `SIM300`, three `RUF002` ambiguous multiplication signs in audit
docstrings), fixed in the cleanup pass described below.

**Most of the 51 failures are the deliverable.** The audit tests state safety
invariants, not current behaviour, so a failure where a finding exists is the
finding being demonstrated. Run #34 independently reproduced the
concurrent-reservation, strategy-exposure-unit, open-order-capacity,
future-timestamp, lifetime-error-rate, headroom-completeness, kill-switch
(reachability, recovery, flatten), exposure-projection and configuration
findings.

It also produced something the audit had not predicted: the production-like
probe observed live net and unhedged exposure beyond configured limits on the
shipped simulation, with no kill-switch trigger. See **Production-like
observations**.

The 3.11 job passing 1381 / 0 failed confirms that no pre-existing test was
disturbed: every failure is in `tests/audit/`, which that job does not select.

### Cleanup pass

A follow-up commit removed **only false audit-harness failures** — defects in
the test construction, not in RUNE:

- The RUNE-AI comparisons built two independent `intent()` objects for the
  with-AI and without-AI runs. `TradeIntent.intent_id` is minted by a
  `default_factory`, and `deterministic_fingerprint` includes it (correctly:
  a decision naming a different intent *is* a different decision), so the
  fingerprints differed for a reason unrelated to commentary. Every comparison
  now evaluates one shared `intent` against one shared `RiskContext`, which is
  the stronger test. No field was removed from the fingerprint.
- `test_stale_commentary_is_attached_but_inert` re-evaluated the *same* intent
  an hour later, so it was rejected by `INTENT_NOT_EXPIRED` and
  `MARKET_DATA_FRESH` — measuring the deadline gates, not the AI boundary. It
  now ages the commentary while keeping the trade temporally valid.
- `test_the_decision_id_is_the_only_minted_value` varied two minted things at
  once. It now reuses one intent, so `decision_id` is demonstrably the only
  field that moves.

No assertion was weakened, no test was skipped, xfailed or deleted, and no
finding assertion was touched. The cleaned failure count is deliberately not
stated here: it has not been measured.

---

## Architecture

```
TradeIntent (Orchestrator._build_intent, sized from ZEPHR's curve)
   → Orchestrator._risk_check          builds RiskContext
      → Rune.evaluate                  publishes RISK_PASS / RISK_FAIL
         → RuneCore.evaluate           sizing, then gating
            → _headroom                largest notional fitting the size limits
            → _gate                    21 deterministic gates
         → RiskDecision
   → Veska.build_plan                  one PlannedOrder per leg
   → PaperExecutor                     fills
   → PaperAccount / PortfolioState     the state the NEXT decision reads
   → RiskUtilization                   dashboard
   → KillSwitch                        post-hoc emergency boundary
```

Two layers, one verdict: `RuneCore` decides, `RuneAI` comments. Commentary is
attached to the decision after the verdict exists and can write only
`ai_commentary`, `ai_concern_level` and `AI:`-prefixed reason codes.

Three paths deliberately bypass the entry gates — exits, hedges and
kill-switch flattening — because refusing to *reduce* exposure would be
backwards. Their safety rests on being exposure-reducing by construction,
audited in `test_rune_exit_hedge_safety.py`.

## Gate inventory

Read off `RuneCore._gate`, in evaluation order. All 21 are mandatory, so
`UNKNOWN` blocks in every case.

| # | Gate | Size-sensitive | Fail-closed input |
| --- | --- | --- | --- |
| 1 | `KILL_SWITCH_CLEAR` | no | — |
| 2 | `SYSTEM_HEALTHY` | no | `health is None` → UNKNOWN |
| 3 | `EXECUTION_HEALTHY` | no | no snapshot, or VESKA never heartbeat → UNKNOWN |
| 4 | `CONSENSUS_THRESHOLD` / `CONSENSUS_COMPLETE` | no | incomplete consensus → FAIL |
| 5 | `MARKET_DATA_FRESH` | no | missing timestamp → UNKNOWN |
| 6 | `INTENT_NOT_EXPIRED` | no | — |
| 7 | `MIN_EXPECTED_EDGE` | no | NaN edge fails |
| 8 | `LIQUIDITY_SUFFICIENT` | yes | no ZEPHR curve → UNKNOWN |
| 9 | `HEDGE_AVAILABLE` | no | — |
| 10 | `MAX_ORDER_NOTIONAL` | yes | — |
| 11 | `MAX_POSITION_NOTIONAL` | yes | — |
| 12 | `MAX_GROSS_EXPOSURE` | yes | — |
| 13 | `MAX_NET_EXPOSURE` | yes | **not in `_headroom`** |
| 14 | `MAX_LEVERAGE` | yes | non-positive equity → FAIL; **not in `_headroom`** |
| 15 | `MAX_VENUE_EXPOSURE` | yes | — |
| 16 | `MAX_STRATEGY_EXPOSURE` | yes | — |
| 17 | `MAX_DAILY_LOSS` | no | — |
| 18 | `MAX_DRAWDOWN` | no | — |
| 19 | `MAX_UNHEDGED_EXPOSURE` | **yes** | classified `no` by this audit; refuted by production evidence and corrected in Remediation D (P5-18) — the residual it reads is measured, but the intent's worst INTERMEDIATE residual scales with its notional |
| 20 | `MAX_OPEN_ORDERS` | no | — |
| 21 | `MAX_ERROR_RATE` | no | — |

Plus `MIN_TRADE_NOTIONAL`, which is not in the list: it short-circuits inside
`evaluate` before `_gate` runs, and a decision rejected there carries that one
gate alone.

Boundary conventions, measured: `MAX_DAILY_LOSS` and `MAX_DRAWDOWN` are strict
(`<`); every other numeric gate is inclusive (`<=`), except `MAX_OPEN_ORDERS`
which is strict on a count. `MIN_TRADE_NOTIONAL` rejects when
`headroom < min_trade_notional`, so headroom exactly at the minimum is
eligible.

## RiskContext construction

`Orchestrator._risk_check` builds it. What each field reads matters more than
the field list:

| Field | Source | Moves when |
| --- | --- | --- |
| `portfolio` | the tick's `PaperAccount` snapshot | a fill lands |
| `kill_switch` | `state.kill_switch` | a trigger fires |
| `health` | `state.health`, snapshotted in `_protect` | each tick |
| `consensus_complete` / `_threshold` | the consensus result | each evaluation |
| `open_orders` | `len(veska.open_orders())` | submission and terminality |
| `error_rate` | `Orchestrator._error_rate()` | lifetime bus counters |
| `unhedged_notional` | `okapi.total_unhedged(portfolio)` | a fill lands |
| `strategy_exposure` | `sum(working_notional.values())` | **authorisation** |
| `max_economical_notional` | ZEPHR's curve ceiling | each evaluation |
| `hedge_available` | `okapi.hedge_available(...)` | market state |

`strategy_exposure` is the **only** field that moves at authorisation rather
than at fill. Every other exposure dimension reads a portfolio that has not
yet changed.

## Headroom analysis

`_headroom` returns `max(0, min(...))` over: the requested notional,
`max_order_notional`, `(max_gross_exposure − gross) / legs`,
`(max_strategy_exposure − strategy_exposure) / legs`, per-leg
`max_venue_exposure − venue`, per-leg `max_position_notional − position`, and
ZEPHR's ceiling.

The `/ legs` division is internally consistent with the gates, which project
`notional × legs` for gross and strategy. Sizing then lands the projection
exactly on the limit — verified.

**Two size-sensitive gates are absent from that list: `MAX_NET_EXPOSURE` and
`MAX_LEVERAGE`.** Both are monotonically increasing in `notional` with every
other term fixed at decision time, so both always admit a smaller passing size.
`evaluate`'s own docstring says a size-based gate "can only fail if the
reduction could not make it pass" — for these two it can fail while a reduction
would have passed.

**A third was absent for a different reason, and this audit's own
classification is what missed it.** `MAX_UNHEDGED_EXPOSURE` was recorded above
as not size-sensitive, because the residual it reads is a measurement of the
filled book rather than a projection of the trade. That reasoning is about the
gate's *input* and not about what the limit governs: an intent's worst
INTERMEDIATE residual — one whole leg, held between the first fill and the
second — is proportional to its notional. The classification was refuted by
production evidence rather than by inspection (CI #38), and is corrected as
P5-18.

`min` over a candidate list containing NaN returns the running minimum rather
than NaN only because the ZEPHR ceiling is appended last. That is append order,
not a safety property.

## Exposure projection

Measured against a post-trade portfolio built with `PositionState.apply` — the
platform's own fill arithmetic — rather than against a re-derivation of the
gate formula.

| Gate | Formula | Verdict |
| --- | --- | --- |
| gross | `gross + notional × legs` | conservative; never understates |
| net | `\|net + Σ(side.sign × notional)\|` | exact for equal-notional legs; conservative when a leg offsets |
| leverage | `(gross + notional × legs) / equity` | conservative; pre-trade equity denominator |
| position | `max over legs of (position + notional)` | **understates when two legs share one venue/symbol** |
| venue | `max over legs of (venue + notional)` | **understates when two legs share a venue** |

The two `max`-over-legs formulas assume at most one leg per venue and at most
one per venue/symbol. The shipped cross-venue strategy satisfies that, so the
understatement is unreachable today — but RUNE is a generic hard-risk layer and
the assumption is nowhere stated or enforced.

Directional behaviour is conservative throughout: an exposure-*reducing* leg is
projected as though it opened a new position, because an entry leg carries no
quantity and RUNE cannot know it will close. Overstatement is safe and is
classified as a design decision, not a defect.

## Pending-risk reservation

`Orchestrator._seek` detects, decides and executes each opportunity in one
pass, with a `bus.drain()` but no settlement between them. So two opportunities
can be authorised against the same unchanged portfolio inside one tick.

`working_notional` is the only reservation, and it feeds only
`MAX_STRATEGY_EXPOSURE`. Gross, venue, position, net and leverage all read the
filled portfolio, which has not moved. Each decision in a concurrent sequence
therefore projects only its own legs against an empty book.

## Strategy-exposure unit analysis

Three places name the same quantity, in two different units:

- **Reservation** — `working_notional[oid] = decision.approved_notional`, stored
  once per opportunity. `approved_notional` is *per-leg*: VESKA sizes every leg
  as `notional / expected_price`, and RUNE's own gross projection multiplies by
  `len(legs)`.
- **Gate** — `strategy_exposure + intent.notional × len(legs)`. The incoming
  intent is counted at `notional × legs`; every already-working opportunity is
  counted at `notional × 1`.
- **Dashboard** — `{STRATEGY: sum(working_notional.values())}`, the same
  per-leg sum, displayed against a limit the gate enforces in `notional × legs`.

With `max_strategy_exposure = 100,000` and two-leg trades at the 25,000
per-order cap, each opportunity holds 50,000 of real strategy gross. Two fill
the budget. The gate sees 50,000 and approves a third, then a fourth.

The gate and the dashboard agree with *each other* and both differ from
reality — so the dashboard cannot be used to notice the discrepancy.

## Open-order capacity

`gate_open_orders` compares `open_orders < max_open_orders`. `Veska.build_plan`
emits one `PlannedOrder` per leg. At 19 live orders against a limit of 20, a
two-leg intent passes the gate and produces 21; a three-leg intent produces 22.

The correct question is `current + orders this plan will create <= limit`.
`Veska.heartbeat` does mark itself DEGRADED at `>= max_open_orders`, which
`EXECUTION_HEALTHY` reads — but that is a next-tick signal against a
same-tick overshoot, and it fires only after the limit is already reached.

## Data-age / future-time behaviour

`age = now_ms − source_data_timestamp`, accepted when `age <= max_data_age_ms`.
A timestamp in the future yields a negative age, and every negative number
satisfies that comparison. There is no lower bound: a source observation
stamped a year ahead reads as maximally fresh.

TIDAL's `_quality` refuses a book whose exchange timestamp leads local receipt
beyond `max_clock_skew_ms` (TIDAL-H3), so the state should not reach RUNE
through the ordinary pipeline. The audit records this as a **defence-in-depth**
question rather than a live exploit: the final hard boundary re-derives every
other limit for itself and does not re-derive this one.

Deadline semantics are inclusive (`now <= deadline`) and unrescuable: high
consensus, huge edge, abundant liquidity and AI commentary all leave an expired
intent rejected.

## Loss / drawdown behaviour

`gate_daily_loss` uses `max(0, −day_realized_pnl) < max_daily_loss`;
`_daily_loss` in the kill switch uses `−day_realized_pnl >= max_daily_loss`.
These are complements, so the two components agree on every value — verified
across the boundary. The same holds for drawdown.

`day_realized_pnl` accumulates `PositionState.apply`'s return value, which is
**pre-fee**; fees are tracked separately in `fees_paid`. A day of heavy fee
drag therefore does not count toward the daily-loss limit. Recorded as a
semantic fact rather than a defect, because the limit's intent is not stated
anywhere.

Drawdown depends on marks. An unmarked position is valued at average entry
cost, so an adverse move is invisible until the marking loop runs. RUNE has no
mark-freshness gate; what stands between a stale valuation and a new trade is
`MARKET_DATA_FRESH` and `SYSTEM_HEALTHY`, which fail closed when the feed those
marks come from stops.

## Error-rate behaviour

`Orchestrator._error_rate` is documented as the "rolling share of bus
deliveries that raised" and computes `errors / (delivered + errors)` over the
bus subscriptions' **lifetime** counters, which nothing decays or resets.

After 10,000 healthy deliveries, 200 consecutive failures produce a reported
rate of 0.0196 against a 0.25 threshold. After 100,000 healthy deliveries the
gate needs tens of thousands of accumulated errors before it fires. The longer
the platform has been healthy, the less a live incident moves the number —
which inverts what the gate is for.

## Kill-switch architecture

`TRIGGER_ACTIONS` defines 12 responses. `TRIGGERS` defines 9 predicates
evaluated every tick. `MANUAL` is operator-invoked through the API by design —
the endpoint passes an arbitrary trigger name through, so any mapping is
*technically* reachable by an operator who already knows to type it, which is
not detection.

That leaves **two** mappings with no predicate and no caller. External
validation reported them together; they are separate findings and are recorded
separately:

- **`RISK_LIMIT_BREACH`** (P5-3, HIGH) requests `HALT_NEW_TRADES` and
  `CANCEL_ALL` — a response no other trigger delivers for an exposure breach.
  Nothing invokes it, so no automatic path notices a live portfolio past
  `max_gross_exposure`, `max_net_exposure`, `max_venue_exposure`,
  `max_strategy_exposure`, `max_position_notional` or `max_leverage`.
- **`AGENT_FAILURE`** (P5-17, LOW) requests `(HALT_NEW_TRADES,)` — byte-identical
  to `SYSTEM_HEALTH_FAILURE`'s action set, and `SYSTEM_HEALTH_FAILURE`'s
  predicate already fires when a required agent stops being HEALTHY. A required
  agent that answers with nothing is separately caught by consensus
  completeness. So nothing is left unprotected: this is a vestigial mapping,
  **not** an exposure gap, and it is deliberately not folded into P5-3.

Drawdown, daily loss and unhedged exposure (via `UNEXPECTED_POSITION`) *are*
watched; the exposure dimensions are not. And `UNEXPECTED_POSITION` fires at
`abs(unhedged) > max_unhedged_notional * 3`, three times the gate's own limit,
so even that dimension has a band in which the entry gate refuses new trades
while nothing acts on the exposure already held — the band the external probe
landed in.

Pre-trade prevention is not equivalent to post-fill detection: partial fills,
slippage and the concurrent commitments above all produce live states that were
never projected.

`EXCESSIVE_LATENCY` compares `inputs.max_latency_ms` against
`max_data_age_ms * 5`. `Orchestrator._protect` fills that field from
`max(s.age_ms ...)` — data age, not `VenueMarketState.latency_ms`, which is the
smoothed `received_ts − exchange_ts` transport measurement. The two are
different numbers and can point in opposite directions: a feed delivering
punctually can carry a 30-second-old exchange observation, and a feed with
30 seconds of transport latency can still have `age_ms == 0` at the snapshot
instant.

`KillSwitch.evaluate` catches every predicate exception and continues. A
mandatory safety predicate that raises is therefore skipped, and the condition
it was watching goes unobserved — fail-open on the emergency boundary.

`engage()` and `clear()` read `self.clock.now_ms()` rather than taking the
tick's canonical instant. The economic *actions* do not depend on it, but the
recorded causal ordering of a safety event does.

## Recovery semantics

`RECONCILIATION_MISMATCH` and `UNEXPECTED_POSITION` set `DISABLE_EXECUTION`,
and `Orchestrator._protect` latches `veska.executor.execution_disabled = True`.
`KillSwitch.clear()` resets `KillSwitchState`.

Nothing anywhere assigns `execution_disabled = False`. Not the orchestrator,
not the kill switch, not the executor, not the API — which exposes `engage` and
no counterpart. After either trigger the platform cannot submit for the life of
the process, and a manual clear looks like it worked.

`MAX_DRAWDOWN_BREACHED` and `MAX_DAILY_LOSS_BREACHED` request `HALT_NEW_TRADES`
and `FLATTEN` but **not** `CANCEL_ALL`. `Orchestrator._flatten` visits records
in `MONITORING`, `RECONCILING` and `HEDGING` — not `EXECUTING`, which is
precisely the state whose entry orders are still live. So an opening order that
was resting when the trigger fired is neither cancelled by the action set nor
unwound by the flatten, and can fill afterwards, re-opening the exposure the
safety action just closed.

`MARKET_DATA_OUTAGE` requests `HALT_NEW_TRADES` and `CANCEL_ALL` but not
`FLATTEN`. Positions are held through the outage deliberately: closing requires
prices, and an outage is exactly when there are none. Resting orders are pulled,
nothing new opens, and the position exits through the ordinary monitoring path
once data returns. Recorded as a **design decision**.

## Exit / hedge / flatten bypass

The bypass is intentional and correctly scoped:

- Exit legs are sized `abs(position.quantity)` from a live account snapshot,
  with the side always opposing the held position; a flat position produces no
  leg, and an exit with no legs closes the record instead of trading.
- `Veska.build_plan` uses `leg.quantity` when present, so an exit is never
  re-sized from a notional.
- Hedges oppose the measured delta, are capped by `max_order_notional`, respect
  `execution_disabled`, and are not duplicated while one is in flight.
- Flatten routes everything through the exit path rather than building its own
  plan.
- The router returns `venue=leg.venue` and never assigns a side.

Exits still produce a recorded `RiskDecision` (a copy of the entry decision
with `EXIT_AUTHORISED`), and an exit with no prior decision does not trade.
Retries widen the *slippage budget*, never the quantity.

`working_notional` is taken after planning succeeds and released only on the
terminal `CLOSED`/`REJECTED` transition, which every route reaches — including
the zero-fill close and `PLANNING_FAILED`. There is exactly one release point.

## Risk-utilization consistency

Every dimension except strategy exposure is computed from the same portfolio
the gates read, in the same units. `strategy_exposure` is the exception,
described above: the dashboard reports a per-leg sum against a per-trade limit,
so a strategy holding 100,000 of real gross displays as 50,000 of a 100,000
budget.

## Replay / determinism

`RuneCore.evaluate` is a pure function of `(intent, ctx, now_ms)`. No gate
reads a clock, randomness, or the network. The only minted value is
`decision_id`, which goes through the platform's `core.ids` mechanism
(random live, deterministic under replay by design). Gate order is fixed and
independent of verdict and portfolio contents, so a replay diff can compare
positionally. Decisions round-trip through JSON with every observation and
limit intact, and the enforced limit travels on the decision, so two
configurations are distinguishable from the record alone.

## Production-like observations

`test_rune_production_behaviour.py` runs the shipped platform for 400 ticks on
the default synthetic market and prints the verdict mix, the blocking-gate
histogram, peak open orders and the worst observed value for every exposure
dimension, then asserts that no live state exceeded a configured hard limit and
that no decision authorised more than its own limits allowed.

The expectation when this was written was that the default simulation would be
too quiet to press any limit, so that a clean result would say little. **It was
not clean.** External validation (run #34) measured:

| | Observed | Limit |
| --- | --- | --- |
| `MAX_NET_EXPOSURE` | **25,103.8003** | 25,000.0 |
| `MAX_UNHEDGED_EXPOSURE` | **25,103.8003** | 10,000.0 |
| kill-switch triggers | **none** | — |
| RiskDecisions | 8, all `APPROVED` | — |

The shipped platform, on its shipped market, with no adversarial construction,
entered a live state beyond two configured hard limits and nothing stopped it.

Two details make this coherent rather than contradictory:

- **Why no trigger fired.** `UNEXPECTED_POSITION` is the only kill-switch
  predicate that watches unhedged exposure, and `Orchestrator._protect` feeds
  it `abs(unhedged) > max_unhedged_notional * 3` — 30,000, not 10,000. So
  25,103 sits inside a band where the pre-trade gate would refuse a new trade
  while nothing acts on the state already held. No exposure dimension has a
  trigger at its own limit; that is P5-3.
- **Why the pre-trade gates did not prevent it.** All 8 decisions were
  `APPROVED`, each projecting against a portfolio that had not yet moved, and
  a balanced two-leg trade projects zero net delta. The live net that resulted
  is post-fill drift — fills landing away from reference prices, and hedge
  activity — which the pre-trade projection does not claim to predict and
  which nothing re-checks afterwards.

This is direct, independent evidence for P5-1 and P5-3 from ordinary
operation rather than from a constructed scenario. The severities of both are
unchanged by it; what changes is that the need for remediation no longer rests
on an argument about reachability.

The test asserting this is preserved exactly as a strict safety invariant.

---

## Findings

Severities follow §46. Ranked most severe first.

| ID | Severity | Area | Invariant | Observed behaviour | Evidence / test | Consequence | Proposed remediation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **P5-1** | CRITICAL | Concurrent authorisation | Gross, venue, position, net and leverage limits bound the total the platform has committed to, not merely the total it has already filled | Only `MAX_STRATEGY_EXPOSURE` consults a reservation. Every other exposure gate reads `PortfolioState`, which moves on fill. `_seek` authorises each opportunity in turn with no settlement between them | `test_rune_pending_reservations.py::TestGrossExposureUnderConcurrentAuthorisation`, `…VenueExposure…`, `…PositionExposure…`, `…Leverage…` | N concurrent authorisations can commit N× a hard limit before any of them fills | Reserve committed exposure per venue / symbol / side at authorisation, release it on fill or terminal transition, and add it to every projection |
| **P5-2** | HIGH | Strategy exposure | The reservation, the gate and the dashboard use one unit | Reservation and dashboard are per-leg (`approved_notional`, once per opportunity); the gate projects the incoming intent at `notional × legs` | `test_rune_strategy_exposure.py::TestStrategyExposureCannotBeAuthorisedPastItsLimit`, `…TestTheMismatchScalesWithLegCount`, `…TestRiskUtilizationReportsTheSameUnitTheGateEnforces` | A two-leg strategy can be authorised to ~2× its configured budget; three-leg ~3×. The dashboard understates by the same factor | Store `approved_notional × len(legs)` in `working_notional`, or divide the incoming projection consistently. One unit, chosen and documented |
| **P5-3** | HIGH | Kill switch | Every configured hard limit has an automatic detection path for a live breach | `RISK_LIMIT_BREACH` exists in `TRIGGER_ACTIONS` with no predicate in `TRIGGERS` and no caller. Gross, net, venue, strategy, position and leverage are never re-checked after a fill | `test_rune_kill_switch.py::TestTriggerInventory::test_risk_limit_breach_is_reachable`, `TestNoAutomaticExposureBreachDetection`; **externally observed** in `test_rune_production_behaviour.py` (live net 25,103.80 > 25,000, unhedged 25,103.80 > 10,000, zero triggers) | A live portfolio past a hard exposure limit — reachable via P5-1, partial fills or slippage — is never detected and trading continues. `UNEXPECTED_POSITION`, the one exposure-adjacent trigger, fires only at 3x the unhedged limit, leaving a band where the gate refuses new trades while nothing acts on the state already held | Add a `_risk_limit_breach` predicate over the live portfolio and register it in `TRIGGERS` |
| **P5-4** | HIGH | Open-order capacity | After authorising a trade the platform holds at most `max_open_orders` | Gate compares only the current count; `build_plan` then creates one order per leg | `test_rune_pending_reservations.py::TestOpenOrderCapacity` | At `limit − 1`, a two-leg trade reaches `limit + 1`; a three-leg trade `limit + 2` | Gate on `open_orders + len(intent.legs) <= max_open_orders` |
| **P5-5** | HIGH | Kill switch recovery | A manual clear restores the platform to a working state | `execution_disabled` is latched `True` by the orchestrator and assigned `False` nowhere in the codebase | `test_rune_kill_switch.py::TestDisableExecutionRecovery` | After `RECONCILIATION_MISMATCH` or `UNEXPECTED_POSITION` the platform silently cannot submit for the life of the process; the clear appears to succeed | Clear the executor latch in `KillSwitch.clear()` (or add an explicit, documented operator action) |
| **P5-6** | HIGH | Kill switch | A safety action cannot be undone by state that predates it | `MAX_DRAWDOWN_BREACHED` / `MAX_DAILY_LOSS_BREACHED` request `FLATTEN` without `CANCEL_ALL`, and `_flatten` skips records in `EXECUTING` | `test_rune_kill_switch.py::TestFlattenAndLiveOrders` | An opening order live when the trigger fired can fill after the flatten, re-opening the exposure that was just closed | Add `CANCEL_ALL` to both action sets, and/or have `_flatten` cancel a record's live orders before exiting |
| **P5-7** | MEDIUM | Sizing contract | "Size the intent to fit every limit, then gate the sized intent" | `_headroom` omits `MAX_NET_EXPOSURE` and `MAX_LEVERAGE`, both strictly increasing in notional | `test_rune_headroom.py::TestNetExposureIsReducible`, `TestLeverageIsReducible` | A trade that a smaller size would have satisfied is rejected outright; the documented contract is materially false | Add both to the `_headroom` candidate list, or narrow the docstring to the limits it actually sizes for |
| **P5-8** | MEDIUM | Error rate | `MAX_ERROR_RATE` measures the current failure rate | Lifetime `errors / (delivered + errors)` over bus counters that never decay | `test_rune_error_rate.py::TestAConcentratedBurstMustBeVisible` | A total current failure is diluted below the threshold by historical success; the longer the uptime the less responsive the gate | Compute over a bounded window (time or count), or rename the limit and its docstring to the lifetime measure it is |
| **P5-9** | MEDIUM | Data age | The final hard boundary refuses implausibly fresh market data | `age <= max_data_age_ms` with no lower bound; a future timestamp reads as maximally fresh | `test_rune_data_time.py::TestFutureTimestamps` | Defence-in-depth gap. Not currently reachable through the pipeline: TIDAL rejects the skew upstream | Also require `age >= −max_clock_skew_ms` |
| **P5-10** | MEDIUM | Kill switch | `EXCESSIVE_LATENCY` consumes latency | `_protect` passes `max(s.age_ms)`; `VenueMarketState.latency_ms` exists and is not used | `test_rune_kill_switch.py::TestLatencyTriggerMeasuresLatency` | The trigger is a second, looser data-age check under a misleading name; genuine transport latency is unmonitored | Pass `latency_ms`, and keep the age check as a separately-named trigger if it is wanted |
| **P5-11** | MEDIUM | Exposure projection | Projections never understate | `gate_venue_exposure` and `gate_position_notional` take `max` over legs, so two legs sharing a venue (or venue/symbol) add once, not twice | `test_rune_exposure_projection.py::TestVenueExposureIsNeverUnderstated::test_two_legs_on_the_same_venue_are_aggregated`, `TestPositionExposureIsNeverUnderstated::test_two_legs_on_one_position_are_not_aggregated` | Unreachable with today's cross-venue strategy, which uses distinct venues; RUNE is a generic layer and nothing enforces that | Sum per-venue and per-position additions before comparing |
| **P5-12** | MEDIUM | Kill switch | A mandatory safety predicate that cannot be evaluated blocks | `evaluate` catches every exception and `continue`s, so the condition goes unobserved | `test_rune_kill_switch.py::TestTriggerExceptionsAreSwallowed` | Fail-open on the emergency boundary: a raising predicate silently stops protecting | Engage a fail-closed trigger (or `SYSTEM_HEALTH_FAILURE`) when a predicate raises |
| **P5-13** | MEDIUM | Observability | Risk utilization displays the units the gates enforce | `strategy_exposure` is reported per-leg against a per-trade limit | `test_rune_strategy_exposure.py::TestRiskUtilizationReportsTheSameUnitTheGateEnforces` | An operator watching the dashboard sees ~50% of true strategy consumption; this is also why P5-2 is invisible in operation | Follows from P5-2 — one unit end to end |
| **P5-14** | LOW | Configuration | A configured limit is a limit | `RiskLimits` fields are `gt=0` without `allow_inf_nan=False`, so `+inf` is accepted and silently disables the gate that reads it | `test_rune_loss_boundaries.py::TestNumericSafety::test_an_infinite_limit_is_not_a_limit` | An infinite limit passes every comparison. `TidalConfig` and `NoroConfig` already set `allow_inf_nan=False`, so the convention exists | Add `allow_inf_nan=False` to every `RiskLimits` float |
| **P5-15** | LOW | Configuration | Incoherent configurations are refused | The validator checks `min_trade ≤ max_order ≤ max_position ≤ max_gross` but not `max_order` against `max_venue_exposure`, nor `max_strategy_exposure` against `min_trade_notional × legs` | `test_rune_loss_boundaries.py::TestConfigCoherence` | A per-order cap larger than any venue may hold, or a strategy budget below the minimum trade, both load without warning and make the platform quietly untradeable | Extend `_limits_are_coherent` |
| **P5-16** | LOW | Determinism | Nothing on the emergency path depends on a live clock read | `KillSwitch.engage()` / `clear()` call `self.clock.now_ms()` rather than accepting the tick instant | `test_rune_kill_switch.py::TestKillSwitchStateSemantics::test_the_switch_reads_a_live_clock_for_its_timestamps` | The economic actions are unaffected; the recorded causal ordering of a safety event can differ between a run and its replay | Thread `now_ms` through, as the fast loop already does |
| **P5-17** | LOW | Kill switch | An action mapping names a response some trigger can deliver | `AGENT_FAILURE` has actions defined, no predicate in `TRIGGERS`, and no caller. Its action set is `(HALT_NEW_TRADES,)` — byte-identical to `SYSTEM_HEALTH_FAILURE`'s, whose predicate already covers a required agent going unhealthy | `test_rune_kill_switch.py::TestTriggerInventory::test_agent_failure_is_not_a_vestigial_mapping` | **Not an exposure gap and explicitly not part of P5-3.** Nothing is unprotected: a failing required agent is caught by `SYSTEM_HEALTH_FAILURE`, and a required agent that answers with nothing is caught by consensus completeness. The mapping is vestigial, and a dead entry in a safety table invites someone to assume a response exists that no code path delivers | Either give it a predicate that means something `SYSTEM_HEALTH_FAILURE` does not, or delete the mapping. Do not add a trigger without deciding what it should mean |
| **P5-18** | HIGH | Pre-trade leg risk | An authorised multi-leg intent must not have a known execution sequence that necessarily crosses `max_unhedged_notional` | `gate_unhedged` compares only `ctx.unhedged_notional` — the residual already present in the FILLED book — and `_headroom` has no candidate for it at all, so the gate was classified as a pure current-state check. An intent's worst INTERMEDIATE residual scales with its notional: the ordinary two-leg delta-neutral trade holds one whole leg of one-sided exposure between its first fill and its second | `test_rune_leg_fill_risk.py`, `test_rune_committed_exposure.py::TestPendingUnhedgedFillRisk`, `test_rune_headroom.py::TestTheSizeSensitiveInventory` | With the shipped defaults (`max_order_notional` 25,000, `max_unhedged_notional` 10,000) RUNE authorises 2.5× the hard unhedged budget on every ordinary trade. CI #38 shows `RISK_LIMIT_BREACH` engaging during normal operation at ≈ START_MS + 15,200ms and latching, as designed; four unrelated long-run tests then halted because the emergency latch had correctly engaged, and the P5-3 breach-to-response probe PASSED — proving the backstop was reacting to a real state rather than to a test artifact. The backstop is correct; the pre-trade projection is the incomplete half | Project worst-case asynchronous fill risk — actual residual + pending entry fill risk + this intent at `notional × per-symbol worst side × slippage multiplier` — in both the gate and a matching `_headroom` candidate, so the entry is SIZED to fit the configured budget rather than authorised and caught afterwards |

### Expected conservative behaviour (not defects)

- Gross, net, position and leverage projections treat an exposure-*reducing*
  leg as though it opened a new position. An entry leg carries no quantity, so
  RUNE cannot know otherwise. Overstatement blocks a safe trade; it never
  authorises an unsafe one.
- `MAX_LEVERAGE` uses pre-trade equity as the denominator.
- The `/ legs` division in `_headroom` makes the largest authorisable per-leg
  size half the gross/strategy budget for a two-leg strategy.
- Exits, hedges and flatten bypass `MIN_EXPECTED_EDGE` and consensus.

### Refuted hypotheses

- RUNE and the kill switch **agree** on the daily-loss and drawdown boundaries
  at every value, including exactly at the limit.
- RUNE-AI cannot affect the verdict, the approved notional, any gate result,
  observation or limit, or the deterministic reason codes — under maximum
  concern, zero concern, malformed output, provider outage and a missing
  provider. It also never calls the provider on the decision path.
- Every gate is mandatory; `UNKNOWN` blocks in all five reachable cases.
- Reason codes name exactly the blocking gates — not a superset, not a subset.
- Gates do not mutate their inputs, and gate order does not affect the verdict.
- NaN edge, NaN consensus, NaN error rate and NaN unhedged notional all block.
- `_headroom` never increases a request, across ten magnitudes and five
  binding-limit combinations.
- Exits, hedges and flatten are exposure-reducing by construction; there is
  exactly one `working_notional` release point and it is terminal.

### Untested / blocked

- Live venue behaviour: the paper boundary is intact and this audit does not
  cross it.
- Partial-fill sequences beyond what `PositionState.apply` models. RUNE is not
  asked to predict fill uncertainty; the audit establishes only what its
  *modelled* projection guarantees.
- Performance (§45): the algorithmic shape is linear in positions × legs
  (`exposure_by_venue` is one pass, each gate one pass over legs), with no
  quadratic structure. No timing was measured, per the no-benchmark rule.

---

## Hypothesis verdicts

| | Hypothesis | Verdict |
| --- | --- | --- |
| **H1** | `_headroom` includes every safely reducible size constraint | **FALSE** — `MAX_NET_EXPOSURE` and `MAX_LEVERAGE` omitted (P5-7) |
| **H2** | Strategy exposure uses consistent units across reservation, gate and utilization | **FALSE** — per-leg reservation, per-trade projection (P5-2, P5-13) |
| **H3** | Authorised-but-unfilled trades reserve enough headroom to prevent concurrent over-allocation | **FALSE** — only strategy exposure reserves anything (P5-1) |
| **H4** | Open-order risk accounts for the orders the new intent will create | **FALSE** — current count only (P5-4) |
| **H5** | Gross / venue / position / net / leverage projections never understate | **PARTIALLY FALSE** — gross, net and leverage hold; venue and position understate when legs share a venue (P5-11) |
| **H6** | RUNE rejects implausibly future timestamps, or upstream makes it impossible | **UPSTREAM ONLY** — TIDAL blocks it; RUNE has no bound of its own (P5-9) |
| **H7** | `MAX_ERROR_RATE` measures the intended horizon | **FALSE** — lifetime, documented as rolling (P5-8) |
| **H8** | All hard-limit breaches have an automatic post-trade detection path | **FALSE** — six exposure limits unwatched; `RISK_LIMIT_BREACH` unreachable (P5-3) |
| **H9** | `EXCESSIVE_LATENCY` consumes latency | **FALSE** — consumes data age (P5-10) |
| **H10** | Manual clear has a complete recovery path after `DISABLE_EXECUTION` | **FALSE** — executor latch never cleared (P5-5) |
| **H11** | Flatten cannot be undone by previously-live opening orders | **FALSE** — no `CANCEL_ALL`, and `EXECUTING` is skipped (P5-6) |
| **H12** | Risk-reducing exits / hedges / flatten cannot increase exposure | **TRUE** |
| **H13** | RUNE-AI cannot affect verdict, size or gate results | **TRUE** |
| **H14** | Risk utilization displays the units the gates enforce | **FALSE** for strategy exposure; **TRUE** for every other dimension (P5-13) |
| **H15** | Replay reproduces approval, reduction, rejection and safety actions | **TRUE** for RUNE decisions; safety-event *timestamps* depend on a live clock read (P5-16) |

---

## Remediation dependency order

**DO NOT IMPLEMENT THE REMEDIATION.** Recorded so a later pass can be
sequenced; nothing here is done in Phase 5.

1. **P5-2** (strategy-exposure units). Smallest change, and it settles the unit
   question every later reservation change has to agree with. **P5-13** falls
   out of it.
2. **P5-7** (headroom completeness). Independent, local to `_headroom`, and it
   makes the sizing contract true before anything starts relying on it.
3. **P5-4** (open-order capacity). Independent and local to one gate.
4. **P5-1** (pending reservation). The largest change: it needs a reservation
   ledger with a lifecycle, and it must use the unit fixed in step 1. Every
   exposure projection then reads reserved + filled.
5. **P5-3** (post-fill breach detection). Best done after P5-1, so the trigger
   is a genuine backstop rather than the primary control. The external probe
   shows the shipped system already reaching such a state, so this cannot be
   deferred indefinitely on the argument that P5-1 makes it unreachable.
   Deciding `RISK_LIMIT_BREACH`'s semantics here is also the natural moment to
   settle **P5-17**, since both are questions about what the trigger table is
   supposed to contain.
6. **P5-5** and **P5-6** (kill-switch recovery and flatten/cancel ordering).
   Independent of the exposure work and of each other.
7. **P5-12** (fail-closed trigger exceptions), **P5-10** (latency input),
   **P5-9** (future-timestamp bound), **P5-16** (kill-switch logical time).
8. **P5-14**, **P5-15** (configuration validation). Last, because tightening
   validation against limits whose semantics are still moving would have to be
   redone.

**P5-11** can be taken with P5-1 (both change how leg contributions aggregate)
or separately; it is unreachable with the shipped strategy either way.

---

## Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED**

No `pytest`, `ruff`, `mypy`, container, replay or application start was
executed in this pass. Every finding above was derived by reading the
implementation and is expressed as an executable assertion for the external
validator to run.

The Phase 5 audit tests are **expected to fail** where a finding exists. None
is marked `skip` or `xfail`: the unresolved risk findings are the deliverable.
Tests that pass are the refuted hypotheses, and they are equally part of the
result.
