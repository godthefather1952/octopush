# Phase 5 Remediation E1 — kill-switch hardening and recovery

Five findings, fixed: **P5-5** (a cleared kill switch left the executor
latched), **P5-10** (`EXCESSIVE_LATENCY` was fed data age), **P5-12** (a
crashing safety predicate failed open), **P5-16** (automatic safety events
read the live clock), and **P5-17** (a vestigial `AGENT_FAILURE` mapping).

Every one is about the emergency boundary itself rather than about exposure
arithmetic. Nothing in RUNE's sizing changed.

- **Base SHA:** `6e4fed11ff8a16794ff834f004fd715a7d1c91aa`
- **Branch:** `phase5-rune-remediation-e1`
- **Production files changed:** `risk/kill_switch/__init__.py`,
  `apps/orchestrator/orchestrator.py`, `apps/api/app.py`. Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Baseline

`phase5-rune-remediation-d` @ `6e4fed1`, verified exact and clean, local equal
to remote. `phase5-rune-remediation-e1` was created directly from that commit —
no merge, no rebase, no tag, no force-push.

## 2. External validation — CI #41

| | |
| --- | --- |
| Full suite | 3018 passed / **8 failed** / 2 skipped |
| Python 3.11 unit + contract | 1382 passed / 126 skipped / **0 failed** |
| Mypy core, paper boundary, Redis + PostgreSQL contract pre-check | PASS |
| Ruff | one audit-only SIM300 |

## 3. P5-18 is closed

**P5-18 — CLOSED / EXTERNALLY VALIDATED.**

All three acceptance surfaces are green: the direct asynchronous-fill and
recovery-headroom tests, the strict first-breach diagnostic, and all four
normal-platform canaries. The ordinary platform no longer enters
`RISK_LIMIT_BREACH` during the validated default runs.

Both `docs/phase5-rune-audit.md` and `docs/phase5-rune-remediation-d.md` are
updated. No P5-18 production code was touched in this pass.

## 4. P5-5 — a cleared switch left the executor latched

`RECONCILIATION_MISMATCH` and `UNEXPECTED_POSITION` set
`KillSwitchState.execution_disabled`, and `_protect` latches that onto
`PaperExecutor.execution_disabled`. `KillSwitch.clear()` builds a fresh state
where the flag is False — but nothing unlatched the executor. The switch
reported itself cleared while every submission kept being rejected for the rest
of the process, and no operator action existed to undo it.

### The coordinated path

`Orchestrator.clear_kill_switch(reason)`:

```python
now = self.clock.now_ms()
state = await self.kill_switch.clear(reason, now_ms=now)
self.state.kill_switch = state
self.veska.executor.execution_disabled = False
```

**The kill switch was deliberately not given a reference to the executor.** It
owns safety state and trigger evaluation; the orchestrator owns
application-side effects, and only the component that applied the latch can
undo it. Handing `KillSwitch` a `PaperExecutor` would invert that boundary —
pinned by a test that checks the module's imports and constructor
dependencies rather than merely the absence of a word, since the module's
prose necessarily explains which component owns the latch it cannot reach.

## 5. Recovery is manual, and stays manual

Nothing calls `clear_kill_switch` automatically — not `_protect`, not
`_manage`, not a heartbeat, not reconciliation, pinned by a test that scans
those methods. A condition that stopped trading should be understood before
trading resumes.

If the unsafe condition still holds, the next protected tick engages the switch
again. That is the correct outcome, not a failed recovery, and it has its own
behavioural test: engage, tick, clear, re-inject, tick, and the switch is
engaged again.

## 6. The API clear endpoint

`POST /api/kill-switch/clear`, routed through
`platform.orchestrator.clear_kill_switch` — **never**
`platform.kill_switch.clear`, which would restore only half the state. It
returns `engaged`, `trading_allowed`, `execution_disabled` and `triggered_by`.

`POST /api/kill-switch` is unchanged and still only stops things.
`test_kill_switch_endpoint_only_stops_things` remains semantically valid for
it, untouched.

`test_there_is_no_endpoint_that_places_a_trade` pinned the mutating-route set
as exactly `{"/api/kill-switch"}`, so its premise had to move. The property it
exists for is unchanged and now stated explicitly: every mutating route is a
kill-switch control — one engages, one acknowledges — and neither can open,
size, route or submit anything.

## 7. P5-10 — the latency trigger now receives latency

`_protect` fed `max(s.age_ms)` into `max_latency_ms`: how long since a venue
last said anything. That made `EXCESSIVE_LATENCY` a second, looser copy of the
staleness check wearing a latency-shaped name, and left genuine transport
latency unmonitored. It now reads `latency_ms` — `received_ts - exchange_ts`,
floored at zero — which is what the name promises.

