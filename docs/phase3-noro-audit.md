# Phase 3 — NORO fair-value agent audit

**Audit only. No production code was changed.**

- **Phase 2 base SHA:** `26c561dca0894ed8cf2c39cbaf77cd6082552d79`
- **Branch:** `phase3-noro-audit`
- **Scope:** `agents/noro/` and everything that feeds or consumes it.

---

## 1. NORO's mission

NORO is meant to answer a question the cross-venue detector cannot:

> Does this apparent cross-venue price discrepancy represent a genuine
> **valuation** discrepancy?

which is a different question from "venue A's ask is below venue B's bid".

It is a **required** consensus agent carrying **weight 1.5 of 4.9** — the joint
heaviest, alongside ZEPHR. A missing NORO opinion suspends the strategy; a
negative one can veto an otherwise unanimous entry. So NORO is only worth its
weight if its answer is not already implied by the question.

**The audit's central finding is that, for the two-venue configuration this
platform actually runs, it is implied — provably, and confirmed in production.**

---

## 2. Architecture

```
venue feeds
   ↓  BOOK_SNAPSHOT / BOOK_DELTA / TRADE_PRINT / VENUE_CONNECTED|DISCONNECTED
TIDAL  (LocalOrderBook → compute_metrics)
   ↓  BookMetrics: mid, microprice, spread, depth buckets {1,5,10,25} bps
VenueMarketState  (+ quality, exchange_ts, latency)
   ↓
MarketState  (venues dict keyed "venue:symbol", consolidated views)
   ↓  MARKET_STATE   ── published once per tick, from Orchestrator._observe()
Noro.on_market_state()      ← rebuilds self.fair_values from scratch
   ↓  compute_fair_value(symbol, states_for(symbol), NoroConfig)
FairValue(fair_value, total_liquidity, venues[VenueValuation])
   ↓
CrossVenueDetector.detect()  ── same MarketState, same tick
   ↓  OPPORTUNITY_DETECTED
Noro.evaluate(opportunity, now_ms)
   ↓  AgentOpinion(NORO)
SystemState.put_opinion()   ← keyed by (correlation_id, agent)
   ↓
ConsensusEngine.combine()   ← weight × confidence × signal
   ↓  TradeIntent → RUNE → VESKA → paper execution
```

### Stage contracts

| Stage | In | Out | Fails how |
|---|---|---|---|
| `venue_price` | `VenueMarketState`, weight | `float \| None` | `None` when `mid is None` |
| `usable_liquidity` | state, window bps | `float` | 0.0 if either side empty |
| `compute_fair_value` | symbol, `list[VenueMarketState]`, config | `FairValue \| None` | `None` when no usable, priceable, liquid venue |
| `Noro.on_market_state` | `MarketState` | — (mutates `fair_values`) | symbol simply absent |
| `Noro.evaluate` | `Opportunity`, `now_ms` | `AgentOpinion \| None` | `None` if no fair value, or any leg venue absent |
| `ConsensusEngine` | opinions | `ConsensusResult` | `complete=False` when a required agent is missing |

Timestamp semantics, freshness and quality gates are covered in §7 and §8.

---

## 3. Formula inventory (verified against the code)

Every formula in the Phase 3 brief was checked line by line. **All were
accurate**; none needed correcting.

```
venue_price      = mid·(1−w) + microprice·w,  w = clamp(microprice_weight, 0, 1)
                   (falls back to mid when microprice is None)

usable_liquidity = min(bid_depth[key], ask_depth[key])   key = f"{window_bps:g}"
                   → min(bid_depth_notional, ask_depth_notional)  on a dict MISS

FV               = Σ(price_v · liquidity_v) / Σ(liquidity_v)
weight_v         = liquidity_v / Σ(liquidity)
deviation_bps_v  = (price_v − FV) / FV · 10_000

confirmation     = −deviation      for a BUY leg
                 = +deviation      for a SELL leg
confirmed_edge   = min(confirmations) + mean(confirmations)
signal           = clamp(confirmed_edge / saturation_bps, −1, +1)

confidence       = clamp(0.35
                         + 0.45·min(1, total_liquidity/250_000)
                         + 0.20·min(1, venues/2), 0, 1)
```

