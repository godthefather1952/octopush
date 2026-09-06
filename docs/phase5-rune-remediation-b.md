# Phase 5 Remediation B — committed exposure accounting

One finding, fixed: **P5-1 — concurrent committed exposure is invisible before
fills.** Between the moment RUNE authorises a trade and the moment its first
fill lands, the exposure that trade has committed the platform to was visible
to no exposure gate. This pass makes it visible, as a value derived from
authoritative order state rather than as a second ledger.

- **Base SHA:** `2e5c992b268a29e03dfc5de4f94704c98ec03ffe`
- **Branch:** `phase5-rune-remediation-b`
- **Production files changed:** `core/models/risk.py`, `core/models/__init__.py`,
  `risk/limits/__init__.py`, `agents/rune/core.py`,
  `apps/orchestrator/orchestrator.py`. Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Baseline

Remediation A validated at `2e5c992`, on branch `phase5-rune-remediation-a`,
clean tree. `phase5-rune-remediation-b` was created directly from that commit —
no merge, no rebase, no tag, no force-push.

## 2. Scope

Remediated:

| ID | |
| --- | --- |
| **P5-1** | concurrent committed exposure is invisible before fills |

Also corrected: three stale audit assertions left behind by Remediation A's
MAX_LEVERAGE headroom solver (§16).

Nothing else. No kill-switch change, no threshold change, no change to NORO,
TIDAL, ZEPHR, consensus, the market simulation, the cost model, the fill
simulator, VESKA planning or PaperExecutor timing.

## 3. The defect

`RiskContext.portfolio` is a `PortfolioState`, and a portfolio only moves when
a fill lands. Five gates read it and nothing else:

- `MAX_GROSS_EXPOSURE`
- `MAX_NET_EXPOSURE`
- `MAX_LEVERAGE`
- `MAX_VENUE_EXPOSURE`
- `MAX_POSITION_NOTIONAL`

`Orchestrator._seek` detects, decides and executes each opportunity in one
pass, with only a `bus.drain()` between them and no settlement. So two
opportunities authorised in a single tick each read the same unmoved book. Each
one is individually inside every limit; together they are not. A limit that
holds only until the second concurrent trade is not a hard limit.

`Orchestrator.working_notional` was the platform's only reservation, and it fed
exactly one gate — `MAX_STRATEGY_EXPOSURE`. That is why the strategy budget was
the only dimension the window did not open under.

## 4. Architecture: derived, not a ledger

The reservation is computed from `SystemState.orders` every time it is asked
for, by `Orchestrator._current_committed_exposure()`.

The rejected alternative is a mutable ledger that has to be independently
incremented on submit, decremented on partial fill, released on cancel,
released on reject, released on expiry, preserved on UNKNOWN, and reconciled
against the OMS. That is a second copy of the order lifecycle, kept in step
with the first by hand, and it is wrong the first time any one path forgets to
release. A snapshot recomputed from the orders themselves has nothing to drift
from: release is a consequence of the order's state, not of a call the platform
has to remember to make.

`SystemState._trim_orders` keeps every live order and every order a retained
opportunity record names, so the derivation cannot lose a working commitment to
history trimming (§18 records the one residual gap).

## 5. The value model

`CommittedExposure` (`core/models/risk.py`), exported from `core.models`:

| field | |
| --- | --- |
| `gross_exposure` | unsigned quote notional still working, summed over every reserved order |
| `net_exposure` | the same amount signed by side, `+BUY` / `-SELL` |
| `venue_exposure` | venue → unsigned committed notional |
| `position_exposure` | `"venue:symbol"` → unsigned committed notional |

Units are quote notional already summed across legs — the same unit as
`PortfolioState.gross_exposure`, so the two add directly. `position_exposure`
is keyed exactly as `PortfolioState.positions` and `legs_per_position` key, so
a reservation and a held position on the same leg compose rather than sitting
in two different buckets.

`is_zero` exists only so gate `detail` strings stay quiet when there is nothing
to explain.

## 6. Which orders count

