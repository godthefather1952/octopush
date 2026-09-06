# Phase 5 Remediation C — live hard-limit backstop

Two findings, fixed: **P5-3 — no automatic detection of live hard-risk
breaches**, and the directly coupled **P5-6 — cancel-before-flatten and the
EXECUTING-order safety race.**

Remediation A and B made RUNE's pre-trade arithmetic correct and gave it the
exposure that is committed but unfilled. This pass adds the layer that runs
*after* the trade: the kill switch now asks, every protected tick, whether the
state that actually exists has crossed a hard boundary — and does something
about it when it has.

- **Base SHA:** `12eb84202e78b11a7bde4698c892795de2705f59`
- **Branch:** `phase5-rune-remediation-c`
- **Production files changed:** `risk/kill_switch/__init__.py`,
  `apps/orchestrator/orchestrator.py`. Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Baseline

`phase5-rune-remediation-b` @ `12eb842`, verified exact and clean, local equal
to remote. `phase5-rune-remediation-c` was created directly from that commit —
no merge, no rebase, no tag, no force-push.

## 2. External validation of Remediation B

CI run #37, on `12eb842`:

| | |
| --- | --- |
| Full suite | 2846 passed / **19 failed** / 2 skipped |
| (Remediation A, for comparison) | 2790 passed / 28 failed / 2 skipped |
| Python 3.11 unit + contract | 1381 passed / 126 skipped / **0 failed** |
| Ruff | PASS |
| Mypy core | PASS |
| Paper boundary | 61 passed |
| Redis + PostgreSQL contract pre-check | 747 passed / 1 tooling-only skip |

Nine former failures disappeared.

## 3. P5-1 is closed

Confirmed closed by external validation: concurrent gross, venue, position and
leverage commitment, plus the new committed-net accounting coverage. The three
stale non-positive-equity audit failures are also gone.

P5-1 is not revisited here. Remediation C only *reads* the
`CommittedExposure` it introduced.

## 4. The breach that did not move

The 400-tick production probe still measured, unchanged:

```
MAX_NET_EXPOSURE       observed 25,103.800333426625   limit 25,000.0
MAX_UNHEDGED_EXPOSURE  observed 25,103.800333426625   limit 10,000.0
risk evaluations 8, verdicts 8 APPROVED, kill-switch triggers []
```

That is the same live breach observed before P5-1 was remediated, and it is
useful evidence rather than a contradiction: P5-1 was real and is now
independently repaired, and what remains comes from post-authorisation
behaviour that pre-trade projection cannot fully prevent.

## 5. Why pre-trade risk cannot eliminate the transient

RUNE decides at one instant, from what is known at that instant. Between the
decision and the settled position:

- one leg of a multi-leg trade fills before the other, so the book is
  transiently one-sided;
- fills land away from the expected prices the projection was built on;
- a fill partials, leaving a fraction of the intended offset;
- a cancel loses a race to a fill;
- a hedge is briefly incomplete.

Each of these is a state that exists *after* the last gate ran. Prevention and
detection are different jobs, and the platform needs both. Note in particular
that the observed breach is on the **unhedged** dimension, which is by
definition about a residual that only exists once something has filled.

## 6. The `RISK_LIMIT_BREACH` predicate

`TRIGGER_ACTIONS` already named `RISK_LIMIT_BREACH`; `TRIGGERS` did not, and no
production caller engaged it. It was a safety response nothing could invoke.

Now:

```python
def live_risk_breaches(inputs, settings) -> list[str]: ...
def _risk_limit_breach(inputs, settings) -> bool:
    return bool(live_risk_breaches(inputs, settings))

TRIGGERS = {..., "RISK_LIMIT_BREACH": _risk_limit_breach, ...}
```

`live_risk_breaches` returns the names of the breached dimensions — useful for
logs, tests and operator observability. Nothing branches on their order; the
predicate only asks whether the list is empty.

`KillSwitchInputs` gained three backward-compatible fields with zero defaults:
`committed_exposure: CommittedExposure`, `strategy_exposure: float`,
`unhedged_notional: float`. The orchestrator passes **state**, never a
pre-computed verdict, so `TRIGGERS` remains the canonical detection layer and
the audit can prove from the predicates which limits are actually enforced.

**Immediate confirmation.** `RISK_LIMIT_BREACH` is deliberately absent from
`CONFIRMATIONS`, so its required streak is 1. A measurement can blip; a
breached hard boundary is true the first tick it is observed, and two more
ticks of confirmation are two more ticks of trading past a limit.

**Boundary semantics.** These are maximum limits, matching RUNE's gate
convention: `value <= limit` is safe, only `value > limit` is a breach. Sitting
exactly on a limit never fires, so the emergency layer cannot fire on a state
the pre-trade layer would have authorised. No safety band was invented.

## 7. Filled plus committed