`NoroConfig`: `liquidity_window_bps` (default 10.0, `gt=0`),
`microprice_weight` (0.5, `[0,1]`), `ttl_ms` (2000, `gt=0`),
`saturation_bps` (15.0, `gt=0`). **None of the confidence constants is
configurable.**

`DEPTH_BUCKETS_BPS = (1.0, 5.0, 10.0, 25.0)` — the only windows that ever hit a
measured bucket.

---

## 4. Existing test coverage (before Phase 3)

**There was no dedicated NORO suite.** `tests/unit/test_noro.py` does not
exist. The whole of NORO's pre-Phase-3 coverage was seven tests inside
`tests/unit/test_pricing.py::TestFairValue`:

1. equal liquidity gives the average
2. a deep venue pulls fair value toward it
3. deviations are signed
4. unusable venues are excluded
5. no usable venue → `None`
6. `venue_price` blends mid and microprice
7. `usable_liquidity` takes the thinner side

All seven test `compute_fair_value` / `venue_price` / `usable_liquidity`.
**Zero tests covered the `Noro` agent itself** — no coverage of `evaluate`,
the signal formula, confidence, reason codes, health, TTL, leg handling, or
consensus interaction.

### Coverage matrix

| Invariant | Existing? | New audit test | Result |
|---|---|---|---|
| fair value arithmetic | partial (2 tests) | `test_noro_fair_value_invariants` | holds |
| convex hull / weights sum to 1 | no | same | holds |
| scale invariance (price, liquidity) | no | same | holds |
| order / label invariance | no | same | holds |
| numerical safety, 500 venues | no | same | holds |
| liquidity weighting | 1 test | `test_noro_liquidity_window` | **P3-1, P3-9** |
| bucket/window domain | no | same | **P3-1** |
| microprice | 1 test | `test_noro_microprice_and_config` | holds; **P3-10** |
| quality filtering | 1 test | `test_noro_freshness_and_health` | holds |
| stale-state lifecycle | no | same | holds |
| symbol isolation | no | same | holds |
| one-venue behaviour | no | same | **P3-6** |
| no-contributor behaviour | 1 test | invariants | holds |
| signal sign symmetry | no | `test_noro_signal_semantics` | holds |
| weakest leg | no | same | **P3-2** |
| saturation semantics | no | same | **P3-3** |
| confidence | no | `test_noro_confidence` | **P3-8** |
| source timestamp | no | freshness suite | **P3-7** |
| health semantics | no | same | **P3-6** |
| consensus influence | no | `test_noro_consensus_impact` | quantified |
| two-venue information value | no | `test_noro_information_value` | **P3-4** |
| 3+ venue information value | no | same + script | **P3-4** |
| self-inclusion | no | same | **P3-5** |
| causal alignment | no | `test_noro_causal_alignment` | safe (see §11) |
| production distribution | no | `test_noro_production_behaviour` | **P3-4** |
| replay | Phase 2 | unchanged | 130 pass |
| performance / state growth | no | script + config suite | linear, bounded |

**363 new audit tests**, all passing, across 10 files.

---

## 5. Fair-value invariants (Section 5)

| # | Invariant | Verdict |
|---|---|---|
| A | identical venues → FV = common price, deviations 0 | **HOLDS** |
| B | min(prices) ≤ FV ≤ max(prices) | **HOLDS** |
| C | Σ weights = 1, all > 0 | **HOLDS** (1–500 venues) |
| D | liquidity monotonicity | **HOLDS** |
| E | input-order invariance | **HOLDS** (tuple order follows input; documented) |
| F | price-scale invariance (K = 0.01…10⁴) | **HOLDS** |
| G | liquidity-scale invariance | **HOLDS** (but `total_liquidity` scales → confidence moves) |
| H | zero contributors → `None` | **HOLDS** |
| I | one contributor → own price, deviation 0 | **HOLDS**, and tautological (see P3-6) |
| J | zero/one-sided liquidity excluded | **HOLDS** |
| K | no NaN/inf/negative weight at 10⁻¹²…10¹⁵ | **HOLDS** |
| L | deterministic repeatability | **HOLDS** (frozen dataclass equality) |