Everything that is **not terminal**: `CREATED`, `SUBMITTING`, `ACKNOWLEDGED`,
`OPEN`, `PARTIALLY_FILLED`, `CANCEL_PENDING` and **`UNKNOWN`**. Only `FILLED`,
`CANCELLED`, `REJECTED` and `EXPIRED` release.

`PaperOrder.is_live` was **not** used, because it excludes `UNKNOWN`. `UNKNOWN`
is a real state meaning "the venue-side truth is not known" — explicitly not a
failure, per `core/models/execution.py`. An `UNKNOWN` order may be resting on
the venue and may fill at any moment, so releasing its reservation would free
budget against risk the platform still carries.

## 7. How much each order reserves

```
remaining_quantity = max(0, quantity - filled_quantity)
reserved           = remaining_quantity * expected_price
```

`Veska.build_plan` sizes an entry leg as `approved_notional / expected_price`,
so for an untouched order this reconstructs exactly the per-leg
`approved_notional` RUNE authorised. As the order fills, the remaining quantity
shrinks and the reservation hands over to the position it has become — the two
never double-count, and a fully filled order releases entirely.

It is deliberately **not** built from executed fill prices and quantities.
Those measure what already filled, which the portfolio has recorded anyway, and
say nothing at all about an order that has not filled once — which is precisely
the case this exists for.

## 8. Entry versus exit and hedge

Exits and hedges **reduce** exposure. Counting them as new commitments would
make the platform's own risk reduction look like risk taking, and could block
the trade that closes a breach.

An order is an entry commitment when its `intent_id` is the entry intent of a
known opportunity record. `Orchestrator._decide` is the only place that sets
`record.intent`; `_submit_exit` and `_hedge` each build their own `TradeIntent`
with `is_exit=True` and never store it on a record, so their orders are never
entry intents.

## 9. The fail-closed fallback

Classification is positive on both sides, in this order:

1. `intent_id` is a known entry intent → **reserve**.
2. otherwise, the order id is still referenced by an opportunity record's
   `order_ids` or by `working_hedges` → positively identified as an
   exit or hedge → **skip**.
3. otherwise → **reserve**, as though it were an entry.

Case 3 over-reserves and blocks; skipping would under-reserve and authorise.
Only one of those is a safe default for a hard limit. It is reachable — an
opportunity whose record has aged out of the bounded history, or an `UNKNOWN`
hedge superseded in `working_hedges` — and it is state-dependent, never
time-dependent: nothing in the derivation consults a clock or an order's age,
so the same set of orders always produces the same snapshot. Fail-closed is not
fail-forever: an unclassifiable order still releases the moment it is terminal.

## 10. Where it is carried

`RiskContext.committed_exposure: CommittedExposure`, defaulting to an empty
snapshot so every existing caller behaves exactly as it did. `_risk_check`
fills it from `self._current_committed_exposure()`.

## 11. Gates extended

Each takes a keyword-only `committed: CommittedExposure | None = None`,
defaulting to zero.

| gate | base |
| --- | --- |
| `MAX_GROSS_EXPOSURE` | `portfolio.gross_exposure + committed.gross_exposure` |
| `MAX_NET_EXPOSURE` | `portfolio.net_exposure + committed.net_exposure` (signed) |
| `MAX_LEVERAGE` | numerator gains `committed.gross_exposure`; equity unchanged |
| `MAX_VENUE_EXPOSURE` | per venue: held + `committed.venue_exposure[venue]` |
| `MAX_POSITION_NOTIONAL` | per position: held + `committed.position_exposure[key]` |

Equity is deliberately left alone in the leverage gate: an unfilled order has
paid no fee and taken no mark, so committed exposure belongs in the numerator
only.

When anything is committed, each of these gates now carries a `detail` reading
`"<filled> filled + <committed> committed"`, so a rejection explains a base
that is not just the settled book. With nothing committed the detail is empty
and the gates read exactly as before.

## 12. Headroom extended to match

`RuneCore.evaluate` promises that a size-based gate "can only fail if the
reduction could not make it pass". That holds only while sizing and gating use
the *same* base. Every committed term added to a gate is added to its headroom
candidate:

- gross: `(max_gross_exposure - (filled + committed)) / legs`
- net: `net_exposure_headroom(..., committed=...)` — `current` becomes
  filled + committed net
- leverage: `leverage_headroom(..., committed=...)` — `gross` becomes
  filled + committed gross
- venue: per group, `(max_venue_exposure - (held + committed)) / leg_count`
- position: per group, `(max_position_notional - (held + committed)) / leg_count`

The visible consequence is that a second concurrent trade is **reduced** to
what remains rather than refused outright, and a third — with nothing left — is
refused early by the `MIN_TRADE_NOTIONAL` short-circuit.

## 13. Strategy exposure is deliberately excluded

`MAX_STRATEGY_EXPOSURE` takes no committed argument, in the gate or in the
headroom candidate.

`Orchestrator.working_notional` is written at authorisation and released only
at `CLOSED`/`REJECTED`, so it **already** covers the authorised-but-unfilled
window that `CommittedExposure` exists to close for the other five gates.
Adding committed exposure on top would count the same trade twice and roughly
halve the effective strategy budget — silently undoing the P5-2 fix rather than
extending it. P5-2 is preserved exactly: `working_notional` still stores
`approved_notional * len(intent.legs)`, still read only through
`_current_strategy_exposure()`, still released at exactly one point.

## 14. Unhedged exposure is deliberately excluded

`MAX_UNHEDGED_EXPOSURE` is unchanged. `unhedged_notional` is a *measurement* of
residual delta OKAPI has observed in the filled book, not a projection. An
order that has not filled has left no residual to hedge, and feeding committed
exposure into it would manufacture a pre-trade unhedged number out of orders
that may yet cancel.

**P5-1 does not solve the observed `MAX_UNHEDGED_EXPOSURE` breach.** That is a
different finding with a different fix (§18).

## 15. Utilization

`RuneCore.utilization` takes an optional `committed` argument, defaulting to
nothing reserved. When supplied, `gross_exposure`, `net_exposure` and
`venue_exposure` are reported as **filled + committed** — the same base the
gates judge against, because a dashboard showing only settled exposure would
read comfortably under its limit at the exact moment the platform was fully
committed against it. `RiskUtilization` also gains
`committed_gross_exposure` / `committed_net_exposure`, reported and never
subtracted, so a number that moved because an order was submitted is
distinguishable from one that moved because a fill landed.

Both call sites pass it — `_refresh_risk_utilization` (every tick) and
`_risk_check` (per evaluation) — through the one canonical method, so the
P5-13 single-formula property is preserved.

## 16. Three stale audit assertions

`TestLeverageIsNeverUnderstated::test_non_positive_equity_always_blocks`
(parametrised `0.0`, `-1.0`, `-50000.0`) called `RuneCore.evaluate` and then
looked up the `MAX_LEVERAGE` gate on the decision. Since Remediation A gave
`MAX_LEVERAGE` a headroom solver, non-positive equity yields zero headroom and
`evaluate` short-circuits on `MIN_TRADE_NOTIONAL` before the twenty-one-gate
list runs — so there is no `MAX_LEVERAGE` gate on that decision to look up.

Split in two, per the brief, with no production change to restore the old
shape:

- `test_the_leverage_gate_itself_fails_on_non_positive_equity` calls
  `gates.gate_leverage(...)` directly and asserts it blocks with detail
  `"non-positive equity"`. The gate is unchanged and still fail-closed.
- `test_non_positive_equity_always_blocks` keeps the safety property and
  asserts what `evaluate` now returns: `REJECTED`, `approved_notional == 0`,
  `reason_codes == ["MIN_TRADE_NOTIONAL"]`.

## 17. Tests

**New:** `tests/audit/test_rune_committed_exposure.py` — the derivation itself,
called against a stub carrying the only two attributes it reads, so each
scenario is the orders it is about:

- every terminal status releases; every unresolved status reserves
- `UNKNOWN` reserves even though it is not live
- a fresh order reserves the authorised per-leg notional
- partial fills shrink the reservation; over-fill floors at zero, never a credit
- net is signed — a balanced pair in flight nets to zero, two one-sided orders stack
- venue and position keys match how the portfolio keys them
- exits and hedges are not commitments; an entry beside an exit reserves only itself
- the fallback reserves an untracked or intent-less order, and still releases on terminality
- the snapshot is idempotent and follows order state

