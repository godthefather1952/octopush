# Phase 5 Remediation D — pre-trade leg-risk headroom

One finding, fixed: **P5-18 — the pre-trade unhedged projection ignored the
exposure a multi-leg intent creates while its legs are filling.**

Remediation C gave the platform a post-fill backstop, and it worked: it caught
a real breach during ordinary operation, immediately, every time. What it
caught was RUNE authorising 2.5× the hard unhedged budget on every ordinary
trade. This pass fixes the half that should have prevented it.

- **Base SHA:** `61161485ad9a8859dcdbe7aa8c497e171a0ace24`
- **Branch:** `phase5-rune-remediation-d`
- **Production files changed:** `core/models/risk.py`, `risk/limits/__init__.py`,
  `agents/rune/core.py`, `apps/orchestrator/orchestrator.py`. Paper trading
  only. **No change under `risk/kill_switch/`.**
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Baseline

`phase5-rune-remediation-c` @ `6116148`, verified exact and clean, local equal
to remote. `phase5-rune-remediation-d` was created directly from that commit —
no merge, no rebase, no tag, no force-push.

## 2. External validation of Remediation C

CI run #38, on `6116148`:

| | |
| --- | --- |
| Full suite | 2907 passed / **12 failed** / 2 skipped |
| (Remediation B, for comparison) | 2846 passed / 19 failed / 2 skipped |
| Python 3.11 unit + contract | 1381 passed / 126 skipped / **0 failed** |
| Ruff | PASS |
| Mypy | PASS |
| Paper boundary | 61 passed |
| Backend contracts | 747 passed / 1 tooling-only skip |

## 3. P5-3 and P5-6 are closed

Confirmed by external validation. `RISK_LIMIT_BREACH` detects live hard-limit
breaches automatically and immediately; every flatten trigger cancels first;
`_flatten` reaches EXECUTING records. **The production breach-to-response probe
is GREEN.**

Nothing in this pass weakens any of that. `RISK_LIMIT_BREACH` keeps its
predicate, its action set, its immediate confirmation and its manual latch;
`max_unhedged_notional` stays in the backstop; no file under
`risk/kill_switch/` was touched.

## 4. Four newly exposed failures

- `tests/audit/test_noro_production_behaviour.py::test_a_meaningful_number_of_opportunities_occurred` — only 4 distinct opportunities
- `tests/failure/test_failures.py::test_the_platform_trades_without_any_intelligence_layer` — kill switch ends engaged
- `tests/failure/test_failures.py::test_a_missing_required_agent_stops_the_strategy` — no new opportunities detected after injection
- `tests/integration/test_api.py::test_kill_switch_endpoint_only_stops_things` — the fixture is already halted before the manual trigger test

**None of these four tests was modified.** They are canaries, and they all fail
for one reason.

## 5. Why they share a cause

CI #38 shows `RISK_LIMIT_BREACH` engaging during the ordinary default
simulation at roughly `START_MS + 15,200ms`, and then staying engaged —
correctly, because clearing the switch is manual by design.

Every one of those four tests runs the platform for a while and then asserts
something about a platform that is still trading: that opportunities keep being
detected, that the switch is clear, that a manual trigger is the first thing to
engage it. Once an emergency latch has fired at second 15 of a long run, all
four of those assertions describe a platform that no longer exists.

They are not four defects. They are four witnesses to one.

## 6. Why auto-clearing is not the fix

The latch is the correct behaviour. A condition that stopped trading should be
understood before trading resumes, and an emergency that clears itself is not
an emergency boundary — it is a rate limiter with extra steps.

The switch fired because the state it watches was genuinely breached. The
question worth asking is not "how do we stop it firing?" but "why did a
compliant, fully-gated trade produce a state that breaches a hard limit?".

## 7. P5-18

**HIGH — pre-trade leg risk.**

*Invariant:* an authorised multi-leg intent must not have a known execution
sequence that necessarily crosses `max_unhedged_notional`.