Metamorphic (Section 29): adding a venue at FV moves nothing; a tiny outlier
has a tiny effect; a huge one dominates; removing an excluded venue is inert;
venue labels are irrelevant; no input, config or list is mutated. All hold.

**Duplicate venues (Section 31):** `compute_fair_value` takes a list and does
**not** deduplicate, so a repeated venue would double its weight. Production
cannot reach this: `MarketState.venues` is a dict keyed `venue:symbol`, so
`states_for()` yields at most one state per venue. Recorded as a property of
the function, not a defect.

---

## 6. Findings

### P3-1 — Liquidity-window bucket fallback discontinuity · **HIGH**

`NoroConfig.liquidity_window_bps` is a `float` with only `gt=0`. TIDAL
publishes exactly four buckets. On a dict miss, `usable_liquidity` falls back
to **whole-book depth** rather than interpolating or failing.

Measured, on a venue with $5,000 near the touch and $1,000,000 further out:

| window | usable liquidity |
|---|---|
| 10.0 (bucket) | 5,000 |
| 10.001 | 1,005,000 |

**a 201× jump for a 0.001 bps configuration change.** The sequence is also
non-monotone: a *wider* window (25 bps) reports *less* liquidity than a
narrower off-bucket one (10.001 bps), because two different quantities are
being reported under one name. A miss is also indistinguishable from a
genuinely empty bucket.

Economic consequence, measured end to end on a two-venue market:

| | window 10.0 | window 10.001 |
|---|---|---|
| venue A weight | 0.0099 | 0.7697 |
| fair value | 100.0990 | 100.0230 |
| deviation A | −9.891 bps | −2.303 bps |
| deviation B | +0.099 bps | +7.695 bps |
| NORO signal | +0.340 | +0.487 |
| confidence | 1.000 | 1.000 |

Which venue anchors fair value inverts completely. The **signal** swing is
damped to +0.147 by `min + mean` (see P3-4), and confidence saturates in
both — which is why this is HIGH rather than CRITICAL. Per bps of input
change it is ~100× the sensitivity of an actual market move.

*Mitigation:* the default (10.0) is on a bucket, so a stock deployment is
unaffected. *Tests:* `test_noro_liquidity_window.py`.

### P3-2 — "Weakest leg governs" is false · **HIGH**

The source comment claims *"the weakest leg governs, so a single rich venue
cannot carry the trade"*. The implementation is `min + mean`, so a strong leg
outvotes a contradicting one up to a computable threshold: with two legs the
edge stays positive while the weak leg exceeds **−⅓ of the strong leg**.

`[-1, +10]` → min −1, mean +4.5, **edge +3.5**: one leg is on the wrong side of
fair value and NORO votes for the trade.

Reachable from real market state, not just hand-entered vectors. Three venues
at 100.00 / 100.20 / 100.50 (liquidity 50k / 50k / 40k) give FV 100.2143, so
the SELL leg sits *below* fair value:

- deviation A −21.38, deviation B −1.43
- edge +8.55, **signal +0.570**, confidence 0.802
- reason codes: `FAIR_VALUE_CONFIRMS_DISLOCATION` **and**
  `LEG_AGAINST_FAIR_VALUE` simultaneously

*Consequence:* a materially positive vote (0.57 × 0.80) carrying NORO's full
weight, on a trade one of whose legs the valuation contradicts.
*Tests:* `test_noro_signal_semantics.py::TestH2_*`.

### P3-3 — `saturation_bps` semantics mismatch · **MEDIUM**

Documented as *"deviation, in bps, at which the signal saturates to |1|"*. For
symmetric confirmations the edge is `2X`, so `saturation_bps = 15` saturates
at a **7.5 bps per-leg deviation**. Measured through the real agent: two
venues 15 bps apart give per-leg deviations of ±7.494 and signal 0.9993.

The parameter actually divides the aggregate `min + mean`, whose two-venue
range is `[G/2, G]` for a gap `G` — so its effective meaning also depends on
how balanced the venues' liquidity is. *Tests:*
`test_noro_signal_semantics.py::TestH3_*`.

