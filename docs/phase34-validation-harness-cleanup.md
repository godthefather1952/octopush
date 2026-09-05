# Final validation harness cleanup

Two tests were failing. Neither was reporting a production defect: one was
corrupting its own premise, the other was asserting something the platform
never promised.

- **Base SHA:** `28500e6adabd83c9d4f30ce6a6cfc93e82d90d3e`
- **Branch:** `phase34-lifecycle-causality`
- **Production changes:** **NONE.** Only `tests/` and `docs/`.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. External validation baseline

External validation of `28500e6` produced:

| | |
| --- | --- |
| Full suite | **2292 passed, 2 failed, 2 skipped** |
| Python 3.11 unit/contract | 1380 passed, 1 failed, 126 skipped |
| Ruff, Mypy core, paper boundary, Redis contracts, PostgreSQL contracts | **pass** |

The two remaining failures:

1. `tests/unit/test_tick_time_invariant.py::TestOutcomeIsIndependentOfMidTickClockDrift::test_same_trades_whatever_the_mid_tick_drift`
2. `tests/failure/test_failures.py::TestAgentFailures::test_a_missing_required_agent_stops_the_strategy`

## 2. What the lifecycle fixes are now validated to have done

The failures that motivated the previous pass are gone, and gone by
integration evidence rather than by argument:

- the natural round trip completes,
- `TRADE_ATTRIBUTION` is emitted and the scorecard receives trades,
- the event-stream lifecycle completes,
- NORO's production-behaviour run gets normal opportunity turnover,
- trading remains restored.

Nothing in this pass touches trading behaviour. The frozen-snapshot
evaluation, the leg-scoped TIDAL timestamp, the 0.10 microstructure deadband,
NORO's abstention, ZEPHR's cost model, current-edge monitoring, the consensus
weights, the 0.60/0.45 thresholds, RUNE, VESKA, the paper executor and the
simulation are all exactly as `28500e6` left them.

## 3. Failure 1 — the clock test was changing its own inputs

`ClockAdvancingFeed` was attached as **unconditional bus middleware**: it
advanced the shared `ManualClock` on *every* publication, market inputs
included.

`SimulatedMarketDriver.step()` reads the clock once and stamps every message
of that step from it:

```python
now = self.clock.now_ms()
...
message.exchange_ts = now
message.received_ts = now
```

So the clock position at the top of each `step()` depended on how many events
the previous iteration had published — which is a function of `step_ms`. The
runs being compared were therefore fed **different markets**. Their
`exchange_ts`, `received_ts`, rolling trade-flow windows, short-volatility
windows and freshness ages all diverged, and the reported difference —
baseline BTC BUY quantity `0.225914121` against `0.225935538` at
`step_ms = 40`, with fill prices and P&L following — was the harness's own
doing.

That is not "same market, different mid-tick scheduling". It is "different
timestamped market input stream", which the invariant says nothing about.

The simulator's stamping is a deliberate production contract and was not
touched. Neither was `venues/simulated.py`, `simulation/market.py`, TIDAL's
metric windows, `ManualClock`, or any production timestamp semantics.

## 4. The correct mid-tick drift boundary

`MidTickClockAdvancer` replaces `ClockAdvancingFeed`. It is still bus
middleware, but it only moves the clock while a scoped context is held:

```python
clock.set(origin + (i + 1) * TICK_PERIOD_MS)
await platform.step_market(1)          # input delivery — no drift

with drift.active():                   # orchestrator processing — drift
    await platform.orchestrator.tick()
    await platform.bus.drain()
```

A scoped context rather than an event-type allowlist, so it cannot go stale as
new internal event types are added — and so `BOOK_SNAPSHOT`, `BOOK_DELTA`,
`TRADE_PRINT`, `VENUE_CONNECTED` and `VENUE_DISCONNECTED` are excluded by
construction rather than by enumeration.

Enabling it around the whole of `tick()` is still "after the snapshot
boundary": `tick()` publishes nothing before `build_state()` has already read
the clock, so `market.created_at`, `tick_time` and the `ORCHESTRATOR_TICK`
marker are the same instant in every run, and the first publication drift can
act on is the `MARKET_STATE` that follows.

### Two things this required, both stated plainly

**An absolute tick schedule.** Scoping the drift is necessary but not
sufficient. The clock is shared and monotonic, so drift injected inside tick
*i* still shifts where tick *i+1*'s `step_market()` lands under the old
relative `clock.advance(100)`. Each market step is therefore pinned to
`origin + (i + 1) * TICK_PERIOD_MS`. With no drift this is identical to
`advance(100)`, so the baseline run is bit-for-bit the run this test has
always used as its reference.

**A per-tick drift budget.** `ManualClock.set()` refuses to move backwards —
correctly; a clock that rewinds is not a clock — so drift injected inside a
tick cannot be unwound afterwards, and the next absolute boundary has to still
be in the future. `TICK_DRIFT_BUDGET_MS = 80` against a 100 ms period
guarantees that. The alternative, unbounded drift, needs a tick period wider
than the worst case (tens of publications × 40 ms, so seconds), which would
push order deadlines and opportunity expiry past their limits and stop the
scenario trading at all.

