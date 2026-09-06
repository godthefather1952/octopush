# Phase 5 Remediation E2 — final risk boundary cleanup

The last four Phase 5 findings, fixed: **P5-8** (`MAX_ERROR_RATE` measured the
whole uptime instead of the recent past), **P5-9** (a market-data timestamp in
the future read as maximally fresh), **P5-14** (`RiskLimits` accepted
`+Infinity` as a limit), and **P5-15** (two incoherent configurations loaded
without warning).

None of them changes what a healthy platform does. Each closes a way for the
risk boundary to report itself as enforced while enforcing nothing.

- **Base SHA:** `cefb76edbb6261208f89667c9c4efa932d329013`
- **Branch:** `phase5-rune-remediation-e2`
- **Production files changed:** `core/bus/base.py`, `core/bus/memory.py`,
  `core/bus/redis_bus.py`, `core/bus/__init__.py`, `core/config/settings.py`,
  `risk/limits/__init__.py`, `apps/orchestrator/wiring.py`,
  `apps/orchestrator/orchestrator.py`. Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Baseline

`phase5-rune-remediation-e1` @ `cefb76e`, verified exact and clean, local equal
to remote. `phase5-rune-remediation-e2` was created directly from that commit —
no merge, no rebase, no tag, no force-push.

## 2. Scope

Exactly P5-8, P5-9, P5-14 and P5-15. Everything else in Phase 5 is frozen:
`risk/kill_switch/**` is not in the diff, and neither are NORO, TIDAL, ZEPHR,
consensus, the simulation, execution, the OMS, `PaperExecutor`, OKAPI, market
generation or the replay engine. No limit value was tuned (§9).

## 3. P5-9 — data from the future is no longer the freshest data

`gate_data_age` computed `age = now_ms - source_data_timestamp` and passed when
`age <= max_data_age_ms`. Every negative number satisfies that. A timestamp
stamped a day ahead produced an age of −86,400,000ms and passed comfortably —
and the further wrong the timestamp was, the more freshness the gate credited
it with. Exactly backwards, and reachable without an adversary: a venue clock
that has jumped, or a unit mix-up that multiplies a timestamp.

The permitted interval is now two-sided:

```
-limits.max_clock_skew_ms <= age <= limits.max_data_age_ms
```

**The lower bound is `max_clock_skew_ms`, not zero.** Two independently synced
machines disagree by small amounts constantly, so `age >= 0` would reject
honest data. `max_clock_skew_ms` is the tolerance the platform already
configures for precisely this question — how far an exchange timestamp may lead
local receipt before it stops being ordinary drift — so it is reused rather
than duplicated by a second number that could drift away from it.

What did not change: the gate name (`MARKET_DATA_FRESH`), `observed` (still the
signed age, so a rejection reads `-2001` rather than an absolute value that
hides the direction), `limit` (still `max_data_age_ms`), the UNKNOWN-and-block
path for a missing timestamp, and the fact that the gate reads no clock —
`now_ms` is supplied by the caller, which is what lets replay reach the same
verdict at the same logical instant. `detail` now names the whole interval
(`market data age -2001ms; permitted [-2000, 2000]ms`), because "age -2001
against limit 2000" reads like a pass.

**TIDAL's upstream skew defence is untouched** and still runs first. `_quality`
still marks a book UNAVAILABLE when its exchange timestamp leads receipt beyond
tolerance. This is defence in depth, not a replacement: both are asserted.

Boundaries pinned: source == now PASS, 1ms lead PASS, `max_clock_skew_ms` lead
PASS, `max_clock_skew_ms + 1` FAIL, `max_data_age_ms` PASS,
`max_data_age_ms + 1` FAIL, missing timestamp UNKNOWN and blocking.

## 4. P5-8 — the error rate is now actually rolling

`Orchestrator._error_rate` documented itself as "Rolling share of bus
deliveries that raised" and computed `errors / (delivered + errors)` over
`Subscription.delivered` and `Subscription.errors` — **lifetime** counters that
never decay.

