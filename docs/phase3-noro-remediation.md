# Phase 3 remediation — NORO valuation engine

Phase 3 audited NORO and proved it was not doing its job. This build makes its
vote truthful:

> NORO judges a proposed direction against a benchmark built from venues that
> are **not** participating in the opportunity, lets the weakest leg govern
> outright, and — when no such independent venue exists — says so, instead of
> confirming the detector with evidence the detector supplied.

- **Base SHA:** `648604886182a780e89ac239780118f0b9f85d3f`
- **Branch:** `phase3-noro-remediation`
- **Scope:** `agents/noro/`, `NoroConfig`, and the depth-bucket contract moved
  into `core/models/market.py`. Paper trading only; no live execution anywhere.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. What NORO used to do

```python
FV        = Σ(price_v · usable_liquidity_v) / Σ(usable_liquidity_v)
price_v   = mid·(1−w) + microprice·w
liquidity = min(bid_depth[window], ask_depth[window])   # or the WHOLE BOOK
edge_bps  = min(confirmations) + mean(confirmations)
signal    = clamp(edge_bps / saturation_bps)
confidence = 0.35 + 0.45·min(1, Σliquidity/250k) + 0.2·min(1, n_venues/2)
```

Every line of that has a defect the audit named.

## 2. The Phase 3 findings

| # | Finding | Severity |
| --- | --- | --- |
| P3-1 | Liquidity-window bucket fallback discontinuity | HIGH |
| P3-2 | "Weakest leg governs" is false under the signal formula | HIGH |
| P3-4 | Two-venue NORO cannot contradict the detector | HIGH |
| P3-3 | `saturation_bps` semantics mismatch | MEDIUM |
| P3-5 | Self-inclusion dilutes outlier detection | MEDIUM |
| P3-6 | Health does not express cross-venue valuation readiness | MEDIUM |
| P3-7 | `source_data_timestamp` launders contributor age | MEDIUM |
| P3-8 | Confidence poorly calibrated; breadth saturates at two | MEDIUM |
| P3-9 | Liquidity weighting overlaps ZEPHR executability | MEDIUM |
| P3-10 | Microprice may dominate valuation on wide-spread books | LOW |
| P3-11 | Semantically meaningless config values are accepted | LOW |

All eleven are addressed here.

## 3. Ownership boundary, after Phase 4

Phase 4 settled who owns what, and this build holds the line:

| | NORO | ZEPHR |
| --- | --- | --- |
| Question | is this direction actually mispriced? | can it be executed profitably at size? |
| Inputs | venue prices, near-touch depth as *reliability*, breadth, agreement, freshness | book levels, fees, slippage, impact, latency, feasible size |
| Never touches | book walking, slippage, fees, impact, executable size | valuation benchmarks |

NORO imports nothing from `agents.zephr` and reads no ZEPHR output. It is
computable from `MarketState` alone.

The word doing the work in P3-9 is *reliability*. Near-touch depth still enters
the estimator, but bounded: `min(1, notional / reliability_saturation_notional)`
caps every contributor's weight at 1.0. Past the saturation point, extra depth
buys no extra influence — so the weight expresses *how much price discovery
stands behind this quote*, not *how much could be traded here*.

## 4. Independent valuation

The audit's central production finding: **82 of 82 entry votes were positive,
none of them informative.** The reason is structural. The detector picks the
cheapest ask and the richest bid. A fair value formed from those same two
prices necessarily lands between them, so the buy venue is necessarily below it
and the sell venue necessarily above it. NORO was grading its own homework.

The fix is to build the benchmark from venues **not participating in the
opportunity**:

```python
participating = {leg.venue for leg in opportunity.legs}
independent   = [c for c in contributors if c.venue not in participating]
benchmark     = weighted_median(independent)      # None when empty
```

Both legs are then compared against that same benchmark. This is strictly
stronger than leave-one-out: leave-one-out with two venues judges A against B
and B against A, which is the same pair of prices the detector used and
confirms it just as reliably. Excluding *both* opportunity venues means no
venue helped build the benchmark that judges it, and no benchmark is built from
the pair whose disagreement is the opportunity.