### P3-4 — Two-venue fair value cannot contradict the detector · **HIGH**

The most important finding.

**Proof.** The detector emits an opportunity only when
`sell_bid − buy_ask > 0`, i.e. the two venues' quote intervals are **disjoint**
with the buy venue's entirely below. `venue_price` is a convex blend of `mid`
and `microprice`, and both lie inside `[best_bid, best_ask]` (the microprice is
`(bid·ask_size + ask·bid_size)/(bid_size+ask_size)`, a convex combination).
Therefore `price_buy < price_sell` always; fair value, a convex combination of
the two, lies strictly between them; both confirmations are strictly positive.

**Closed form.** With weights `w_A, w_B` and gap `G` bps:

```
confirmations = (w_B·G, w_A·G)
mean          = G/2                     ← independent of liquidity entirely
edge          = G·(min(w_A, w_B) + ½)   ← spans exactly [G/2, G]
```

So **half of NORO's two-venue edge is the detector's own price gap**, and its
entire independent contribution is a factor-of-two modulation.

**Measured** (real detector + real agent, seeded random books):

| venues | opportunities | rejected | rate |
|---|---|---|---|
| 2 | 25,027 | 0 | **0.000%** |
| 3 | 35,311 | 13 | 0.037% |
| 4 | 38,699 | 6 | 0.016% |
| 5 | 39,650 | 0 | 0.000% |
| 8 | 39,994 | 0 | 0.000% |

Structured grid, 2,025 two-venue opportunities: **0 rejections**, 64%
saturated at exactly +1, p1 = +0.334 (the `G/2` floor).
`corr(signal, detector edge) = +0.655` over the grid, **+0.999** below
saturation with balanced liquidity.

**Production** (real platform, 5,000 ticks of the seeded market):

```
distinct opportunities         82
FIRST evaluations (entry)      82   negative: 0
RE-evaluations (monitoring)  1166   negative: 48   (4.1%)
```

**Every entry decision NORO has ever made on this market was positive.** Its
rejections come exclusively from continuous re-evaluation after entry, where
the market has moved away from the opportunity's original legs.

Rejection *is* reachable at three or four venues, but only in a narrow corner:
the extra venue must pull fair value outside the `[buy, sell]` interval
**without winning either extreme**, which requires a wide spread straddling
both. A tight venue priced outside simply becomes the new extreme and the
detector re-targets onto it. One such case is pinned exactly in
`test_noro_information_value.py::TestRejectionIsReachableButVanishinglyRare`.

*Consequence:* NORO's 1.5 consensus weight, on entry, is a near-deterministic
"yes". *Mitigations:* it can still veto on exit; a missing NORO still
suspends the strategy; ZEPHR and RUNE remain independent gates.

### P3-5 — Self-inclusion dilutes outlier detection · **MEDIUM**

Every venue is judged against a benchmark it is part of, weighted by its own
liquidity. Deep venue at 100.0 (1,000,000) vs thin at 101.0 (10,000):
deviation A = 0.99 bps, deviation B = 99.0 bps. Reverse the liquidity and the
verdict about which venue is mispriced reverses — with identical prices.

At 90% of the liquidity, an outlier's measured deviation is ~⅕ of what a
leave-one-out benchmark reports. At extreme dominance the outlier *becomes*
fair value and the two agreeing venues are reported as the outliers.

Audit-only comparators (`tests/audit/helpers.py`) quantify it: over a 3-venue
grid, leave-one-out **never changed the sign** (0/120) but was **more than 2×
the magnitude in 13.3%** of cases. With a dominant outlier the benchmarks
disagree by >45 price units (weighted mean 143 vs median 100 vs weighted
median 150 vs trimmed mean 100).

### P3-6 — Health does not express cross-venue readiness · **MEDIUM**

`_heartbeat` compares priced symbols to expected symbols. "Priced" means *at
least one usable venue*. With one venue per symbol across two symbols, NORO
reports **HEALTHY** — while being unable to evaluate any cross-venue
opportunity, because a one-venue fair value places that venue at exactly zero
deviation and the other leg is absent from the benchmark entirely.

