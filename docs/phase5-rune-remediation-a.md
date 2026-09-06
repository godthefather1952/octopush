# Phase 5 Remediation A — RUNE risk-math primitives

Five deterministic arithmetic defects, fixed. No new mechanism, no new state,
no calibration change — the limits are the same numbers they were, and this
pass makes RUNE compute them correctly.

- **Base SHA:** `bb08226bd9b449a340b580ba48aceb1425a282fa`
- **Branch:** `phase5-rune-remediation-a`
- **Production files changed:** `agents/rune/core.py`,
  `risk/limits/__init__.py`, `apps/orchestrator/orchestrator.py`. Paper trading
  only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Audit baseline

The cleaned Phase 5 audit validated at `bb08226`:

| | |
| --- | --- |
| Full suite | 2713 passed / **40 failed** / 2 skipped |
| Python 3.11 unit + contract | 1381 passed / 126 skipped / **0 failed** |
| Ruff, Mypy core, paper boundary, Redis + PostgreSQL contract pre-check | PASS |

The 40 failures are the audit's findings, asserted as safety invariants. This
pass addresses five of them.

## 2. Scope

**Fixed:** P5-2 (strategy-exposure unit mismatch), P5-13 (risk-utilization
understatement), P5-7 (`_headroom` omits `MAX_NET_EXPOSURE` and
`MAX_LEVERAGE`), P5-4 (open-order gate ignores the orders the intent creates),
P5-11 (venue/position projections undercount duplicate legs).

These are the deterministic local primitives the future P5-1 reservation
ledger has to build on. Fixing them first means that ledger is added to
arithmetic that is already correct, rather than layered over arithmetic that
is not.

**Deliberately untouched:** P5-1, P5-3, P5-5, P5-6, P5-8, P5-9, P5-10, P5-12,
P5-14, P5-15, P5-16, P5-17. Their audit tests still expose them.

## 3. Canonical per-leg notional

One definition, now stated in `RiskContext`, in `risk.limits`, and on the
orchestrator's reservation map:

> `TradeIntent.notional` and `RiskDecision.approved_notional` are the **per-leg**
> quote notional.

`Veska.build_plan` sizes every leg as `notional / expected_price`, so an N-leg
intent puts `notional` on each of N venues. `gate_gross_exposure` already
projected `notional * len(legs)`, so the unit was never in doubt — it simply
was not applied consistently.

## 4. Strategy gross exposure

For an N-leg opportunity:

```
strategy gross exposure contribution = approved_notional * N
```

`max_strategy_exposure` is a **gross** budget. With the shipped 100,000 budget
and the 25,000 per-order cap, one two-leg trade consumes 50,000 and two fill
the budget exactly.

## 5. Working strategy reservation (P5-2)

`Orchestrator.working_notional` now stores the gross contribution:

```python
self.working_notional[opportunity.opportunity_id] = (
    decision.approved_notional * len(intent.legs)
)
```

Previously it stored the bare per-leg `approved_notional`, so
`gate_strategy_exposure` compared a per-leg *sum* against a per-trade
*projection* — the incoming intent counted at `notional x legs`, every
already-working one at `notional x 1`. A two-leg strategy could be authorised
to roughly twice its budget, a three-leg one to roughly three times.

The map keeps its name: renaming it would have touched the release path, the
lifecycle tests and the attribution trail for no behavioural gain. Its
docstring now states the unit, and it is read only through
`_current_strategy_exposure()`.

**Lifecycle unchanged.** The reservation is still taken only after risk
approval *and* successful planning, still held for the whole working life of
the trade, and still released only at the terminal `CLOSED` / `REJECTED`
transition. No second release path, nothing released on fill or while
`MONITORING`, no detector change.

## 6. Utilization (P5-13)

`_risk_check` and `_refresh_risk_utilization` both call
`_current_strategy_exposure()`. The gate and the dashboard cannot report
different numbers for the same quantity, because there is only one expression.

Two working two-leg trades at the cap now display as 100,000 of a 100,000
budget rather than 50,000 — which matters because the dashboard was the one
place an operator could have noticed P5-2, and it was understating by exactly
the factor that hid it.

## 7. Net-exposure headroom (P5-7)

`gate_net_exposure` computes `abs(current + coefficient * n) <= limit`, where
`coefficient = sum(leg.side.sign)`. That is linear in `n`, so the feasible set
is an interval and its upper bound is solved directly:

```
roots  = sorted(((-limit - current) / coefficient, (limit - current) / coefficient))
upper  = roots[1]
result = max(0.0, upper)
```