For the five dimensions P5-1 repaired, the backstop inspects filled exposure
plus unresolved entry commitment — both are risk the platform has already
accepted — using the same `CommittedExposure` model, not a second calculation:

```
gross_current       = portfolio.gross_exposure + committed.gross_exposure
net_current         = portfolio.net_exposure   + committed.net_exposure     (signed)
venue_current[v]    = filled venue exposure    + committed.venue_exposure[v]
position_current[k] = positions[k].notional    + committed.position_exposure[k]
leverage            = gross_current / portfolio.equity
strategy_current    = inputs.strategy_exposure                (canonical, unchanged)
unhedged_current    = abs(inputs.unhedged_notional)           (filled book only)
```

Detail worth stating:

- **Leverage.** Non-positive equity *carrying* exposure is a breach — unbounded
  leverage, reported rather than divided by. Flat and insolvent is not: with no
  exposure there is no leverage to breach, and manufacturing an infinity would
  fire this trigger for a condition that is not its subject.
- **Position.** Opposing legs on one `venue:symbol` are added, never netted —
  the same conservative definition RUNE uses.
- **Strategy.** `committed.gross_exposure` is deliberately **not** added.
  `strategy_exposure` is reserved at authorisation and held for the whole
  lifecycle, so it already covers working trades; adding it again would
  double-count them and halve the effective budget (P5-2).

## 8. Unhedged, watched at its own limit

`abs(unhedged_notional) > max_unhedged_notional` is a breach.

This is the gap that mattered. `UNEXPECTED_POSITION` only fires at
`max_unhedged_notional * 3` — 30,000 against a configured limit of 10,000 —
leaving a band in which the configured hard gate said *unsafe* and the
emergency layer said nothing at all. The production probe's 25,103.80 sat
squarely inside it.

`committed.net_exposure` is deliberately **not** netted off here. An unfilled
second leg is a commitment, but it has not neutralised the one-sided position
that exists now, and this dimension is precisely about the residual that exists
now. That distinction is why the probe reached the breach in the first place.

## 9. `UNEXPECTED_POSITION` is unchanged

Not removed, not weakened. It remains the catastrophic-anomaly trigger at the
3x level, with its own action set including `DISABLE_EXECUTION`. Both levels
are pinned by tests: the own-limit breach is caught by `RISK_LIMIT_BREACH`, the
3x anomaly by `UNEXPECTED_POSITION`.

## 10. The response

`TRIGGER_ACTIONS["RISK_LIMIT_BREACH"]` is now:

```
HALT_NEW_TRADES, CANCEL_ALL, FLATTEN
```

Halting new entries alone leaves the breaching position in place, which is not
a response to a breach that already exists.

`DISABLE_EXECUTION` is deliberately **excluded**. Closing a position and
hedging a residual are themselves submissions; a trigger whose job is to reduce
exposure cannot also refuse to submit. P5-5 (execution-disabled recovery) is a
separate decision and is untouched.

## 11. P5-6 — cancel before flatten

`MAX_DRAWDOWN_BREACHED` and `MAX_DAILY_LOSS_BREACHED` requested `FLATTEN`
without `CANCEL_ALL`. An opening order already resting when the trigger fired
could fill after the flatten completed, re-opening the exposure the safety
action had just closed. Both now request `HALT_NEW_TRADES, CANCEL_ALL,
FLATTEN`.

After this pass **every action set containing `FLATTEN` also contains
`CANCEL_ALL`** — `MAX_DRAWDOWN_BREACHED`, `MAX_DAILY_LOSS_BREACHED`,
`RISK_LIMIT_BREACH`, `MANUAL` — and the invariant is pinned over the derived
set of flatten triggers rather than over a hard-coded list, so a trigger added
later cannot slip past it. The same test asserts none of them disables
execution.

`Orchestrator._protect` applies `cancel_all_requested` before
`flatten_requested`, unchanged and now covered by a regression assertion:
cancelling after flattening would leave the identical window.

## 12. P5-6 — EXECUTING records are flattened

`_flatten` handled `MONITORING`, `RECONCILING` and `HEDGING` but skipped
`EXECUTING` — precisely the state whose entry orders may still be live or
partly filled, and therefore the record a flatten most needs to reach. It is
now in the list. `EXECUTING -> EXITING` was already a legal transition; nothing
about the state machine changed.

Semantics when an EXECUTING record is flattened:

1. `CANCEL_ALL` has already been applied — every flatten trigger cancels, and
   `_protect` cancels first — so its resting entry orders are cancel-pending.
2. Whatever portion already filled is in `PaperAccount`.
3. `_submit_exit` builds exit legs from the position that ACTUALLY exists —
   `quantity = abs(position.quantity)` — never from the original approved
   notional. That rule is unchanged and now asserted.
4. Unfilled entry quantity remains cancel-pending and may still lose the race.
5. If that late fill lands, the ordinary EXITING residual logic notices the
   residual and works it down through its retries and OKAPI's standing hedge
   loop.