The health record carries no venue count, so a one-venue and a two-venue
symbol are indistinguishable to an operator (both HEALTHY, both `detail=""`).

**This is an observability defect, not a trading-safety one:** `evaluate`
returns `None`, consensus marks NORO missing, `complete=False`, and
`entry_allowed` is False. Both halves are proved.

### P3-7 — Source timestamp launders contributor age · **MEDIUM**

`Noro.evaluate` stamps `source_data_timestamp = self.market.source_data_timestamp`
— the newest observation **anywhere in the market**.

Measured: BTC legs at T−100 and T−1,000 with an unrelated ETH venue at T
produce a NORO opinion claiming freshness T — **1,000 ms fresher than its own
oldest input**. Even without an unrelated symbol, the newer leg masks the
older one.

Phase 1 established exactly this principle (TIDAL-H4) and added
`MarketState.source_data_timestamp_for(legs)`, which returns the **oldest**
contributing leg. The orchestrator's three `TradeIntent` sites were moved onto
it and guarded by a regression test whose own docstring says *"AgentOpinion
elsewhere is out of this batch's scope"*. NORO is that scope note.

*Mitigation:* nothing currently gates on a NORO opinion's `data_age_ms`; RUNE
gates on the TradeIntent's, which already uses the correct helper.

### P3-8 — Confidence is poorly calibrated · **MEDIUM**

```
confidence = 0.35 + 0.45·min(1, liquidity/250_000) + 0.20·min(1, venues/2)
```

| observation | value |
|---|---|
| floor, 1 venue, ~0 liquidity | 0.45 |
| floor, 2 venues, ~0 liquidity | **0.55** |
| $1 of depth across 2 venues | 0.55 (measured through the agent) |
| saturation | $250,000, absolute, shared by every symbol |
| breadth saturation | **2 venues** — a 3rd, 4th or 10th adds exactly 0 |
| reachable two-venue range | [0.55, 1.00] |
| configurable | **no constant is** |

Confidence is a function of `(total_liquidity, venue_count)` alone —
source-inspected to confirm it reads no deviation, spread, quality or age.
Two venues agreeing within 1 bps and two disagreeing by 100 bps produce
**identical** confidence; two venues quoting a 5× price difference report
confidence 1.000.

The same `usable_liquidity` number both weights the benchmark and rates it, so
P3-1's window discontinuity moves confidence by 0.356 with no market change.

**In production this field is inert:** across 1,248 observed opinions the
minimum confidence was exactly 1.0000 — simulated liquidity is always above
the hard-coded threshold.

### P3-9 — Liquidity weighting measures executability, not price discovery · **MEDIUM**

`min(bid_depth, ask_depth)` is two-sided *usable capacity*. A venue quoting
$1,000,000 of bid and $5,000 of ask is weighted identically to one quoting
$5,000 on both sides, and a venue with an empty side is excluded from price
discovery entirely however well observed its other side is.

The choice of statistic spans two orders of magnitude on such a book
(min 5,000 · harmonic 9,950 · geometric 70,711 · total 1,005,000).

**This is ZEPHR's question.** ZEPHR explicitly owns depth, impact, slippage,
fees, latency and economic size. NORO weighting a fair-value *benchmark* by
executable capacity is an architectural overlap: the two agents answer
partially the same question and are then combined as if independent.

### P3-10 — The microprice can dominate on a wide book · **LOW**

At the default `microprice_weight = 0.5`, the microprice controls up to half
the spread of the venue price. On a 60 bps spread that is **30 bps** — twice
the entire saturation band. Measured: touch imbalance alone, with identical
mids, liquidity and spreads, flips the vote from +0.63 to −0.30.

On a tight (1 bps) book its authority is 0.5 bps and negligible. So NORO's
sensitivity to a single book feature is entirely spread-dependent.

### P3-11 — Semantically valid but meaningless configuration · **LOW**

- `saturation_bps = inf` passes `gt=0`: every signal becomes 0.0, so NORO
  votes neutral on everything while reporting healthy and confident.
- `saturation_bps = 1e-12`: every non-zero dislocation saturates at ±1.
- `liquidity_window_bps = 9.999`: accepted, can never hit a bucket (P3-1).
- `ttl_ms = 10¹²`: an opinion that never expires is permanently FRESH.