**Data age keeps its own protections**, unchanged and pinned: TIDAL's quality
classification, RUNE's `MARKET_DATA_FRESH` gate, and `market_data_ok` feeding
`MARKET_DATA_OUTAGE`.

**The threshold is untouched.** The predicate still compares against
`max_data_age_ms * 5`. P5-10 is an input-semantics finding; giving latency its
own configured threshold is a calibration question and does not belong bundled
into a safety fix.

## 8. P5-12 — a predicate that raises now fails closed

The exception path logged and `continue`d, so a crashing predicate silently
stopped protecting while the platform carried on trading believing it had one
more safety condition than it did — fail-open on the emergency boundary.

It now engages `SYSTEM_HEALTH_FAILURE`. That is the honest classification: a
mandatory check that cannot be evaluated has not been satisfied, the same
fail-closed rule RUNE applies to an UNKNOWN gate. No new trigger was invented.

Two properties are pinned explicitly:

- **No recursion.** `engage` applies state and actions directly; neither the
  failing predicate nor `evaluate` is called again, so this stays safe even
  when the predicate that raised was `SYSTEM_HEALTH_FAILURE` itself — which
  has its own test.
- **The loop continues.** One broken predicate must not take the remaining
  ones with it; a second trigger in the same evaluation still fires.

## 9. P5-16 — automatic safety events use logical tick time

`engage`, `clear` and `evaluate` now accept `now_ms: Millis | None = None`,
resolved as `self.clock.now_ms() if now_ms is None else now_ms`. `evaluate`
threads one instant into every engagement it produces, including P5-12's
fail-closed one, and `_protect` passes `self.tick_time`.

An automatic engagement happens because of state the orchestrator observed
during one tick, so it is now stamped at that tick's instant rather than at
whatever the live clock reads by publish time. The economic actions never
depended on this; the recorded causal ordering of a safety event did, and a
replay could order it differently from the run that produced it.

The clock remains the fallback for a genuinely manual operator action, which
happens outside any tick. `clear_kill_switch` reads it once, at the
orchestration boundary, and threads that value in — so the state reset and its
published event share an instant.

## 10. P5-17 — the vestigial mapping is gone

`AGENT_FAILURE` had actions defined, no predicate and no caller. Its action set
was byte-identical to `SYSTEM_HEALTH_FAILURE`'s, whose predicate already covers
a required component ceasing to be HEALTHY, and a required agent that answers
with nothing is separately caught by consensus completeness. Nothing was
unprotected; the entry named a response another trigger already delivered.

Removed rather than given a predicate — inventing one would have meant
inventing a safety condition nobody had defined. The audit now guards against
its reintroduction without one, and the unreachable-mapping inventory is
**empty**: every action mapping except `MANUAL`, which is operator-invoked by
design, has a predicate that can reach it.

## 11. Ruff

`tests/audit/test_rune_leg_fill_risk.py` — `assert RESERVE > drift_notional`
became `assert drift_notional < RESERVE`. SIM300 only; no arithmetic or
threshold change.

## 12. Deferred to E2

| ID | |
| --- | --- |
| **P5-8** | lifetime vs rolling error rate — `Orchestrator._error_rate` untouched |
| **P5-9** | future-timestamp defence — `gate_data_age` untouched |
| **P5-14** | `+Infinity` accepted by `RiskLimits` |
| **P5-15** | remaining config coherence |

Their audit assertions remain strict and are expected to keep failing.

## 13. What must not have regressed

Preserved and re-verified statically: P5-1 committed exposure, P5-2 strategy
units, P5-3 live hard-limit backstop, P5-4 open-order projection, P5-6
cancel-before-flatten, P5-7 net and leverage headroom, P5-11 venue and position
grouping, P5-13 utilization units, and all of P5-18 — fill-sequence risk, the
slippage multiplier, the recovery reserve and the strict 10,000 emergency
ceiling.

Unchanged: `max_unhedged_notional`, `hedge_tolerance_notional`,
`RISK_LIMIT_BREACH` and its action set, its immediate confirmation and its
manual latch, `UNEXPECTED_POSITION` at the 3x level, every `RiskLimits`
default, consensus thresholds, TIDAL, NORO, ZEPHR, the simulation and the
execution fill model. `agents/rune/core.py`, `risk/limits/__init__.py` and
`core/config/settings.py` are not in the diff.

## 14. Testing status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed under a test runner. Every claim above is
derived from the code as written.