`gate_unhedged` compared only `ctx.unhedged_notional` — the residual already
present in the filled book — and `_headroom` had no candidate for it at all,
because the gate was classified as a pure current-state check.

With the shipped defaults:

```
max_order_notional     25,000
max_unhedged_notional  10,000
strategy               BUY venue A / SELL venue B, equal per-leg notional
final planned delta    ≈ 0
worst intermediate     ≈ one full per-leg notional, one-sided
```

So RUNE could authorise 25,000 per leg against a 10,000 hard ceiling, and the
first leg to fill put the book 2.5× over it. The backstop then did exactly what
Remediation C built it to do.

## 8. Current versus projected: two different questions

The distinction is deliberate and is enforced in both directions.

| | actual | projected |
| --- | --- | --- |
| what it means | residual OKAPI measures in the FILLED book | residual that could arise if working orders fill in the most adverse sequence |
| who owns it | the post-fill kill switch | pre-trade RUNE |
| where it appears | `RiskUtilization.unhedged_notional`, `live_risk_breaches` | `CommittedExposure.unhedged_fill_risk`, `gate_unhedged`, `unhedged_headroom` |

`unhedged_fill_risk` is **not** fed to the kill switch and is **not** folded
into `RiskUtilization.unhedged_notional`. Making the emergency layer fire
because an order *could* create exposure would engage it merely because a trade
is in flight; making the dashboard field mean two things at once would make it
mean neither. It is reported beside the actual figure as
`RiskUtilization.pending_unhedged_fill_risk` instead.

## 9. Deriving pending fill risk

Accumulated on the existing walk in
`Orchestrator._current_committed_exposure()` — no second ledger, no second pass
over the orders, and the same entry classification Remediation B established.
For each unresolved ENTRY order:

```
quote = remaining_quantity * expected_price
buy_remaining[symbol]  += quote      (if BUY)
sell_remaining[symbol] += quote      (if SELL)
```

## 10. Per-symbol side aggregation

```
unhedged_fill_risk = Σ over symbols  max(buy_remaining[s], sell_remaining[s])
```

For one symbol: if every working BUY fills before any SELL, the book is
transiently long the whole BUY side; if the SELLs go first, short the whole
SELL side. The worst magnitude is the larger side — **not** their net (which
assumes the offset lands) and **not** their sum (which assumes both sides go
one-sided at once, which they cannot).

Across symbols the bound is **summed**, because each symbol can independently
become one-sided, and `Okapi.total_unhedged` is itself
`Σ abs(residual per symbol)` — the same aggregation, so the bound is stated in
the units the limit is measured in.

Worked examples, all pinned by tests:

| working orders | bound | not |
| --- | --- | --- |
| BTC BUY 7,000 + BTC SELL 7,000 | 7,000 | 0 (net) or 14,000 (gross) |
| BTC BUY 8,000 + BTC SELL 5,000 | 8,000 | 3,000 (net) or 13,000 (gross) |
| BTC ±5,000 and ETH ±4,000 | 9,000 | 5,000 |

Note that the same symbol on two venues still offsets here even though it is
two distinct positions for `MAX_POSITION_NOTIONAL`: unhedged residual is a
delta measured across venues.

## 11. The new-intent fill factor

`risk.limits.unhedged_fill_factor(intent)` — pure, and computed from leg shape
alone. Group legs by symbol, take `max(buy_legs, sell_legs)` per symbol, sum
across symbols:

| intent | factor |
| --- | --- |
| 1 BUY BTC | 1 |
| BUY BTC + SELL BTC | 1 |
| 2 BUY BTC + 1 SELL BTC | 2 |
| BUY BTC + SELL ETH | 2 |
| BUY BTC + SELL BTC + BUY ETH + SELL ETH | 2 |

## 12. The execution multiplier