NaN and −inf are correctly rejected by pydantic's bound.

---

## 7. Quality, freshness and lifecycle

`DataQuality.is_usable` is **FRESH-only**, so DEGRADED contributes nothing at
all — unlike consensus, which down-weights it. All 4 levels and 6 mixed
combinations verified.

`on_market_state` rebuilds `self.fair_values = {}` from scratch every
snapshot, so **no stale valuation can survive**: after a snapshot in which
both venues go STALE, `fair_value()` returns `None` and evaluating a
previously-valid opportunity returns `None` rather than a fabricated neutral.
Verified.

Symbol isolation verified: BTC and ETH fair values are independent, unrelated
symbol updates leave the other untouched, and a symbol with no venues simply
has no fair value.

---

## 8. Opportunity leg coverage (Section 20)

| case | behaviour |
|---|---|
| both legs present | opinion produced |
| one leg missing | `None` — fails closed |
| both legs missing | `None` |
| duplicate venue legs | evaluated twice; edge = −\|dev\|, signal < 0 |
| single-leg opportunity | accepted; `min + mean` of one element = 2× it |
| three-leg opportunity | accepted |
| wrong symbol | `None` |
| same venue both sides | unreachable — the detector refuses it |
| empty leg list | `None` — the `if not confirmations` guard works |

---

## 9. Consensus influence (Sections 23 / 24)

Weights: TIDAL 1.4, **NORO 1.5**, ZEPHR 1.5, LUMEN 0.5 (total 4.9, NORO 30.6%).
Entry threshold 0.60, exit 0.45, degraded factor 0.35.

With TIDAL at 0.20 and ZEPHR at 0.80 held fixed, **NORO alone decides**: the
entry threshold is crossed at a NORO signal of ≈ **0.76**. At full confidence
NORO commands >0.55 of the score range; even at its realistic floor (0.55) it
commands >0.4. A NORO signal of −1.0 blocks an otherwise unanimous 0.9/0.9
consensus.

**Missing ≠ neutral**, proved both ways: a missing NORO gives
`complete=False` and blocks regardless of a 0.90 agreement; a neutral NORO
gives `complete=True` and 0.5715. With TIDAL and ZEPHR at 1.0/0.9 the missing
case has the *higher* raw agreement and is still blocked while the neutral one
is allowed. STALE is reported missing and blocks; DEGRADED counts at 0.35×.

---

## 10. Agent overlap (Sections 40–42)

| feature | TIDAL | detector | NORO | ZEPHR |
|---|---|---|---|---|
| mid | produces | — | uses | uses |
| microprice | produces | — | uses | — |
| best bid/ask | produces | **uses** | — | uses |
| depth buckets | produces | — | **uses** | uses |
| full-book depth | produces | — | uses (fallback) | uses |
| consolidated reference | produces | uses | — | — |
| fees / latency / impact | — | — | — | **owns** |

NORO consumes TIDAL features and recombines them; the detector consumes a
*different* TIDAL feature (the touch). NORO's two-venue signal correlates
**+0.999** with the detector's gap below saturation. NORO's liquidity
weighting overlaps ZEPHR's executability mandate (P3-9).

So consensus combines: the detector's gap (TIDAL touch), a monotone function
of the same gap (NORO), and a cost model applied to the same gap (ZEPHR) —
weighting one underlying observation three times.

---

## 11. Causal alignment (Sections 45 / 46) — **no defect found**

`MARKET_STATE` has exactly one producer (`Tidal.publish_state`), called from
exactly one place (`Orchestrator._observe`), which publishes and **drains**
before `_tick_body` runs. Detection publishes and drains inside that same tick
body. So NORO's `fair_values` always belong to the snapshot that produced the
opportunity. Verified by source inspection and through NORO's own handler.

**But the alignment is a property of the orchestrator, not an invariant NORO
enforces.** Forcing a newer snapshot between publication and evaluation makes
the *same* opportunity flip from +0.57 to −1.0. The `Opportunity` model
carries no reference to its snapshot, and `evaluate` never reads
`opportunity.created_at`, so NORO cannot detect a misalignment if one were
ever introduced.