`coefficient == 0` — a balanced two-leg trade — returns `inf`: no size changes
the projection, so this constraint does not bind on `n` at all. That is not a
bypass. If `abs(current)` alone already breaches, the gate still rejects and no
reduction could have helped.

The naive `limit - abs(current)` would have been wrong because **direction
matters**. Against +8,000 held with a 10,000 limit, a BUY has 2,000 of room and
a SELL has 18,000; the naive form gives both 2,000. The solver handles a
positive or negative current position, a trade increasing or reducing exposure,
and a reducing trade crossing through zero into the permitted band on the other
side.

Never negative, and never enlarging: the caller takes a `min` over every
candidate *including the requested notional*. Where only a larger risk-reducing
trade would re-enter the band, RUNE leaves the request alone and the gate
rejects it.

## 8. Leverage headroom (P5-7)

`gate_leverage` computes `(gross + n * legs) / equity <= max_leverage`. Every
term but `n` is fixed at decision time, so:

```
result = max(0.0, (max_leverage * equity - gross) / legs)
```

Non-positive equity yields zero, and `gate_leverage` remains the fail-closed
authority for that case — its behaviour is unchanged.

## 9. Venue grouping (P5-11)

Both the gate and the sizing path now group legs by venue before projecting:

```
projected(venue) = existing venue exposure + notional * legs_on_that_venue
candidate(venue) = (max_venue_exposure - existing) / legs_on_that_venue
```

`max` over ungrouped legs answered "what is the largest single leg's effect",
which is not the question a venue limit asks. Two legs routed to one venue put
`2 * notional` on it.

Appending one full remaining amount per leg in `_headroom` would have described
a different model from the gate judging the result, so both use the same
`legs_per_venue` helper.

## 10. Position grouping (P5-11)

The same, keyed on `venue:symbol`:

```
projected(key) = current position notional + notional * legs_on_that_key
candidate(key) = (max_position_notional - current) / legs_on_that_key
```

**Deliberately conservative with opposing legs.** A BUY and a SELL on one
position are added, not netted. RUNE has no guaranteed fill sequence or per-leg
quantity for a generic multi-leg intent, so it cannot know the two would
offset — and overstating a position blocks a safe trade, while understating one
authorises an unsafe trade.

## 11. Open-order capacity (P5-4)

```python
gate_open_orders(open_orders, incoming_orders, limits)
projected = open_orders + incoming_orders
pass      = projected <= max_open_orders
```

`RuneCore` passes `len(intent.legs)`. The old `open_orders < max_open_orders`
let 19 live orders admit a two-leg trade and reach 21.

`observed` is now the **projected** count, so a rejection reads "21 against a
limit of 20" rather than "19 against a limit of 20", which explained nothing.
`detail` carries `"19 live + 2 incoming = 21"`.

This gate has **no headroom candidate**, deliberately: an intent creates one
order per leg regardless of its notional, so no reduction can make it pass and
rejection is the only correct answer. That absence is pinned by a test so it
stays a decision rather than an oversight.

## 12. Headroom now mirrors every reducible gate

| Gate | Headroom candidate |
| --- | --- |
| `MAX_ORDER_NOTIONAL` | `max_order_notional` |
| `MAX_POSITION_NOTIONAL` | per position group |
| `MAX_GROSS_EXPOSURE` | `(limit - gross) / legs` |
| `MAX_NET_EXPOSURE` | `net_exposure_headroom` **(new)** |
| `MAX_LEVERAGE` | `leverage_headroom` **(new)** |
| `MAX_VENUE_EXPOSURE` | per venue group |
| `MAX_STRATEGY_EXPOSURE` | `(limit - working) / legs` |
| `LIQUIDITY_SUFFICIENT` | `max_economical_notional` |
| `MAX_OPEN_ORDERS` | **none, by design** |

`RuneCore.evaluate`'s documented contract — "a size-based gate can only fail if
the reduction could not make it pass" — is now true.

## 13. Boundary semantics

Unchanged where they were already right, and pinned from both sides in
`tests/audit/test_rune_remediation_a_boundaries.py`:

| Constraint | Passes | Fails |
| --- | --- | --- |
| strategy exposure | projected `== limit` | projected `> limit` (reduced, or rejected at zero headroom) |
| net exposure | `abs(projected) == limit` | above it (reduced to the bound) |
| leverage | projected `== max_leverage` | above it (reduced to the bound) |
| open orders | `18 + 2 == 20` | `19 + 2 == 21` |
| duplicate venue | `2 legs x 10,000 == 20,000` limit | above it (reduced to 10,000 per leg) |
| duplicate position | `2 legs x 10,000 == 20,000` limit | above it |