## 5. Two-venue behaviour

With exactly two usable venues, both are in the opportunity, so `independent`
is empty. There is no independent evidence, and NORO says exactly that:

```
signal      = 0.0
confidence  = noro.insufficient_breadth_confidence   (0.1)
reason      = INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH
```

Three deliberate choices:

- **Not `None`.** NORO is a required agent, and a missing opinion suspends the
  whole strategy. Two-venue markets are the common case, not an outage.
  *Missing* and *neutral* are different claims and must stay different.
- **Not a positive vote.** That was the defect.
- **Low confidence, not zero and not high.** `signal = 0, confidence = 1`
  asserts strong belief in neutrality — the opposite of what is being said.
  Non-zero keeps the opinion visible in the weighted consensus rather than
  silently vanishing as though NORO had never been asked.

TIDAL and ZEPHR then carry the decision on their own evidence, which is the
honest outcome.

## 6. Three-plus-venue behaviour

With at least one venue outside the opportunity, NORO becomes informative and
can genuinely contradict the detector: the benchmark is real evidence the
detector never saw.

```
for leg:
    deviation_bps  = bps(leg_venue_price − benchmark)
    confirmation   = −deviation  if BUY else  +deviation
confirmed_edge_bps = min(confirmations)
```

A BUY leg confirms only when its venue is below the independent benchmark; a
SELL leg only when above.

## 7. The benchmark formula

**Reliability-weighted median** of independent contributor prices:

```
price_v   = mid_v · (1 + clamp(bps(microprice_v − mid_v) · w, ±cap) / 10_000)
r_v       = min(1, near_touch_notional_v / reliability_saturation_notional)
benchmark = weighted_median({(price_v, r_v)})
```

`weighted_median` orders contributors by `(price, venue)` — the venue name
breaks price ties, so the result never depends on upstream dict ordering — and
returns the first price at which accumulated weight passes half the total. When
the accumulated weight lands *exactly* on half, the two straddling prices are
averaged (the standard lower/upper convention, which keeps two equally reliable
venues symmetric).

Why a median rather than the previous liquidity-weighted mean:

- It **lies within contributor prices** — a price some venue is actually
  quoting, not an average of two that nobody is.
- One extreme venue **cannot drag it**. A weighted mean moves with every
  outlier in proportion to its size, which is exactly how one enormous venue
  came to define fair value and dilute the outlier detection it existed to
  perform (P3-5).
- It behaves sensibly at every size: 1 contributor → that price; 2 equally
  reliable → their midpoint; 3+ → the middle of the distribution.

Contributor weights are bounded at 1.0, so the estimator cannot be captured by
depth even before the median's own robustness applies.

## 8. Depth buckets: exact, or excluded

`DEPTH_BUCKETS_BPS = (1.0, 5.0, 10.0, 25.0)` now lives in
`core/models/market.py`, beside the `BookMetrics` that carries the buckets,
with a shared `depth_bucket_key()` so the producer and every consumer cannot
drift into writing `"10"` and reading `"10.0"`. TIDAL re-exports it and is
otherwise unchanged.

Two consequences:

1. `NoroConfig.liquidity_window_bps` is **validated against that tuple**. A
   value of `10.001` is now a startup error naming the supported set, not a
   silent runtime surprise.
2. `BookMetrics.depth_within()` returns `None` when a bucket was not measured,
   and NORO **excludes that contributor**. The old fallback to
   `bid_depth_notional`/`ask_depth_notional` is gone: whole-book depth answers
   a different question and is one to two orders of magnitude larger, so the
   venue that measured *nothing* near the touch came back looking like the
   deepest contributor in the market.

## 9. Microprice, bounded

The microprice leads the mid and is genuine information, so it is kept. But it
lives strictly between bid and ask, so its distance from mid scales with the
spread: a 40 bps book can hand the valuation a 10 bps displacement out of
nothing but touch imbalance, which then outweighs every other venue.

```
displacement_bps = bps(microprice − mid) · microprice_weight
price            = mid · (1 + clamp(displacement_bps, ±microprice_max_displacement_bps) / 10_000)
```

