# Causal snapshot, and live economics for an open position

Two defects with one shape: **a decision made against market data that is not
the data the decision claims to be about.**

- **Base SHA:** `63d629c771fb6a10a3d682aaa71e08f5fe0d1f6f`
- **Branch:** `phase34-lifecycle-causality`
- **Production files changed:** `agents/tidal/agent.py`,
  `apps/orchestrator/orchestrator.py`. Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. The two defects

**Fix A — TIDAL scored a market it was never handed.** `Tidal.evaluate` called
`self.venue_state(leg.venue, leg.symbol)` for each leg. That method rebuilds a
venue's state from the *current* local order book and ages it against
`clock.now_ms()` at the instant of the call. But the tick had already published
a `MarketState`; the detector found the opportunity in that snapshot; the
orchestrator's `tick_time` *is* that snapshot's `created_at`. So one agent was
scoring a different market from the one the rest of the tick was reasoning
about — and doing it three times per leg, since the volatility read looked
every leg up again.

**Fix B — the monitor asked a question whose answer could not change.**
`Orchestrator._monitor` republished the *original* `Opportunity` on every tick
of an open position. Its `gross_edge_bps` and its legs' `reference_price`
values were the ones observed at detection. The agents were therefore asked
"would this trade have been good back then?", which is not a question about the
position that currently exists.

## 2. Why Fix A matters beyond tidiness

`venue_state()` is not a read — it is a re-derivation. It calls
`compute_metrics()` against the live book and calls `_quality(book, now)`
against a live clock read.

Two consequences, both real:

- **Within one tick.** TIDAL could classify a leg FRESH while the tick's own
  `MarketState` had it DEGRADED, because the two were aged at different
  instants. `confidence` is computed from `worst_quality_age`, so the number
  TIDAL published was an age nothing else in the tick agreed with.
- **Across a replay.** Replay pins its clock to the `ORCHESTRATOR_TICK` marker
  and rebuilds the snapshot there, but it cannot reproduce *dispatch latency* —
  how long the bus took to reach this subscriber in the original run. Under a
  live feed that latency is nonzero and varies. So the original run and its
  replay computed different `age_ms`, different confidences, and could reach
  different verdicts from an identical recorded market.

This is the observation-side twin of the guarantee Phase 2 Batch 1.4 already
made for time. `now_ms` fixed *when* the tick decides. This fixes *what it is
looking at*.

## 3. What Fix A changed

```python
market = self.state
if market is None:
    return None
...
leg_states = []
for leg in opportunity.legs:
    state = market.venue_state(leg.venue, leg.symbol)   # a dict lookup
    ...
    leg_states.append(state)
vol = max((s.metrics.short_vol_bps for s in leg_states), default=0.0)
```

Every market read now comes from `self.state`, the `MarketState` this tick
published. `MarketState.venue_state()` is a dictionary lookup into a frozen
snapshot — no book walk, no metric recomputation, no clock read.

The leg states are collected once and reused for the volatility term. That is
not only cheaper: it means the volatility that penalises the score and the
volatility reported in `detail[f"vol_bps_{venue}"]` are read from the same
object and cannot disagree.

**The microstructure score itself is untouched.** Same 0.7 book-imbalance /
0.3 trade-flow blend, same `side.sign`, same volatility penalty, same
`min(0.8, …)` cap, same clamp, same deadband, same confidence formula. Only the
source of the inputs changed.

## 4. No snapshot means no opinion

If `self.state` is `None`, nothing has been published yet and there is no
observation to reason about. `evaluate` returns `None` — the same answer an
unusable book already gets. TIDAL is a required agent, so a `None` makes the
consensus result incomplete and the strategy suspends.

That is deliberately **missing**, not **abstaining**. The three states stay
distinct exactly as the previous pass established them:

| State | Condition | Result |
| --- | --- | --- |
| **Missing** | no published snapshot, or a leg absent/unusable in it | `None`; required-agent completeness fails |
| **Abstaining** | snapshot read, `abs(raw) < threshold` | present and inconclusive; no numerator, no denominator |
| **Informative** | snapshot read, `abs(raw) >= threshold` | votes at full weight |

## 5. The source timestamp (TIDAL-H4 / P3-7, now closed for TIDAL)

Previously:

```python
source_data_timestamp=self.state.source_data_timestamp if self.state else None
```

`MarketState.source_data_timestamp` is the **market-wide newest** observation.
An unrelated symbol ticking, or a fresh update on one leg while the other sat
still, stamped the opinion as current while it was in fact reasoning about a
stale book. Downstream age gates read that field, so overstating freshness
there is precisely how a stale opinion passes a check designed to stop it.

Now:

```python
source_data_timestamp=market.source_data_timestamp_for(
    (leg.venue, leg.symbol) for leg in opportunity.legs
)
```

The **oldest exchange observation among this opportunity's own legs** — the
same helper, and the same reasoning, already used by the detector, by ZEPHR
and by NORO. It fails closed: a leg with no exchange observation makes the
whole result `None` rather than averaging over the hole, and `None` is what the
age gates treat as "unknown, block".

This was recorded as deferred in `docs/phase34-tidal-integration.md` §13. It is
now closed.

## 6. Why Fix B matters

`gross_edge_bps` is not decoration on the republished opportunity. It is an
input:

- **ZEPHR** builds its sizing curve against it: `build_sizing_curve(symbol,
  opportunity.gross_edge_bps, …)`. Every net-edge figure, every feasibility
  verdict, `EDGE_SURVIVES_EXECUTION` itself, is that number minus modelled
  costs.
- **TIDAL** scales its volatility penalty by it:
  `vol / max(1e-9, opportunity.gross_edge_bps) * 0.25`.

Frozen at the entry value, an opportunity whose dislocation had *entirely
closed* still presented ZEPHR with the entry edge, and ZEPHR still reported
that execution economics survived. The exit path exists to notice decay, and
it was being shown a snapshot in which decay was impossible. Continuation was
being decided on a question that could only ever be answered the same way.

## 7. What Fix B computes

```
current_buy_touch  = best ASK now on the ORIGINAL buy venue
current_sell_touch = best BID now on the ORIGINAL sell venue
reference          = (current_buy_touch + current_sell_touch) / 2
gross_edge_bps     = safe_bps(current_sell_touch - current_buy_touch, reference)
```

Touch to touch — the same reference frame the detector used and the frame
`EdgeFrame.TOUCH_TO_TOUCH` names — so the monitored edge is directly comparable
to the entry edge rather than a differently-defined number. Reading mids here
would overstate the surviving edge by a full spread.

The denominator is the midpoint of the two current touches, **not** the
consolidated reference price. A denominator only sets the scale, so the
difference is a fraction of a basis point either way; the point is that it
keeps the whole computation a function of the two venues actually holding the
position, with no qualification needed.

Everything comes from the tick's frozen `MarketState`. `_monitor_opportunity`
reads no clock and no local book.

## 8. What is deliberately NOT re-detected

**The venues.** The position exists on the venues it was opened on, and those
are the venues that have to be unwound. Re-running detection here would
retarget the trade to whichever pair is cheapest *now* and produce an edge for
a position nobody holds — which is worse than the frozen number it replaced,
because it would look plausible.

`_monitor_opportunity` does not call `detect()`, does not read or write the
detector's in-flight registry, and carries the buy leg's venue, the sell leg's
venue and both sides through untouched.

**The identity and the lifetime.** `opportunity_id`, `correlation_id`,
`created_at`, `expires_at`, `kind`, `strategy` and `symbol` all belong to the
original. The response barrier keys off the id; attribution keys off the
correlation id; and the tick-time invariant requires that an opportunity is
stamped at the tick it was **born** in and never restamped — a property
`tests/unit/test_tick_time_invariant.py` asserts directly, and which this pass
leaves strict.