The gate exists to catch a platform that is failing *right now*. A lifetime
ratio cannot express that. After 100,000 healthy deliveries it took tens of
thousands of accumulated errors to move the number past 0.25; a process failing
every single current delivery still reported roughly 0.002 and traded on. The
longer the platform stayed up, the harder it became to stop.

### The window

`core.bus.base.DeliveryOutcomeWindow` holds the outcomes of the most recent N
delivery attempts in a fixed-length `deque`: `record_success()`,
`record_error()`, and an `error_rate` property that reports the share that
failed (`0.0` when empty — no evidence is not evidence of failure, and the gate
has a separate UNKNOWN path for genuinely missing inputs). The running error
count is maintained incrementally, so reading the rate is O(1) whatever the
window size.

It is **bounded** (memory does not grow with uptime), **deterministic** (the
same sequence of calls always yields the same rate) and **clock-free**
(eviction is by count, not by age) — which is what makes it usable inside
replay: two runs over the same event sequence measure the same health, whatever
wall time either took.

### Where the outcomes are recorded

At dispatch time, in the bus, one entry per matching handler per event — never
reconstructed afterwards from totals. Both implementations do it identically:
`InMemoryEventBus._dispatch_one` and `RedisStreamBus._dispatch`.

In the in-memory bus the error is recorded **before** the optional re-raise. With
`raise_on_handler_error=True` a failure propagates, but it is still a failure
that happened; a window that forgot it would report the healthier of the two
possible answers exactly when the bus is configured to be strict. No attempt is
counted twice: success and error are exclusive branches of one `try`.

Redis transport behaviour is unchanged — the recording sits inside the existing
`try/finally`, ahead of the unchanged `_inflight` decrement, `_last_dispatched_id`
assignment and `xack`.

### The contract

`recent_error_rate` is an **abstract** property on `EventBus` (contract clause
9). An implementation that cannot answer it cannot be used, rather than being
silently substituted or quietly measured by lifetime totals: a health input
that degrades to a different definition without saying so is worse than one
that is missing, because the gate reading it cannot tell the difference.

`build_platform` follows the same rule for a caller-supplied bus. It is never
swapped out — the whole point of the argument is that the caller controls the
transport — but one that cannot answer raises `TypeError` at construction
rather than letting the orchestrator fall back to lifetime counters.

### Configuration

`RiskLimits.error_rate_window_deliveries`, default 200, `gt=0`, `le=100_000`.
Threaded into both buses by `build_platform`. It is held equal to
`core.bus.base.DEFAULT_DELIVERY_WINDOW` by an audit assertion rather than by an
import, so configuration does not have to depend on the transport package.

The unit suffix is deliberate: the window is counted in delivery attempts, not
milliseconds, and `deliveries` joined the settings suite's unit vocabulary for
exactly that reason.

### What the orchestrator does now

```python
def _error_rate(self) -> float:
    return self.bus.recent_error_rate
```

`Subscription.delivered` and `Subscription.errors` **remain**, as per-handler
diagnostics — they say *which* handler is failing, which the aggregate cannot.
They are simply no longer the shape of a health measurement.

### Measured behaviour

| Recent 200 deliveries | Reported rate |
| --- | --- |
| 200 successes | 0.0 |
| 150 successes + 50 failures | 0.25 (at the limit, passes) |
| 149 successes + 51 failures | 0.255 (blocks) |
| 10,000 old successes, then 200 failures | 1.0 |
| 10,000 old failures, then 200 successes | 0.0 |
| nothing yet | 0.0 |

The last two matter as much as the others. A platform that has recovered must
be allowed to trade again; a rolling window is indifferent in both directions.
After 100,000 healthy deliveries the gate now fires on the 51st recent error.

## 5. P5-14 — an infinite limit is not a limit

`RiskLimits` constrains its floats with `gt=0`, which admits `+inf`. Every
comparison against an infinite ceiling passes, so one value in one config file
turned a hard gate into a permanent PASS while the gate still reported itself
as checked and the dashboard still rendered a limit. NaN fails in the other
direction: every comparison against it is false, so the gate reads as failing
with no size that could ever satisfy it.