**Updated:** `tests/audit/test_rune_pending_reservations.py`

- the harness carries a `CommittedExposure` snapshot forward between
  authorisations, mirroring the derivation from its own definition rather than
  calling production
- `reserve_committed=False` reproduces the pre-remediation platform, so three
  "the finding, kept executable" tests still assert the breach the audit found
- premise tests updated where the premise inverted:
  `test_no_exposure_gate_receives_a_pending_reservation` becomes
  `test_the_exposure_gates_now_receive_a_pending_reservation`; the strategy
  wiring assertion is unchanged; a new test asserts the snapshot is derived
  from order state and uses neither `is_live` nor executed fill prices
- `test_the_gate_reports_a_projection_that_ignores_the_earlier_trade` becomes
  `test_the_third_trade_finds_the_budget_already_committed`, recording that the
  third decision is now refused early rather than projecting only its own legs
- new: `TestNetExposureUnderConcurrentAuthorisation` — a one-leg BUY of 25,000
  approved and unfilled, then another against `max_net_exposure = 30,000`,
  which must be reduced to the remaining 5,000 or rejected; plus the opposing
  and balanced cases that prove the reservation is signed rather than a
  magnitude
- new: same-venue and same-position concurrency — the second is reduced to what
  remains, the third finds it full, two legs on one venue consume it twice
  (P5-11 composing with P5-1), and a held position plus a committed one add up

**Safety assertions were not weakened anywhere.** The invariant tests —
authorised gross, venue, position, leverage and net never exceeding their
limits under concurrent authorisation — are unchanged; they are what this pass
is meant to make pass.

## 18. What is NOT resolved

Still open, with their audit tests still failing as designed: **P5-3, P5-5,
P5-6, P5-8, P5-9, P5-10, P5-12, P5-14, P5-15, P5-16, P5-17.**

- **The observed live breach is not claimed as fixed.** External validation
  observed `MAX_NET_EXPOSURE 25,103.8003 > 25,000` and
  `MAX_UNHEDGED_EXPOSURE 25,103.8003 > 10,000` on the shipped simulation.
  P5-1 addresses the *pre-trade* window; whether the shipped run still breaches
  is a question for the external validator, and
  `tests/audit/test_rune_production_behaviour.py` is untouched and still
  strict. **It is not expected to be green yet.**
- No file under `risk/kill_switch/` was touched. `RISK_LIMIT_BREACH` remains
  unreachable — P5-3 is a separate pass.
- One residual gap in the derivation: an order that has gone `UNKNOWN` *and*
  whose opportunity record has aged out of `SystemState`'s bounded history can
  be evicted by `_trim_orders`, which keeps only live orders and
  record-referenced ones. That belongs to the retention policy rather than to
  this calculation, and it is not changed here.

## 19. Determinism, and what did not change

No clock read, no randomness, no network, no iteration-order dependence: the
derivation walks `state.orders` and accumulates into plain dicts, and the
snapshot is a pure function of order state. `risk.limits` still contains none
of `random`, `time.time`, `datetime.now`, `uuid`, `requests`, `httpx`,
`clock`; `RuneCore.utilization` still reads no clock.

Unchanged: every configured limit value; `MIN_TRADE_NOTIONAL` semantics; gate
boundary conventions (`MAX_DAILY_LOSS` and `MAX_DRAWDOWN` strict, everything
else inclusive); gate ordering; fail-closed `UNKNOWN` handling; the RUNE-AI
boundary; `MAX_OPEN_ORDERS` remaining deliberately non-reducible; the
`working_notional` release point; consensus thresholds (`0.60` entry, `0.45`
exit); `PaperExecutor` as the only execution boundary. No live trading, no
venue-private endpoint, no wallet, no private key.

## 20. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed. Every claim above is derived from the code
as written. The failure counts, the production probe's outcome, and whether the
P5-1 audit assertions now pass are all for the external validator to establish.