| Field | Monitored copy |
| --- | --- |
| `opportunity_id`, `correlation_id`, `event_id` | preserved |
| `created_at`, `expires_at` | preserved |
| `kind`, `strategy`, `symbol` | preserved |
| leg `venue`, `symbol`, `side`, `quantity` | preserved |
| leg `reference_price` | **current touch** |
| `gross_edge_bps` | **current** |
| `source_data_timestamp` | **re-derived, oldest leg** |
| `reason_codes` | originals + `MONITOR_REPRICED` |
| `detail` | originals + current prices + `entry_gross_edge_bps` |

## 9. The original record is never mutated

`record.opportunity` is the historical record of what was detected and what the
entry decision was taken against; `_finish_attribution` reads it after the
trade closes. `_monitor_opportunity` returns a `model_copy`, and the leg list
it installs is a fresh list of fresh `OpportunityLeg` copies — `model_copy` is
shallow, so reusing the original list would have mutated the original through
it. The `detail` dict and `reason_codes` list are likewise rebuilt rather than
appended to in place.

## 10. Failing closed

`_monitor_opportunity` returns `None` when a leg is absent from the snapshot,
is not `FRESH`, or has no touch on the side that matters. `_monitor` then
cancels working orders and exits.

This is the same outcome the old code reached by a longer route — an unusable
leg made TIDAL and ZEPHR return `None`, consensus was incomplete, and
`continuation_allowed` requires `result.complete` — but it now says so directly
instead of depending on every required agent independently failing closed. A
position whose live economics cannot be observed is not a position to keep
holding.

## 11. Terminal transitions still release the detector

Verified statically. `Orchestrator.transition` calls
`self.detector.release(symbol, opportunity_id)` on entry to `CLOSED` or
`REJECTED`, and those are the only states with no outgoing transitions in
`STRATEGY_TRANSITIONS`. Every exit route terminates there:

- `MONITORING → EXITING → CLOSED` via `_advance_exit`. Its two early returns
  are "not yet" rather than "never": one waits for live orders, the other
  retries a partially-filled exit while attempts remain. Once neither holds it
  reaches `transition(record, CLOSED)` unconditionally — including on the
  out-of-retries path that hands a residual to OKAPI.
- `_submit_exit` transitions straight to `CLOSED` when there is nothing left
  to close.
- `_advance_execution` transitions to `CLOSED` when nothing filled.

The new fail-closed branch enters `EXITING`, which is inside that same set. No
route was added that can strand `detector.active`.

## 12. Determinism and replay

Both fixes are strictly deterministic and both *increase* replay fidelity.

- Fix A removes a live-clock read and a book re-derivation from the opinion
  path, which is exactly the class of nondeterminism replay could not
  reproduce.
- Fix B is a pure function of the frozen `MarketState` and the record's
  opportunity.

No randomness, no network call, no LLM call, no background task, no new id
generation — `model_copy` preserves `event_id`, so the republished payload
keeps the identity it had before this pass.

**Config digest:** unchanged. No configuration field was added, removed or
re-typed, so recordings made under `63d629c` still qualify for exact
same-config replay on the settings digest. The `VERSION` bump to `tidal-0.3` is
recorded in each opinion's `model_version` and is what distinguishes the two
semantics in a stored timeline.

## 13. What was NOT changed

- **Consensus.** Weights (TIDAL 1.4, NORO 1.5, ZEPHR 1.5, LUMEN 0.5), entry
  threshold 0.60, exit threshold 0.45, required-agent list, hysteresis. No
  timer exit was added. `strategies/consensus/` is untouched.
- **`TidalConfig.informative_signal_threshold = 0.10`** and the whole deadband
  mechanism.