On a tight book the cap never binds and this is the old blend exactly — with
the simulated market's ~3 bps spreads the displacement is ≤ 0.75 bps against a
2.0 bps cap, so simulated behaviour is unchanged. On a wide book, touch
imbalance refines the price by at most the cap instead of dominating it.

## 10. Weakest leg governs

```
confirmed_edge_bps = min(confirmations)          # was min(...) + mean(...)
```

The old formula let a strongly confirming leg buy off a contradicting one:
`[-1, +10]` came out at `+3.5`, positive, so "both legs must agree" was not
what the code did. Under `min`, one negative leg makes the aggregate negative
and one neutral leg caps it at neutral. There is no compensation available.

## 11. Signal and saturation

```
signal = clamp(confirmed_edge_bps / saturation_bps, −1, +1)
```

Because the aggregate is now a *single leg's* confirmation rather than a
weakest-plus-mean sum, `saturation_bps` finally means what its name says: 15
bps of weakest-leg confirmation is `+1`, and −15 bps is `−1`. Under the old
formula the same 15 bps could produce anything from `+1` to well past
saturation depending on the other leg.

## 12. Confidence

Three components, at configured weights validated to sum to exactly 1:

```
breadth   = 1 − 1/(1 + n_independent)     # 0, 0.5, 0.667, 0.75, 0.8 …
agreement = clamp(1 − dispersion_bps / dispersion_tolerance_bps, 0, 1)
quality   = mean(reliability of independent contributors)

confidence = 0.4·breadth + 0.3·agreement + 0.3·quality
```

Against the audit's complaints:

- **Breadth no longer saturates at two.** It has diminishing returns but never
  stops rising, so a third and fourth independent opinion are worth something.
- **Disagreement now matters.** `dispersion_bps` is the widest contributor
  deviation from the benchmark; venues that disagree by more than
  `dispersion_tolerance_bps` are not describing one price.
- **Nothing measures tradeable size.** The fixed $250k liquidity scale that
  every venue cleared by a wide margin — which is why production confidence was
  pinned near 1.0 — is gone. That question is ZEPHR's.

The neutral two-venue path uses `insufficient_breadth_confidence` directly, and
does not compute the three components at all: there is nothing to compute them
over.

## 13. Reason codes

| Code | When |
| --- | --- |
| `FAIR_VALUE_CONFIRMS_DISLOCATION` | weakest confirmation > 0 |
| `FAIR_VALUE_CONTRADICTS_DISLOCATION` + `LEG_AGAINST_FAIR_VALUE` | weakest < 0 |
| `FAIR_VALUE_NEUTRAL_ON_DISLOCATION` | weakest exactly 0 |
| `INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH` | no independent contributor |

`LEG_AGAINST_FAIR_VALUE` fires exactly when the weakest confirmation is
negative, which is exactly when `CONTRADICTS` fires — so the old incoherent
`CONFIRMS + LEG_AGAINST_FAIR_VALUE` pairing is now unreachable.

## 14. Timestamps

`market.source_data_timestamp` (the market-wide newest observation) is gone.
The opinion now carries:

```python
market.source_data_timestamp_for((c.venue, c.symbol) for c in contributors)
```

— the oldest exchange observation among the contributors that actually
supported the conclusion, which is the union of the opportunity's own legs and
the independent venues judging them (the legs are always contributors, so the
contributor set *is* that union).

`None` means unknown, and unknown is fail-closed: an opinion whose age cannot
be checked is not published, matching the choice Phase 4 made for ZEPHR.

## 15. Health

| Status | Condition |
| --- | --- |
| `HEALTHY` | at least one configured symbol has ≥ `MIN_VALUATION_CONTRIBUTORS` (2) usable cross-venue contributors |
| `DEGRADED` | market data exists, but no symbol reaches 2 |
| `OFFLINE` | no usable market data at all |

Previously one contributor per symbol counted as "priced", so NORO reported
healthy on a market where no cross-venue valuation was possible at all (P3-20's
one-venue case).