## 13. Orchestrator input wiring

`_protect` derives each value once and passes all three in:

```python
unhedged = self.okapi.total_unhedged(portfolio)
committed = self._current_committed_exposure()
strategy_exposure = self._current_strategy_exposure()
```

These are the same helpers the tick's risk-utilization snapshot and RUNE's own
gates use, so the two safety layers cannot come to disagree about what the
platform currently holds. The `UNEXPECTED_POSITION` input still reads
`abs(unhedged) > max_unhedged_notional * 3` from the same measurement.

## 14. The production probe's new invariant

The probe used to assert that no live breach may ever be observed. That was the
right question while testing whether pre-trade risk alone could hold the state
inside limits; two validation runs have now answered it, and the answer is no.

The measurement is **not** deleted or weakened. It is followed through. The
probe now records, for every tick: tick number, current live breaches,
`triggered_by`, `halt_new_trades`, open-order count and live position count.
Then it asserts:

1. **Detection.** Any tick whose live state breaches a hard limit must have
   `RISK_LIMIT_BREACH` in `triggered_by` by the end of that same
   `orchestrator.tick()`. The tick order is settle → measure → protect →
   manage → seek, so a fill-created breach is visible before `_protect` runs
   and there is no reason to defer detection to the following tick.
2. **No unexplained triggers.** `kill_switch_triggers == []` is replaced by
   "nothing outside `{RISK_LIMIT_BREACH}` fired".
3. **No new entries after the response.** No approved entry `RISK_PASS` and no
   entry `EXECUTION_PLAN` after the trigger tick. Scoped by entry intent —
   exits, hedges and the flatten build their decisions directly and publish no
   `RISK_PASS`, so risk-reducing activity is untouched by this assertion.
4. **Eventual recovery.** The final state of the run must be back inside
   gross, net, venue, position, leverage and unhedged limits. A trigger that
   fires and leaves the book breached has not finished its job.

The probe measures the live state with its own arithmetic — an independent
implementation of the stated contract (filled + committed for exposure, the
filled book alone for the unhedged residual) rather than a call into
`live_risk_breaches`, so it compares production against the requirement rather
than against itself.

**If assertion 4 fails externally, leave it failing.** It means the safety
response is incomplete and another remediation is needed. It was not weakened
preemptively.

## 15. Findings deliberately deferred

Untouched, with their audit tests expected to keep failing:

| ID | |
| --- | --- |
| **P5-5** | `execution_disabled` recovery — nothing re-enables the executor |
| **P5-8** | lifetime vs rolling error rate |
| **P5-9** | future-timestamp defence in depth |
| **P5-10** | `EXCESSIVE_LATENCY` consumes data age, not latency |
| **P5-12** | kill-switch predicate exception fail-open |
| **P5-14** | `+Infinity` limit validation |
| **P5-15** | additional config coherence |
| **P5-16** | kill-switch logical-time stamps |
| **P5-17** | vestigial `AGENT_FAILURE` mapping |

Specifically **not** done here: no `execution_disabled = False` anywhere;
`_latency` unchanged; `KillSwitch.evaluate`'s exception handling unchanged;
`engage`/`clear` still read the live clock rather than taking a logical
`now_ms`; `AGENT_FAILURE` still has no predicate.

`AGENT_FAILURE` is now the ONLY dead automatic action mapping, and the audit's
inventory premise was updated to say so — its own P5-17 assertion is untouched
and still fails.

## 16. Regression status

Remediation A and B are preserved and were re-verified statically: canonical
per-leg notional; strategy gross reservation units; risk-utilization strategy
units; the net-exposure root solver; leverage headroom; duplicate venue and
position grouping; projected open-order capacity; `CommittedExposure` derived
from unresolved entry orders; `UNKNOWN` staying reserved; partial-fill handoff;
exit and hedge exclusion. Nothing was duplicated or replaced — the backstop
consumes the same model.

No `RiskLimits` default changed. No consensus threshold, TIDAL deadband, NORO
semantic or ZEPHR economic changed. No change to the fill simulator, order
latency, maker/taker behaviour, fees, slippage, routing, order quantities or
cancel-race simulation: P5-6 changes which records a flatten visits and which
actions a trigger requests, never how easily anything fills.
`agents/rune/core.py` and `risk/limits/__init__.py` were not touched — this is
post-fill enforcement, not another RUNE sizing change.

Phase 3+4 is unchanged.

## 17. Determinism

`live_risk_breaches` is pure: no wall-time read, no sample drawn, no network,
no model. The same portfolio, committed exposure, strategy exposure, unhedged
measurement and settings always produce the same list, so a replayed run
reaches the same verdict as the live one that produced it. The replay framework
is unmodified.

## 18. Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed. Every claim above is derived from the code
as written. Whether the production probe's four new invariants hold — in
particular eventual recovery — is for the external validator to establish.