`model_config = ConfigDict(extra="forbid", allow_inf_nan=False)` makes the
property total across all thirteen float fields — `max_position_notional`,
`max_gross_exposure`, `max_net_exposure`, `max_leverage`, `max_daily_loss`,
`max_drawdown`, `max_venue_exposure`, `max_strategy_exposure`,
`max_order_notional`, `min_trade_notional`, `max_unhedged_notional`,
`min_expected_edge_bps`, `max_error_rate` — rather than field by field, where a
field added later could quietly escape. The convention already existed:
`TidalConfig` and `NoroConfig` set the same flag.

The audit test enumerates the float fields from the model rather than from a
hand-written list, and a guard asserts the enumeration is non-empty and matches
the known set, so the parametrised cases cannot pass vacuously.

Integer fields need no infinity rule. **No default moved.**

## 6. P5-15 — two incoherent configurations now refuse to load

### `max_order_notional <= max_venue_exposure` (on `RiskLimits`)

Every order executes on exactly one venue, so a per-order cap larger than any
one venue may hold means every trade is silently reduced or rejected — the
platform looks merely quiet. This is the same shape the validator already
rejected for position and gross, left unchecked for venue. Equality is allowed:
one order may fill a venue's whole budget.

### `max_strategy_exposure >= SHIPPED_ENTRY_LEGS * min_trade_notional` (on `Settings`)

Below this, the smallest entry the platform can construct cannot be authorised
and every trade rejects at `MIN_TRADE_NOTIONAL`.

**This rule lives on the root `Settings`, deliberately not on `RiskLimits`.**
It depends on `CrossVenueDetector` building exactly two entry legs and on every
size-sensitive gate charging `notional * len(intent.legs)` — a fact about the
strategy that is currently shipped, not a property of risk limits in general. A
one-leg strategy would find a two-leg floor arbitrary, so `RiskLimits` still
accepts the same numbers on its own; the leg count is a named constant
(`SHIPPED_ENTRY_LEGS`) so a future strategy changes one line and the rule
follows.

### `max_unhedged_notional - hedge_tolerance_notional >= min_trade_notional` (on `Settings`)

The recovery reserve introduced by Remediation D2 comes off the top of the
unhedged budget an entry may use. If the reserve consumes the whole budget, no
entry can ever be sized — and nothing said so. Also a root rule, because
`hedge_tolerance_notional` lives on `Settings`.

**No rule requiring the same symbol on two or more venues was added.** A
single-venue universe is a legitimate configuration; it simply yields no
cross-venue opportunity, which is the correct outcome, not a misconfiguration.

Each of the three is asserted from both sides: the boundary value loads, one
unit below (or above, for the venue rule) raises `ValidationError`.

### Fixture collateral

The venue rule made three isolation fixtures incoherent — they held
`max_order_notional` wide open while narrowing `max_venue_exposure` to isolate
the venue gate. Each now holds the order cap equal to the venue cap, which
leaves it non-binding in every affected case (venue room is strictly smaller in
all of them) and changes no expected number:
`tests/unit/test_risk.py::test_venue_exposure_caps_the_size` still approves
1,000, and `TestDuplicateVenueBoundary` still approves 10,000, 5,000 and 20,000
at its three boundaries. No assertion was weakened.

## 7. Replay and the config digest

`error_rate_window_deliveries` is a new field on `RiskLimits`, so it appears in
`Settings.model_dump()` and therefore changes `config_digest`. **This is
correct** — the window size materially affects when the platform stops trading,
so a recording made under a different window is not the same material
configuration.

The consequence is that recordings produced by builds before this commit do not
satisfy exact same-material-config replay and will be reported as a config
mismatch. That is the mechanism working as designed. **No replay code was
changed**, and no test pins a literal digest value — every digest assertion in
the suite is relative.

## 8. What must not have regressed