The detail string also names how many symbols reach
`MIN_INFORMATIVE_CONTRIBUTORS` (3) — the count needed to confirm or contradict
rather than merely return the neutral verdict. A symbol sitting at exactly two
is healthy and answering honestly, but will never carry information, and an
operator should see that without reading opinions one at a time.

**The readiness bar is 2, not 3, and that is a deliberate choice.** Setting it
at 3 would make the default two-venue configuration permanently `DEGRADED`;
NORO is a required component, so the strategy would be suspended and the
platform would never trade. Two contributors *is* a cross-venue valuation — the
honest neutral one — so it is healthy. Breadth is exposed in the detail rather
than smuggled into the status.

## 16. Configuration

| Field | Default | Change |
| --- | --- | --- |
| `liquidity_window_bps` | `10.0` | now validated against `DEPTH_BUCKETS_BPS` |
| `microprice_weight` | `0.5` | unchanged, now `allow_inf_nan=False` |
| `microprice_max_displacement_bps` | `2.0` | **new** — P3-10 cap |
| `ttl_ms` | `2_000` | unchanged |
| `saturation_bps` | `15.0` | unchanged value, corrected meaning |
| `reliability_saturation_notional` | `100_000.0` | **new** — replaces the inline `250_000` |
| `dispersion_tolerance_bps` | `20.0` | **new** |
| `breadth_confidence_weight` | `0.4` | **new** — replaces inline `0.2` breadth term |
| `agreement_confidence_weight` | `0.3` | **new** |
| `quality_confidence_weight` | `0.3` | **new** — replaces inline `0.45` liquidity term |
| `insufficient_breadth_confidence` | `0.1` | **new** |

Validation added (P3-11): the window must name a measured bucket; the three
confidence weights must sum to exactly 1; every float carries
`allow_inf_nan=False`, because `Field(gt=0)` rejects NaN and −inf but **accepts
+inf** (the same trap `PriceLevel` documents as TIDAL-M3).

No environment variables were added — `NoroConfig` has never been
env-overridable.

## 17. Model version

`VERSION = "noro-0.2"`. The signal's *meaning* changed: measured against a
benchmark that excludes the venues under judgement, governed outright by the
weakest leg, and neutral rather than positive on a two-venue market. An 0.1
opinion and an 0.2 opinion carrying the same number do not say the same thing,
and attribution must be able to tell them apart.

## 18. Data model

`VenueValuation` gains `symbol`, `near_touch_notional`, `reliability` and
`exchange_ts`; its old `liquidity`/`weight`/`deviation_bps` fields are gone.
`FairValue` gains `dispersion_bps` and drops `total_liquidity` — the rename to
`total_near_touch_notional` is the point, because the old name asserted
executable capacity that NORO never measured.

`compute_fair_value` still exists and still returns a symbol-wide valuation. It
is now explicitly the **diagnostic** view — health, the dashboard, "what does
the market think this is worth" — and is never what an opportunity is judged
against. `Noro.independent_valuation()` is that. The two are kept separate on
purpose: one cached self-inclusive fair value cannot serve both roles without
reintroducing exactly the defect P3-4 names.

`apps/api/app.py` reads `noro.fair_value(symbol).fair_value`; both survive
unchanged.

## 19. Expected behavioural change

1. **On a two-venue market — the default configuration — NORO stops voting
   positive.** It contributes `signal 0.0` at `confidence 0.1` instead of a
   near-saturated `+1` at `confidence ~1.0`. NORO carries consensus weight 1.5
   out of 4.9. Entry agreement will fall materially and **the platform will
   enter far fewer trades, possibly none, on a two-venue market.**
   This is the intended correction: those votes were never evidence. Consensus
   weights and the entry threshold were **not** changed to compensate, as
   instructed. **This is the first thing external validation should measure.**
2. **With three or more venues, NORO becomes able to contradict the detector.**
   It could not before.
3. **Confidence stops being pinned near 1.0** and starts responding to breadth
   and disagreement.
4. **Venues with no measured near-touch depth are excluded**, where they were
   previously admitted at whole-book depth — which is also the largest possible
   weight. Any construction that relied on the fallback now produces fewer
   contributors, or none.