VESKA sizes each leg as `notional / routing.expected_price`, but a marketable
order may fill worse than expected — up to the slippage budget the intent
already carries. Taking the worst-case filled quote exposure as exactly
`notional` therefore still permits a trade that lands slightly over the limit.

```
execution_multiplier = 1 + max(0, intent.max_slippage_bps) / 10,000
```

The intent's own `max_slippage_bps` is used rather than a new configurable
buffer: it is already the platform's statement of how far a fill may stray, and
a second number would give one question two answers. No change to the execution
model, and no new configuration.

## 13. The gate

```
actual   = abs(ctx.unhedged_notional)
pending  = committed.unhedged_fill_risk
incoming = intent.notional * unhedged_fill_factor(intent) * execution_multiplier(intent)

PASS  when  actual + pending + incoming <= max_unhedged_notional
```

`observed` is the projection, `limit` is `max_unhedged_notional`, and `detail`
separates the three terms — `"2,000.00 actual + 3,000.00 pending + 1,001.00
incoming"` — so a rejection says which term consumed the budget.

**Addition here is deliberately conservative.** `ctx.unhedged_notional` is an
unsigned aggregate: it does not retain the signed direction of each per-symbol
residual, so the sum cannot know that an incoming first fill might offset an
existing residual rather than add to it. The result is an upper bound that can
overstate and never understates. For a hard safety limit that is the correct
direction to be wrong in, and it is not offered as an exact post-fill
predictor.

## 14. The headroom

`risk.limits.unhedged_headroom(...)` mirrors the gate term for term. The gate
is linear in `n`, so it rearranges directly:

```
remaining = max_unhedged_notional - abs(unhedged) - committed.unhedged_fill_risk
candidate = remaining / (unhedged_fill_factor(intent) * execution_multiplier(intent))
```

clamped at zero, and added to `RuneCore._headroom`. That makes `evaluate`'s
documented contract — "size the intent to fit every size-sensitive limit" —
true for MAX_UNHEDGED_EXPOSURE as well.

A non-finite residual also lands on zero: every comparison against NaN is
false and `max(0.0, nan)` is `0.0`. An unknown residual is not an acceptable
one.

## 15. UNKNOWN, partial and terminal

The same derived-state handoff P5-1 relies on, so the three dimensions cannot
disagree about which orders exist:

- **UNKNOWN** entry orders keep their bound. UNKNOWN means the venue-side truth
  is not known, not that the order is gone; it may still fill and go one-sided.
- **Partial fills** shrink the bound by exactly what the filled part adds to
  the actual residual OKAPI measures — `remaining_quantity`, never the original.
- **Terminal** orders (FILLED, CANCELLED, REJECTED, EXPIRED) release it
  entirely: nothing left that can fill is nothing left that can go one-sided.
- **Exits and hedges** consume none of it. They reduce exposure, and charging
  them against the entry budget could stop the platform hedging its way out of
  a breach.
- **Unclassifiable** orders still contribute, matching the existing fail-closed
  fallback: an order conservatively counted as an entry commitment must not
  vanish from this dimension.

## 16. Expected effect on the default strategy

For an ordinary `BUY BTC / SELL BTC` intent with a 10bps slippage budget,
factor 1 and multiplier 1.001:

```
requested per-leg   25,000
authorised          10,000 / 1.001  =  9,990.01
projected unhedged  exactly 10,000
```

RUNE now reduces rather than authorising 25,000 and relying on the kill switch
after the first leg fills. Nothing is hard-coded: both numbers come from
`RiskLimits`, and neither default changed.

Two consequences worth stating plainly:

- **The order limit is no longer what binds the shipped strategy.** The
  unhedged budget is tighter. `tests/unit/test_risk.py` records both cases —
  the default, where unhedged binds, and a control with an ample unhedged
  budget, where `max_order_notional` binds again.