The cap does not make the three cases equivalent. At a 80 ms budget,
`step_ms = 1` lands up to eighty small movements spread through the tick,
`step_ms = 7` eleven, and `step_ms = 40` two large ones — different totals and
very different distributions. The invariant under test is precisely that
*when* the clock moves inside a tick does not change what the tick decides.

This is a change in what the harness injects, not in what it asserts, and it
is forced by the monotonic clock rather than chosen. It is called out here so
that external validation reads the new numbers with the right expectations.

## 5. The input-fingerprint guard

The premise is now asserted rather than assumed. Each run collects every
`MARKET_INPUT_TYPES` event and reduces it to what is economically meaningful:

```
(type, ts_ms, source, schema_name, venue, symbol, exchange_ts, received_ts, sequence)
```

Minted identifiers are excluded — they are nondeterministic by design and say
nothing about what the market did.

Before any economic comparison, every drift run's fingerprint must equal the
baseline's. If it does not, the test says so in those terms: the runs are not
comparable and the harness, not the platform, is at fault. This pins

> SAME INPUTS + DIFFERENT MID-TICK CLOCK MOVEMENT = SAME ECONOMIC OUTCOME

and stops the harness silently corrupting its own premise again.

The other three tests in the module were migrated to the same gated advancer
for the same reason, and each now asserts that drift was actually injected.
`test_every_economic_stamp_in_a_tick_equals_the_tick_time` keeps its
`feed.advances > 100` proof of substantial mid-tick movement and runs
unbounded — it compares stamps within one run, so it needs neither a fixed
schedule nor a budget.

## 6. Failure 2 — a terminality assumption, not a safety hole

The injection is correct and the platform's behaviour is correct. The
assertion was:

```python
assert all(record.rejected_reason == "CONSENSUS_INCOMPLETE" for record in new_records)
```

over *every* opportunity detected after NORO was suppressed. That assumes each
one had already reached its response deadline by the time the loop stopped.

It has not. `consensus.agent_response_timeout_ms` is 1,000 ms and a tick is
100 ms, so opportunities detected in the last ten ticks are legitimately still
in `AGENTS_EVALUATING`. They are *waiting* for the agent that will never
answer — which is the designed behaviour, and safe: a waiting opportunity has
no intent, no risk decision and no orders.

The assumption only held while opportunity turnover was broken. Restoring
turnover exposed it. It was always a latent bug in the test.

## 7. Pending versus terminal

The records are now partitioned, and each half carries the assertion that
actually applies to it:

| | Requirement |
| --- | --- |
| **terminal** (`REJECTED`) | non-empty; every one `rejected_reason == "CONSENSUS_INCOMPLETE"` |
| **pending** (`AGENTS_EVALUATING`) | `0 <= now - opportunity.created_at < agent_response_timeout_ms` |
| **anything else** | fails the test |

That last row is the point: `terminal + pending` must account for *all* new
records, so `AUTHORIZED`, `EXECUTING`, `HEDGING`, `RECONCILING`, `MONITORING`,
`EXITING` and `CLOSED` are each a failure.

No other terminal reason is accepted. The response timeout (1,000 ms) is half
the opportunity lifetime (2,000 ms), so a normally progressing missing-agent
opportunity reaches `CONSENSUS_INCOMPLETE` well before expiry; a terminal
record stopped for anything else — an expiry, say — would mean the platform
never noticed NORO was gone, and remains a failure.

## 8. Safety assertions preserved, and strengthened

Every new record, pending or terminal, must satisfy:

```python
record.intent is None
record.decision is None
not record.order_ids
record.filled_notional == 0.0
record.realized_pnl == 0.0
AgentId.NORO not in state.opinions_for(record.opportunity.opportunity_id)
```

And the direct proof, added in this pass: a subscriber captures every
`TRADE_INTENT`, `RISK_PASS`, `EXECUTION_PLAN`, `PAPER_ORDER_CREATED` and
`PAPER_FILL` published during the failure window, and **no** correlation id
among them may belong to any opportunity detected during it.

Scoped by correlation id rather than by a global before/after count, for two
reasons. First, hedging and the unwinding of positions opened *before* the
injection are expected to continue — refusing to close an existing position
because an intelligence agent is down would be the opposite of failing safely.
Second, `oms.orders` and `account.fill_log` are both compacted during a long
run (MARIN seals a verified prefix of the ledger; the OMS drops terminal
orders), so a raw count would be measuring compaction as much as trading. The
event stream is never compacted.

## 9. Production changes

**NONE.**

No file under `agents/`, `apps/`, `core/`, `execution/`, `risk/`, `replay/`,
`simulation/`, `strategies/` or `venues/` was modified. No threshold, weight,
formula or configuration value was changed. No skips or xfails were added.

## 10. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

No `pytest`, `ruff`, `mypy`, container, replay or application start was
executed. Every expectation here was derived by reading the implementation.

What external validation should watch:

1. Whether the fingerprint assertion passes. If it fails, the remaining
   coupling between injected drift and market-input stamping is somewhere this
   static analysis did not reach, and the economic comparison after it should
   be disregarded until it does.
2. Whether the baseline still trades. The absolute schedule is identical to
   the old relative one in the zero-drift case, so it should be unchanged, but
   that is reasoning rather than measurement.
3. Whether the missing-agent test's `pending` partition is empty or not. Either
   is correct; a non-empty one is the case the old assertion could not express.