5. **NORO publishes no opinion when a contributor has no exchange timestamp**,
   where it previously published one carrying a market-wide timestamp.
6. `NoroConfig` gained fields, so the **material configuration digest changes**;
   recordings made before this commit will be refused against a current digest.
   Correct — the valuation model genuinely changed. The digest mechanism itself
   is untouched.

## 20. Deferred, and why

- **Consensus weights and thresholds are untouched**, by instruction. NORO's
  weight of 1.5 now buys a mostly-neutral vote on two-venue markets, which is a
  real question for whoever calibrates consensus — but calibrating around a
  vote that has only just become truthful is the wrong order.
- **Health readiness at 2 rather than 3**, argued in §15.
- **A third venue is not synthesised.** Adding a reference feed to manufacture
  independent evidence is a data-sourcing decision, not a NORO one.
- **`dispersion_bps` is a max, not a spread statistic.** Simple, bounded and
  interpretable with as few as one contributor; a proper weighted MAD would be
  better with many, and there are rarely many.
- **The Phase 3 audit artifacts were not touched.** `tests/audit/**` and
  `scripts/audit_noro_*.py` encode the *old* semantics by design — they are the
  reproduction of the findings. Several will now fail, and several should: the
  external validator decides which are regressions and which are findings that
  no longer reproduce. Nothing in `tests/` was modified or added.

## 21. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed: no `pytest`, no `ruff`, no `mypy`, no import
check, no script, no container, no replay, no application start. Every
statement here is a claim about code that an independent validator must
confirm.

---

## 22. Validation-suite migration

A follow-up commit on this branch migrated the test surface. No production
behaviour was changed by it, and no test was executed by its author.

**Why the tests were out of date by design.** The Phase 3 audit suite was
written *before* remediation, and much of it existed to prove that defects
were real: that `10.001` bps silently fell back to whole-book depth, that
`[-1, +10]` still produced a positive aggregate, that a two-venue detector
opportunity was confirmed by construction, that confidence sat at 1.0
regardless of disagreement. Those assertions were correct records of the old
behaviour. NORO v0.2 changed that behaviour deliberately, so the tests had to
change with it.

**What they became.** Evidence, not deletions. Each defect reproduction was
turned into a regression assertion that the defect stays closed, keeping the
original finding's numbers in the docstring as the thing that must not come
back, and naming the finding ID in the class or test name (`TestP3_2_...`,
`test_p3_1_arbitrary_window_cannot_trigger_full_book_fallback`). Nothing was
solved with a module-level `skip` or `xfail`.

Two collection blockers reported by the external validator are addressed:
`tests/unit/test_pricing.py` and `tests/audit/test_noro_liquidity_window.py`
both imported the removed `usable_liquidity`, which stopped collection before
any semantic test ran. Both now use `near_touch_notional` and the
`NoroConfig`-based `venue_price`. A separate Ruff `RUF022` failure —
`core/models/__init__.py`'s `__all__` no longer sorted after the
`DEPTH_BUCKETS_BPS` and `depth_bucket_key` exports were added — is fixed by
reordering the list. That is the only non-test change in the migration commit,
and it is ordering only.

**One end-to-end consequence is deliberately left exposed.**
`tests/integration/test_pipeline.py::TestAttribution` asserts that the
platform closes trades on the simulated two-venue market. NORO now contributes
`signal 0.0` at confidence `0.1` there instead of a near-saturated `+1` at
confidence `1.0`, which removes roughly 1.5 of weighted mass from the
consensus numerator while leaving the entry threshold at 0.60. Whether the
remaining agents still clear that threshold is a question about the running
system, not about the code, and this pass could not answer it without
executing the suite. The assertion was therefore left untouched: if trades
stop, that is a real and important consequence of making NORO's vote truthful,
and it should surface as a failing end-to-end test rather than be smoothed
away by weakening the test or — expressly out of scope here — retuning
consensus.

**Testing status: TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.** Every
expectation in the migrated suite was derived by reading the implementation.
None of it is a measured result.