The one place production *does* re-evaluate an older opportunity —
`_monitor` — is deliberate: it re-publishes with `ts_ms=self.tick_time`, NORO
stamps the current time, and NORO reads only `leg.venue`/`leg.side`, never the
recorded leg prices (source-verified). Re-evaluation is coherently "is this
venue cheap *now*".

Correlation integrity verified: opinions carry the opportunity id, published
events carry it, concurrent BTC/ETH opportunities do not contaminate each
other.

---

## 12. Performance and state

`compute_fair_value` — **linear**:

| venues | calls | total ms | µs/call | µs/venue | relative |
|---|---|---|---|---|---|
| 2 | 10,000 | 58.40 | 5.84 | 2.920 | 1.00× |
| 5 | 4,000 | 46.27 | 11.57 | 2.314 | 0.79× |
| 10 | 2,000 | 40.33 | 20.17 | 2.017 | 0.69× |
| 25 | 800 | 36.60 | 45.76 | 1.830 | 0.63× |
| 50 | 400 | 36.71 | 91.77 | 1.835 | 0.63× |
| 100 | 200 | 37.84 | 189.18 | 1.892 | 0.65× |

Per-venue cost is flat or slightly improving — no quadratic behaviour.

`Noro.on_market_state` — 17.85 µs (2 symbols) → 77.36 (10) → 1,212 (100);
roughly linear with per-symbol overhead.

**State growth:** `fair_values` is bounded by `settings.symbols` (200
snapshots × 5 symbols → 5 entries); an unconfigured symbol in the snapshot is
ignored, not accumulated; 500 evaluations add nothing but an integer counter;
the only growable attribute on the agent is `fair_values`.

---

## 13. Replay

NORO is unchanged, so Phase 2's guarantees are untouched — and re-verified:
**130 replay tests pass**, and the 15–95 tick sweep plus the 800-tick session
remain equivalent.

---

## 14. Hypothesis verdicts

| # | Hypothesis | Verdict |
|---|---|---|
| H1 | arbitrary window falls back to full-book depth | **CONFIRMED** (201× at 0.001 bps) |
| H2 | "weakest leg governs" is false under `min + mean` | **CONFIRMED** (veto only past −⅓) |
| H3 | `saturation_bps` docs ≠ behaviour | **CONFIRMED** (saturates at half) |
| H4 | two-venue NORO structurally confirms the detector | **CONFIRMED** — proved, and 0/25,027 empirically, 0/82 in production |
| H5 | self-inclusion lets a deep venue dominate its own benchmark | **CONFIRMED** (magnitude; sign never flipped in 120 comparisons) |
| H6 | source timestamp is not the oldest contributor | **CONFIRMED** (1,000 ms laundering measured) |
| H7 | healthy with one venue per symbol | **CONFIRMED** (observability only; trading fails closed) |
| H8 | confidence high at negligible liquidity, breadth saturates at 2 | **CONFIRMED** (0.55 floor; 3rd venue adds 0) |
| H9 | NORO double-counts TIDAL/ZEPHR features | **PARTIALLY CONFIRMED** (r = +0.999 vs detector; ZEPHR overlap architectural) |
| H10 | an opportunity could be judged against a newer FairValue | **REFUTED in production**, but undefended by NORO itself |

---

## 15. Findings table