## 14. Findings intentionally deferred

Untouched, with their audit tests still failing as designed:

| ID | |
| --- | --- |
| **P5-1** | concurrent committed-exposure reservation |
| **P5-3** | automatic live hard-limit breach detection |
| **P5-5** | `execution_disabled` recovery |
| **P5-6** | flatten / resting-order race |
| **P5-8** | lifetime vs rolling error rate |
| **P5-9** | future-timestamp defence in depth |
| **P5-10** | latency-vs-age kill-switch input |
| **P5-12** | kill-switch predicate exception fail-open |
| **P5-14** | `+Infinity` limit validation |
| **P5-15** | additional config coherence |
| **P5-16** | kill-switch logical-time stamps |
| **P5-17** | vestigial `AGENT_FAILURE` mapping |

No file under `risk/kill_switch/` was touched. `RISK_LIMIT_BREACH` remains
unreachable. `UNEXPECTED_POSITION`, `FLATTEN`, `CANCEL_ALL`,
`DISABLE_EXECUTION` and the manual clear are unchanged.

## 15. The production-like live breach is NOT resolved

External validation observed, on the shipped simulation:

```
MAX_NET_EXPOSURE      25,103.8003 > 25,000
MAX_UNHEDGED_EXPOSURE 25,103.8003 > 10,000
kill_switch_triggers  []
RiskDecisions         8, all APPROVED
```

**This pass does not fix that**, and the probe asserting it is untouched and
still strict.

That state is post-fill drift — fills landing away from reference prices, plus
hedge activity — against a portfolio that had not moved when each decision was
taken. Reaching it needs P5-1 (reserve committed exposure so concurrent
authorisations cannot over-allocate) and detecting it needs P5-3 (an automatic
trigger when a live portfolio passes a hard limit). Neither is in scope here.

What this pass does change is that the arithmetic those two remediations will
build on is now correct: a reservation ledger added on top of a strategy budget
counted in two different units would have inherited the inconsistency.

## 16. Determinism, and what did not change

Every new calculation is a pure function of `(TradeIntent, PortfolioState,
RiskLimits)`. No clock read, no randomness, no network, no LLM. The same
inputs at the same `now_ms` produce the same verdict, approved notional and
gate results. No replay-framework change.

Unchanged: every `RiskLimits` default; the gate list, its order and its
`__all__`; NORO abstention; the TIDAL deadband; ZEPHR's cost model; consensus
weights and the 0.60 / 0.45 thresholds; `PaperExecutor` and the paper boundary;
the simulation.

## 17. Tests

Added `tests/audit/test_rune_remediation_a_boundaries.py`: exact boundaries for
all five repaired primitives, the direction-sensitivity of net headroom, the
leg-count scaling of leverage headroom, the grouping helpers themselves, and a
`TestNothingElseMoved` guard covering the default limits, the gate list, the
ordinary two-leg approval and determinism.

Existing audit tests were updated **only** where the interface they read
changed — never to match an implementation:

- Assertions that pinned production's *source text* as the premise of a defect
  (the reservation's unit, `_headroom`'s omissions, the open-order gate's
  ungrouped comparison) now pin the corrected text. Their safety assertions are
  untouched.
- Harnesses that *modelled* the orchestrator's reservation now store gross, as
  production does. The invariants they assert — "authorised strategy exposure
  never exceeds the limit" — are character-for-character the same.
- `TestLeverageIsReducible`'s premise test asserted a rejection attributable to
  leverage. There is no longer a rejection, so it now asserts what leverage
  binds instead: the sized trade lands exactly on the limit.

No test was skipped, xfailed or deleted, and no assertion was weakened.

## 18. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

No `pytest`, `ruff`, `mypy`, container, replay or application start was
executed. Every expectation here was derived by reading the implementation.

What external validation should watch:

1. Whether the five finding groups turn green, and whether any *other* audit
   test changed state — particularly the production-like probe, which should
   still fail on net and unhedged exposure.
2. Whether the shipped simulation still trades. The strategy-exposure unit fix
   makes that budget bind roughly `leg count` times sooner than before, which
   is the correction working, but it is the change most likely to alter how
   many trades a 400-tick run produces.
3. The 3.11 unit + contract job, which should stay at 0 failed: nothing outside
   `tests/audit/` was modified.