- **Rejections on this dimension now arrive as `MIN_TRADE_NOTIONAL`.** Once a
  gate is mirrored in `_headroom` it can no longer be reached as a *blocking*
  gate: either the reduction makes it pass, or headroom is zero and `evaluate`
  short-circuits. That is true of every size-sensitive limit, and it is the
  same shape non-positive equity took for MAX_LEVERAGE in Remediation B. Four
  assertions that named the gate were updated to assert the safety property
  (nothing is authorised) plus a direct call proving the gate itself still
  fails the state. None was weakened.

## 17. Audit fixtures that had to open the limit

Several audit fixtures declare "generous everywhere so only the limit under
test binds" and then set every limit but this one, because it was not
size-sensitive when they were written. Now that it is, they must open it like
the rest, or they silently stop isolating what they claim to isolate.
`max_unhedged_notional` was opened in the isolation fixtures of
`test_rune_headroom.py`, `test_rune_pending_reservations.py`,
`test_rune_strategy_exposure.py`, `test_rune_remediation_a_boundaries.py` and
one config-coherence case in `test_rune_loss_boundaries.py`.

No safety assertion was changed by that sweep. The fixtures that deliberately
hold this limit tight — `test_rune_leg_fill_risk.py` and the backstop's
`TestUnhedgedBoundary` — were left tight.

## 18. Findings deliberately deferred

Untouched, with their audit tests expected to keep failing: **P5-5, P5-8, P5-9,
P5-10, P5-12, P5-14, P5-15, P5-16, P5-17.**

Specifically not done here: no `execution_disabled = False` anywhere;
`EXCESSIVE_LATENCY` unchanged; predicate-exception handling unchanged;
`RiskLimits` still accepts `+inf`; `_limits_are_coherent` unchanged;
`KillSwitch.engage`/`clear` still read the live clock; `AGENT_FAILURE` still
has no predicate.

## 19. What must not have regressed

Preserved and re-verified statically: P5-1 committed exposure (derived, UNKNOWN
reserved, partial-fill handoff, entry/exit/hedge classification), P5-2 strategy
units, P5-3 live backstop with immediate detection, P5-4 projected open orders,
P5-6 cancel-before-flatten, P5-7 net and leverage headroom, P5-11 duplicate
venue/position aggregation, P5-13 utilization units.

All 21 exported gates are still run by `_gate`; `_headroom` now bounds seven
size-sensitive limits and still excludes `max_open_orders` deliberately;
`risk.limits` still contains none of `random`, `time.time`, `datetime.now`,
`uuid`, `requests`, `httpx`, `clock`; `RuneCore.utilization` still reads no
clock.

No `RiskLimits` default changed. No consensus threshold, TIDAL deadband, NORO
semantic or ZEPHR economic changed. No change to the fill simulator, order
latency, maker/taker behaviour, fees, slippage, routing, order quantities or
the cancel-race simulation. The synthetic market is untouched — the risk layer
sizes against the market it already trades. Phase 3+4 is unchanged, and the
replay framework is unmodified.

## 20. The production probe

Remediation C's breach-to-response semantics are kept exactly. "No breach may
ever occur" is **not** restored as the safety invariant, and no expectation of
zero triggers is hard-coded.

With pre-trade fill-risk sizing in place the ordinary 400-tick run is expected
to avoid the previously deterministic unhedged breach — but if it still
observes one, the existing breach-to-response assertions remain the authority
and remain unweakened.

## 21. Determinism

Every new helper is pure. `unhedged_fill_factor` reads leg shape;
`execution_multiplier` reads one intent field; `unhedged_headroom` and
`gate_unhedged` read the intent, the measurement, the committed snapshot and
`RiskLimits`. No clock, no network, no randomness, no model. The same order
state, portfolio and intent always produce the same fill-risk projection,
headroom, gate result and approved notional.

## 22. Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed under a test runner. Whether the four canary
tests go green naturally, and whether the production probe now runs without a
trigger, are for the external validator to establish.

---

## 23. External validation — CI #39

Run on `d5c5edb`:

| | |
| --- | --- |
| Full suite | 2939 passed / **43 failed** / 2 skipped |
| Python 3.11 unit + contract | 1381 passed / 126 skipped / **0 failed** |
| Ruff, Mypy core, paper boundary, Redis + PostgreSQL contract pre-check | PASS |

**P5-18 — PARTIAL / VALIDATION PENDING.**

### The sizing change is present

Every P5-18 direct test passes. `test_rune_leg_fill_risk.py`,
`test_rune_committed_exposure.py::TestPendingUnhedgedFillRisk` and the
size-sensitivity inventory are all green, and the baseline unit/contract suite
is unchanged at zero failures. The fill factor, the slippage multiplier, the
projected gate, the headroom solver, the pending fill-risk accounting and the
25,000 → 9,990.01 default sizing are all doing what Remediation D says they do.

### The default run still enters emergency state

`RISK_LIMIT_BREACH` engages at `1788000015200` — `START_MS + 15,200ms` —
**exactly the same first-trigger instant observed before Remediation D**. The
four canaries therefore still fail, for the same reason they failed in CI #38:
they assert things about a platform that is still trading, and it is not.

An unchanged first-trigger time is itself informative: whatever remains is not
sensitive to the entry size P5-18 reduced. That rules out the simplest
explanation and rules against reaching for a second production adjustment
before the state at the trigger has been measured. **No further production
change is justified until then**, which is why this pass adds no production
diff at all.

### The exposure-projection failure cluster is a stale fixture

The largest new cluster is `tests/audit/test_rune_exposure_projection.py`, and
none of it is a newly discovered projection defect:

```
gross    projection 19,980.02   vs realised 50,000
venue    projection  9,990.01   vs realised 20,000
position projection  9,990.01   vs realised 10,000
net      projection  9,990.01   vs realised 10,000
leverage projection      0.1998 vs realised     0.2
```

Every claimed projection is exactly the 9,990.01 sizing, or a multiple of it.
The module's `OPEN` fixture declares "generous everywhere, so nothing is
reduced" but never named `max_unhedged_notional`, because while the gate had no
headroom candidate the shipped 10,000 default could not reduce anything and the
claim was true without it. P5-18 made the gate size-sensitive; `OPEN` then cut
every intent to 9,990.01 while the module's `apply_trade` reference portfolio
was still built from the ORIGINAL request. The two sides stopped describing the
same trade.

The repair is to open the unrelated limit, and only that: the assertions,
`apply_trade`, and the comparison against the *requested* size are all
unchanged. The question this module asks — at a size every limit intentionally
allows, does each gate conservatively bound the whole requested trade? — is
still the right question and still gets a straight answer.

The same omission was closed in three `test_rune_gate_invariants.py` fixtures
and one `test_rune_loss_boundaries.py` config-coherence fixture. Those four
passed either way, but with the unhedged budget binding first they no longer
demonstrated the order or venue limit they name.

### What is measured next

`tests/audit/test_rune_default_breach_diagnostic.py` runs the real platform for
250 ticks and reports the complete state at the first trigger: which dimensions
`live_risk_breaches` flags, each observed value against its limit, the entry
authorisations that preceded it with their MAX_UNHEDGED_EXPOSURE projections,
the orders those entries became, and — for the dominant one-sided position — the
expected price VESKA sized against, the average entry price actually filled, and
the current mark, as three notionals plus their bps differences.

That last comparison separates the candidate causes rather than assuming one:

- entry away from expected → execution slippage at fill time
- mark away from expected → mark-to-market drift after sizing
- all three in agreement → the position is simply larger than the budget, a
  sizing-model gap
- unhedged breached with no large position → a residual carried in from an
  earlier, partly-closed trade

The diagnostic asserts `first_trigger is None` and is **expected to fail** while
the default run still trips the switch. That is deliberate: the failure is what
makes CI print the report.

No buffer, tolerance, limit change, auto-clear or confirmation delay was
implemented in this pass. The measurement decides the fix.