- **NORO** abstention, **ZEPHR** formulas, **RUNE** limits.
- **Execution.** No `PaperExecutor` change, no maker/taker change, no
  fill-probability change. `execution/` is untouched.
- **Simulation.** Same price process, same venue count. `simulation/` is
  untouched.
- **Detection.** `strategies/cross_venue/detector.py` is untouched.
- **Paper boundary.** No API keys, no authenticated endpoints, no real
  executor, no live order submission.

## 14. Tests

Added `tests/unit/test_lifecycle_causality.py`:

*Fix A* — the opinion reflects the published snapshot even after the underlying
books are moved hard in the opposite direction (the decisive test: it recreates
the window a live feed occupies between `publish_state()` and dispatch); no
published snapshot yields no opinion; the verdict is identical with the clock
parked 0 ms, 5 s and 9,000 s past the snapshot; the reported age is the
snapshot's age and does not grow when the clock does; structural guards that
neither `evaluate` nor `microstructure_metrics` can quietly go back to the
rebuild-and-re-age path.

*The stamp* — with a leg at `T-500`, a leg at `T` and an unrelated same-symbol
venue at `T`, the opinion is stamped `T-500`, is explicitly *not* the
market-wide newest, reports `data_age_ms == 500`, and is unaffected by the
presence of the third venue.

*Fix B* — a dislocation that has fully closed republishes an edge of ~0 against
an entry edge of 20 bps; an inverted market republishes a negative edge; a
surviving edge is still reported; the touches used are ask-on-buy-leg and
bid-on-sell-leg rather than mids; reference prices move; `entry_gross_edge_bps`
and `MONITOR_REPRICED` are recorded; the source timestamp is re-derived.

*No retargeting* — a third venue two dollars cheaper does not enter the legs
and does not move the monitored edge; sides are preserved; structural guards
that `_monitor` and `_monitor_opportunity` neither re-detect nor touch the
in-flight registry nor read a clock, and that `_monitor` publishes the repriced
copy stamped at the current tick.

*Immutability* — a full `model_dump()` of the original is byte-identical before
and after; the leg list, each leg object, the `detail` dict and the
`reason_codes` list are all distinct objects; identity and lifetime fields are
carried through.

*Fail-closed* — a DEGRADED, STALE or UNAVAILABLE leg, a missing leg and an
empty snapshot all yield no monitored opportunity, and `_monitor` exits rather
than holding.

Migrated `tests/unit/test_tidal_abstention.py`: `build_tidal` now installs a
real `MarketState` instead of monkeypatching the `venue_state` accessor. That
patch had become meaningless — the opinion path no longer calls it, so a test
that kept patching it would have gone on passing while measuring nothing. The
model-version assertion moved to `tidal-0.3`. **No deadband assertion was
weakened**; the substitution point moved one layer out, onto the production
path.

Left strict, unmodified: `tests/unit/test_tick_time_invariant.py` (including
the outcome-invariance-under-mid-tick-drift test and the "must trade for this
to prove anything" guard), the natural round-trip tests, NORO's production
behaviour suite with its `>= 10 distinct opportunities` assertion, the
missing-required-agent tests, and the two-venue calibration probe with its
`assert entry_count > 0` intact.

## 15. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

No `pytest`, `ruff`, `mypy`, container, replay or application start was
executed. Every expectation stated here was derived by reading the
implementation.

The specific things external validation should watch:

1. Whether `_monitor`'s live edge changes how often positions exit, and how
   quickly. This is the first pass in which continuation can genuinely fail on
   economics rather than only on agent availability.
2. Whether the leg-scoped `source_data_timestamp` causes TIDAL opinions to be
   rejected by an age gate that the market-wide-newest value was previously
   getting past. If so, that gate is now doing its job for the first time.
3. Whether `tests/integration/test_pipeline.py::TestAttribution` still sees
   trades — carried forward from the previous pass as the highest-risk unknown,
   and still not statically determinable.