| ID | Severity | Finding | Confirmed | Economic consequence | Current mitigation |
|---|---|---|---|---|---|
| P3-1 | HIGH | liquidity-window bucket fallback discontinuity | yes | which venue anchors fair value inverts on a 0.001 bps config change | default is on a bucket |
| P3-2 | HIGH | "weakest leg governs" is false | yes | positive vote on a trade one leg contradicts | ZEPHR/RUNE independent |
| P3-4 | HIGH | two-venue fair value cannot contradict the detector | yes | NORO's 1.5 entry weight is a near-deterministic yes | can veto on exit; missing still suspends |
| P3-3 | MED | `saturation_bps` semantics mismatch | yes | signal saturates at half the configured deviation | monotone; direction correct |
| P3-5 | MED | self-inclusion dilutes outlier detection | yes | a deep outlier becomes its own benchmark | sign preserved in tests |
| P3-6 | MED | health does not express cross-venue readiness | yes | operator misled; no unsafe trade | `evaluate` → None → consensus incomplete |
| P3-7 | MED | source timestamp launders contributor age | yes | opinion claims to be fresher than its inputs | nothing gates on it today |
| P3-8 | MED | confidence poorly calibrated | yes | scales NORO's weight on a near-constant | saturated at 1.0 in practice |
| P3-9 | MED | liquidity weight measures executability | yes | duplicates ZEPHR; penalises informative one-sided books | — |
| P3-10 | LOW | microprice can dominate on a wide book | yes | touch imbalance alone flips the vote | tight books unaffected |
| P3-11 | LOW | semantically meaningless config accepted | yes | NORO silently neutral or always saturated | non-default only |

**No CRITICAL findings.** Nothing produces an unsafe trade on its own: every
NORO failure path either fails closed (missing opinion → strategy suspended)
or is bounded by ZEPHR's cost model and RUNE's hard gates.

---

## 16. Top five risks

1. **P3-4** — NORO cannot reject a two-venue entry. It is a required, joint-heaviest agent whose entry vote was positive in 82/82 production decisions and 25,027/25,027 random ones. Fix first: everything else is calibration of a signal that currently cannot say no.
2. **P3-2** — "weakest leg governs" is false, and the reason codes say so out loud (`CONFIRMS` and `LEG_AGAINST` together). Common whenever a third venue moves fair value past a leg.
3. **P3-1** — a 0.001 bps configuration digit inverts which venue anchors fair value. Rare (needs an off-bucket window) but silent and total.
4. **P3-8** — confidence multiplies NORO's weight and is pinned at 1.0 in practice; a 3rd venue, the one thing that makes NORO able to disagree, raises it by zero.
5. **P3-7** — a required agent's opinion claims to be fresher than its inputs, re-introducing the exact defect Phase 1 closed for TradeIntents.

---

## 17. Proposed remediation batches (not implemented)

**Batch 3.1 — valuation-data semantics.** P3-1, P3-7, P3-11.
Files: `agents/noro/fair_value.py`, `agents/noro/agent.py`, `core/config/settings.py`.
Success: no silent fallback; window validated against published buckets; opinion age = oldest contributing leg; config rejects meaningless values.

**Batch 3.2 — signal semantics.** P3-2, P3-3, and the reason-code contradiction.
Files: `agents/noro/agent.py`, `core/config/settings.py`.
Success: the documented rule and the implemented rule agree; `saturation_bps` parameterises a named, documented quantity; no output asserts confirmation and leg-contradiction at once.

**Batch 3.3 — independence.** P3-4, P3-5, P3-9.
Files: `agents/noro/fair_value.py`, possibly `strategies/consensus`.
Success: NORO can reject a two-venue opportunity on evidence (leave-one-out or a robust benchmark), or its role/weight is explicitly re-scoped and its required status revisited. Evidence already gathered in `tests/audit/helpers.py`.

**Batch 3.4 — confidence and consensus integration.** P3-6, P3-8.
Files: `agents/noro/agent.py`, `core/config/settings.py`.
Success: confidence reflects valuation uncertainty, not data volume; constants configurable; health expresses cross-venue readiness.

**Batch 3.5 — validation.** Re-run every audit suite as regression, the full
replay sweep, and an original-vs-replay economic equivalence check.

---

## 18. Known limitations of this audit

1. The seeded simulated market is **two-venue only**, so production evidence for 3+ venue behaviour comes from constructed and random books, not from a live multi-venue feed.
2. Live public-feed data remains externally blocked (unchanged since Phase 1), so all measurements are synthetic.
3. Section 25's full end-to-end economic demonstration (MarketState → PaperOrder driven purely by a NORO change) was **not** built: NORO's entry vote could not be made negative on the two-venue simulated market without changing production code, which this phase forbids. The consensus threshold map (§9) establishes the same influence quantitatively.
4. Rejection rates are measured against *this* detector. A different opportunity source would change P3-4's numbers.