Preserved and re-verified statically: P5-1 committed exposure, P5-2 strategy
units, P5-3 live hard-limit backstop, P5-4 open-order projection, P5-6
cancel-before-flatten, P5-7 net and leverage headroom, P5-11 venue and position
grouping, P5-13 utilization units, all of P5-18 (fill-sequence risk, the
slippage multiplier, the recovery reserve, the strict 10,000 emergency
ceiling), and all of E1 (P5-5, P5-10, P5-12, P5-16, P5-17).

`risk/kill_switch/__init__.py`, `agents/rune/core.py`, `apps/api/app.py` and
every agent module are not in the diff. `MAX_ERROR_RATE`'s own comparison
(`error_rate <= limits.max_error_rate`) is unchanged — only the number reaching
it is now the one the limit was written for.

## 9. No threshold tuning

Unchanged: `max_error_rate` 0.25, `max_data_age_ms` 2,000, `max_clock_skew_ms`
2,000, `max_unhedged_notional` 10,000, `hedge_tolerance_notional` 500, every
exposure limit, every consensus threshold, TIDAL's deadband, NORO's semantics
and ZEPHR's economics.

One new default in the entire pass: `error_rate_window_deliveries = 200`.

## 10. Test changes

| File | Change |
| --- | --- |
| `tests/audit/test_rune_error_rate.py` | Rewritten. The lifetime-horizon premise is inverted into an assertion that the horizon is rolling; the window arithmetic, the dispatch-time recording (including the strict-bus re-raise case) and the one-outcome-per-handler rule are asserted directly against a real `InMemoryEventBus`. The concentrated-burst safety property and the huge-lifetime-counter control are kept. |
| `tests/audit/test_rune_data_time.py` | `test_the_gate_has_no_lower_bound_at_all` inverted; lower-boundary inclusivity, the interval in `detail`, the signed `observed`, and the no-clock-read property added. The TIDAL upstream-defence test is untouched. |
| `tests/audit/test_rune_loss_boundaries.py` | The two descriptive P5-15 tests became `pytest.raises(ValidationError)`; the strategy-budget one moved to root `Settings`; the hedge-tolerance rule and both new boundary controls added. The P5-14 sweep now covers every float field against `+inf`, `-inf` and NaN, plus a defaults-unchanged pin. |
| `tests/audit/test_rune_remediation_a_boundaries.py`, `tests/unit/test_risk.py` | Venue-isolation fixtures made coherent under the new rule (§6). |
| `tests/contract/test_config_validation.py` | `deliveries` added to the unit vocabulary. |

No test is skipped, xfailed, or weakened.

## 11. Deliberate omissions

- **`EXCESSIVE_LATENCY` still compares against `max_data_age_ms * 5`.** Giving
  latency its own configured threshold is a calibration question, recorded in
  E1 and still out of scope here.
- **The error-rate gate still reads one aggregate number.** Per-handler
  thresholds would be a new safety policy, not a repair of this one.
- **No new trigger, no new limit, no new buffer.** Every rule added here is
  expressed with numbers the platform already configures.

## 12. Ruff

No SIM300-shaped inversions were introduced; the assertions added compare
computed values against constants in the conventional order.

## 13. Phase 5 status after this pass

| | |
| --- | --- |
| CLOSED / externally validated | P5-1, P5-2, P5-3, P5-4, P5-5, P5-6, P5-7, P5-10, P5-11, P5-12, P5-13, P5-16, P5-17, P5-18 |
| Remediated here, external validation pending | P5-8, P5-9, P5-14, P5-15 |

Phase 5 becomes validated only if external CI reports 0 failed and every gate
PASS. Nothing in this document claims that has happened. **No tag was created
in this build.**

## 14. Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed under a test runner, and no application was
started. Every claim above is derived from the code as written, plus short
read-only static checks of the two new arithmetic surfaces — the delivery
window's rate at each documented input, `gate_data_age`'s verdict at each
boundary, and each new coherence rule at its boundary and one unit past it —
disclosed here rather than presented as test results.
