# Phase 4 build — ZEPHR executability engine

Phase 4 makes one claim defensible:

> ZEPHR reports the size a cross-venue opportunity can actually be executed at,
> charging every cost exactly once against a *stated* reference frame, using a
> floor no looser than the one RUNE will apply to the resulting intent, and
> reporting confidence and health about the trade it was actually asked to
> price.

- **Base SHA:** `d2c4c1f8b9b3e51dabe860010931ed9b141061ee`
- **Branch:** `phase4-zephr-build`
- **Scope:** `agents/zephr/`, `execution/costs.py`, `ZephrConfig`, and the one
  orchestrator call site that consumed ZEPHR's curve. Paper trading only; no
  live execution anywhere in the codebase.
- **Testing status:** **NOT RUN — validation delegated externally.** No test,
  linter, type checker, script, container or application was executed in this
  pass. Every claim below is derived from static reading of the code.

---

## 1. What ZEPHR is for

NORO answers *is this mispriced?* ZEPHR answers *could we actually do it?*

It takes a detected cross-venue dislocation, prices both legs against the real
book at a ladder of sizes, subtracts every modelled cost, and reports:

- the size to trade (`SizingCurve.best`),
- the largest size that is executable at all (`max_economical_notional`),
- a signal in `[0, 1]` scaled by how far net edge clears the floor, or `-1`
  when nothing on the ladder is executable,
- a confidence driven by the thinnest leg's book,
- and a per-rung breakdown so a refusal says *why*.

It is a required consensus agent. When it cannot price an opportunity it
publishes nothing, and the orchestrator suspends the strategy rather than
trading on a hole.

## 2. The reference frame, and why it is the whole story

Every cost in this system is a *gap*: the distance between some reference
price and the price actually paid. That makes a cost meaningless without its
reference. Charging a component the reference already contains charges it
twice.

`strategies.cross_venue.detector.find_dislocation` measures gross edge between
the cheapest **ask** and the richest **bid**:

```python
buy_price  = min(usable, key=...best_ask).metrics.best_ask
sell_price = max(usable, key=...best_bid).metrics.best_bid
edge       = safe_bps(sell_price - buy_price, reference)
```

Those are the prices an aggressor actually gets. Both crossings are already
inside that number. `walk_book` then measures slippage **against the touch**
(`levels[0].price`), so slippage is purely what happens *beyond* the touch and
never overlaps it. The honest identity for a two-leg taker trade is therefore:

```
realised edge = gross_edge_bps
                - slippage(buy) - slippage(sell)
                - impact(buy)   - impact(sell)
                - fee(buy)      - fee(sell)
                - latency(buy)  - latency(sell)
                - residual hedge
```

There is no spread term. `EdgeFrame` (`execution/costs.py`) now makes that
choice explicit rather than assumed:

| Frame | Meaning | Spread charged to a taker leg? |
| --- | --- | --- |
| `TOUCH_TO_TOUCH` | edge measured ask-to-bid, as the detector produces | no — already paid for |
| `MID_TO_MID` | edge measured mid-to-mid | yes — the half spread crossed |

ZEPHR pins `GROSS_EDGE_FRAME = EdgeFrame.TOUCH_TO_TOUCH`, which is correct for
every opportunity this platform produces today.

## 3. Findings addressed

| # | Finding | Severity | Resolution |
| --- | --- | --- | --- |
| Z1 | Half the quoted spread was charged on every taker leg, on top of a gross edge already measured touch to touch — the spread was paid twice | **HIGH** | `EdgeFrame`; `spread_cost_bps` is 0 in the touch-to-touch frame |
| Z2 | ZEPHR's opinion TTL came from `settings.noro.ttl_ms` | **MEDIUM** | `ZephrConfig.ttl_ms` |
| Z3 | Opinion freshness used the market-wide newest `source_data_timestamp` | **MEDIUM** | per-leg oldest via `source_data_timestamp_for`, fail-closed |
| Z4 | Cost arithmetic existed twice: in `build_sizing_curve` and again in `Orchestrator._build_intent` | **MEDIUM** | `SizePoint.cost_breakdown()` is the only implementation |
| Z5 | Confidence read `best.legs[0].usable_liquidity` — one leg's book reported as the whole trade's | **MEDIUM** | bottleneck `min(...)` across all legs |
| Z6 | ZEPHR called a size economical at 1 bps that RUNE would reject at 2 bps | **MEDIUM** | `Settings.zephr_min_net_edge_bps` = `max(zephr, risk)` |
| Z7 | HEALTHY required only two usable venue/symbol pairs *anywhere*, so one venue on BTC-USD plus one on ETH-USD read as healthy while nothing was priceable | **MEDIUM** | per-symbol: HEALTHY needs ≥1 symbol with ≥2 usable venues |
| Z8 | `SIZE_CAPPED_BY_LIQUIDITY` was reported whenever the chosen size was not the largest rung, including when every rung was executable and the cap was ordinary edge decay | **LOW** | `SIZE_CAPPED_BY_DEPTH` / `_BY_EDGE_DECAY` / `_BY_RISK_LIMIT` |
| Z9 | `size_ladder` accepted empty, zero, negative, duplicated and unsorted ladders | **LOW** | validator: non-empty, finite, positive, strictly ascending |
| Z10 | Sizes below `risk.min_trade_notional` were reported as economical although RUNE could never authorise them | **LOW** | rungs under the floor are infeasible with a named reason |
| Z11 | Magic constants `0.4`, `0.6`, `200_000`, `3.0`, `0.9` were inline | **LOW** | five `ZephrConfig` fields |
| Z12 | `worst = min(points, key=total_cost_bps)` named the *cheapest* rung "worst" | **LOW** | renamed `cheapest`, and `cheapest_notional` added |
| Z13 | `hedge_cost_bps` was documented as "cost assumed for the hedge leg" while both legs were already priced | **LOW** | redocumented as a residual hedging allowance, charged once |
| Z14 | Slippage and modelled impact were summed into `CostBreakdown.slippage_bps` | **LOW** | impact carried in `other_bps` |

## 4. `LegQuote`: charged versus informational

The single field `spread_bps` conflated *what a leg crosses* with *what
crossing costs it*. That conflation is what let the spread be charged against
an edge that already contained it. It is now three fields with one meaning
each:

| Field | Kind | Value |
| --- | --- | --- |
| `quoted_half_spread_bps` | informational | half the venue's quoted spread, always |
| `crossed_spread_bps` | informational | the half spread this leg's style crosses — taker: quoted half; maker: 0 |
| `spread_cost_bps` | **charged** | 0 in `TOUCH_TO_TOUCH`; `crossed_spread_bps` in `MID_TO_MID` |

`total_cost_bps` sums exactly the charged components:
`slippage + impact + fee + latency + spread_cost`.

`LegQuote.spread_bps` survives as a **deprecated property** returning
`crossed_spread_bps`, so existing callers keep working. New code should say
which of the two it means.

## 5. Execution style

`LegQuote.liquidity` records the style (`Liquidity.MAKER` / `TAKER`) that
produced the fee tier, so a consumer can see which schedule was applied
instead of inferring it. `is_maker` remains the single knob on `quote_leg`,
because fee tier and crossing behaviour always move together and letting them
be set independently is how a leg ends up paying a maker fee for a taker's
crossing.

`build_sizing_curve` prices **every leg as a taker**, deliberately. A resting
order's economics are dominated by whether it fills at all, and this module
models neither queue position nor fill probability. Pricing a passive leg as
certain to fill would credit the strategy with a spread it has not earned. The
maker path remains for callers that model fill risk themselves.

## 6. Feasibility: three checks, in order

A rung is economical when it is physically available, large enough to be
permitted, and profitable enough to be worth doing — checked in that order, so
the reported reason is the *first* thing that rules the size out:

1. `NO_LEGS` — nothing to price.
2. `INSUFFICIENT_DEPTH` — the book could not supply the size on some leg.
3. `BELOW_MIN_TRADE_NOTIONAL` — under `risk.min_trade_notional`.
4. `NET_EDGE_BELOW_FLOOR` — net edge below the effective floor.

Infeasible rungs stay on the curve. The shape either side of the cutoff is the
diagnostic; dropping them leaves a refusal with nothing to explain itself
with.

The floor test is written `not (net >= floor)` rather than `net < floor` so a
non-comparable net edge is infeasible: every ordinary comparison against NaN
is false, and `<` would wave one through.

## 7. `best` versus `max_economical_notional`

These are deliberately different and must not be collapsed:

- **`best`** — the size to *trade*: the feasible rung with the greatest
  expected profit. Net edge decays with size while notional grows, so the most
  profitable rung is frequently not the largest.
- **`max_economical_notional`** — the *ceiling*: the largest feasible rung.
  RUNE gates on it (`LIQUIDITY_SUFFICIENT`) and caps its own sizing by it.

Replacing the ceiling with the chosen size makes the liquidity gate
tautological — it would compare the intent's notional against itself.
Replacing the chosen size with the ceiling trades the largest size that clears
the floor rather than the most profitable one. Both are preserved.

## 8. The minimum-edge disagreement

`zephr.min_net_edge_bps` defaulted to 1.0 bps; `risk.min_expected_edge_bps` to
2.0. Every size in that gap was advertised by ZEPHR as economical, sized into
an intent, and then rejected by RUNE's `MIN_EXPECTED_EDGE` gate.

`Settings.zephr_min_net_edge_bps` resolves it as `max(zephr, risk)` — the only
direction that cannot weaken RUNE. **No risk limit was changed.** ZEPHR's
floor rises to meet the limit; the limit never falls to meet ZEPHR. An
operator who sets ZEPHR's floor *above* RUNE's stays authoritative, because
being pickier than the hard limit is always allowed.

## 9. Size floor, and what was deliberately not done

Rungs below `risk.min_trade_notional` are now marked infeasible. No rung is
**added** at that boundary. Injecting a smaller tradeable size than the
operator configured would let ZEPHR find executable trades where the
configured ladder found none — a widening of what the platform trades, which
is not this pass's business. The boundary is enforced as a floor only.

## 10. One implementation of the cost arithmetic

`SizePoint.cost_breakdown()` is now the only place per-leg quotes are folded
into a `CostBreakdown`. `Orchestrator._build_intent` calls it instead of
re-deriving the same sum by hand.

The invariant this buys: `cost_breakdown().total_bps == total_cost_bps`
exactly, therefore
`cost_breakdown().net_from(gross) == SizePoint.net_edge_bps` exactly. The two
copies could previously drift, and RUNE's `MIN_EXPECTED_EDGE` gate reads the
orchestrator's copy while ZEPHR's signal reads its own.

Mapping into `CostBreakdown`:

| Field | Source |
| --- | --- |
| `fees_bps` | Σ `leg.fee_bps` |
| `spread_bps` | Σ `leg.spread_cost_bps` (zero in the touch-to-touch frame) |
| `slippage_bps` | Σ `leg.slippage_bps` — **walk-book only** |
| `other_bps` | Σ `leg.impact_bps` — **modelled impact only** |
| `latency_bps` | Σ `leg.latency_bps` |
| `hedge_bps` | the residual allowance, once |

Slippage and impact stay separate because they are separate models: slippage
is measured off the levels actually quoted, impact is assumed beyond them.
Summing them hid which half of an expensive trade was observed and which was
guessed.

## 11. `hedge_cost_bps`, made explicit

It is **not** the cost of the offsetting leg of a cross-venue trade: both legs
are priced explicitly against their own books, and charging a whole extra leg
would be a second helping of the same cost. It is the allowance for the delta
the platform is left holding when the two priced legs do not cancel exactly —
partial fills, size rounding, a leg that misses — which OKAPI then hedges out
with a separate order.

It is charged **once per opportunity, never per leg**, and is carried on
`SizePoint.hedge_bps` so the single charge is visible rather than implied.

## 12. Signal and confidence

```
floor      = max(zephr.min_net_edge_bps, risk.min_expected_edge_bps)
saturation = floor * zephr.signal_saturation_multiple      # 3.0
signal     = min(1.0, best.net_edge_bps / saturation)

bottleneck = min(leg.usable_liquidity for leg in best.legs)
credit     = min(1.0, bottleneck / zephr.confidence_liquidity_saturation)
confidence = zephr.confidence_floor + zephr.confidence_liquidity_weight * credit
```

A feasible point has `net_edge_bps >= floor`, so the signal is bounded below
by `1 / signal_saturation_multiple` and above by 1 — it is never negative for
a tradeable size. A refusal is `signal = -1`, `confidence =
zephr.refusal_confidence`.

Confidence uses the **bottleneck**: the trade is only as executable as its
thinnest leg, and a deep buy side cannot supply the sell side's missing depth.
Reading leg zero reported the first leg's book as the whole trade's, so an
opportunity with one deep venue and one nearly empty one came back close to
fully confident.

`ZephrConfig` validates `confidence_floor + confidence_liquidity_weight <= 1`,
so the liquidity term can never be clipped away into irrelevance.

## 13. Reason codes

| Code | Meaning |
| --- | --- |
| `EDGE_SURVIVES_EXECUTION` | a size clears every check |
| `SIZE_CAPPED_BY_DEPTH` | a larger rung failed on book depth |
| `SIZE_CAPPED_BY_EDGE_DECAY` | larger rungs were feasible but less profitable, or fell under the floor |
| `SIZE_CAPPED_BY_RISK_LIMIT` | the ladder itself was truncated at `risk.max_order_notional` |
| `NO_ECONOMICAL_SIZE` | nothing on the ladder is executable |
| `INSUFFICIENT_DEPTH`, `BELOW_MIN_TRADE_NOTIONAL`, `NET_EDGE_BELOW_FLOOR`, `NO_LEGS` | the distinct binding reasons, taken from the rungs |

The previous code asserted a liquidity cap whatever the cause. Depth and edge
decay are different facts about the market and now say so.

## 14. Health

HEALTHY requires at least one **configured symbol** with at least **two usable
venue states for that symbol** — the minimum a cross-venue quote needs. Some
usable venues but no such symbol is DEGRADED; no usable venue state at all is
OFFLINE.

Counting usable venue/symbol *pairs* across the whole market let one usable
venue on BTC-USD and one on ETH-USD add to HEALTHY while ZEPHR could price
neither symbol. ZEPHR is a required component, so a false HEALTHY there keeps
the strategy running on an agent that will answer nothing.

## 15. New configuration

All under `ZephrConfig`. No environment variables were added; none of these
were previously env-overridable either.

| Field | Default | Purpose |
| --- | --- | --- |
| `ttl_ms` | `2_000` | ZEPHR's own opinion TTL (was `noro.ttl_ms`) |
| `signal_saturation_multiple` | `3.0` | was inline `3.0` |
| `confidence_floor` | `0.4` | was inline `0.4` |
| `confidence_liquidity_weight` | `0.6` | was inline `0.6` |
| `confidence_liquidity_saturation` | `200_000.0` | was inline `200_000.0` |
| `refusal_confidence` | `0.9` | was inline `0.9` |

Every default reproduces the previous inline constant exactly, so none of
these fields is itself a behaviour change.

New validation: `size_ladder` must be non-empty, finite, positive and strictly
ascending; `confidence_floor + confidence_liquidity_weight <= 1`.

New derived property: `Settings.zephr_min_net_edge_bps`.

## 16. Behaviour changes, ranked by expected impact

1. **Removing the double-charged spread raises net edge on every
   opportunity.** This is the intended correction, and it is the largest
   change in the build. Magnitude: two legs × half the quoted spread. On the
   simulated market (`half_spread_bps = 1.5`, so `metrics.spread_bps ≈ 3.0`)
   that is roughly **3 bps of phantom cost removed per opportunity**, against
   a `min_dislocation_bps` of 4.0. Expect materially more opportunities to
   clear the floor and materially larger chosen sizes. **This is the single
   thing external validation should measure first.**
2. **The effective floor rises from 1 bps to 2 bps** (`max` with
   `risk.min_expected_edge_bps`), which is strictly stricter and partially
   offsets (1) at the margin. It also changes `signal`, whose saturation point
   moves from 3 bps to 6 bps of net edge — so signals are *lower* for the same
   net edge.
3. **Sizes below `risk.min_trade_notional` are no longer economical.** With
   the default ladder starting at 1 000 and the floor at 250, no default rung
   is affected.
4. **Confidence falls whenever the legs' books differ**, because the
   bottleneck replaces leg zero. It never rises.
5. **ZEPHR publishes no opinion when a leg has no exchange observation.**
   Previously it published one with a market-wide timestamp. The orchestrator
   then suspends on a missing required agent — fail-closed, and stricter.
6. **`intent.costs` reallocates impact from `slippage_bps` to `other_bps`.**
   `total_bps` is unchanged by the reallocation itself, and nothing outside
   the attribution builder reads anything but `total_bps`.
7. **Health can now report DEGRADED where it previously reported HEALTHY**, in
   exactly the case where ZEPHR could price nothing.

## 17. What was deliberately not changed

- **NORO.** No production NORO behaviour was touched. The Phase 3 findings
  P3-1 … P3-11 remain open by instruction. `NoroConfig.ttl_ms` is untouched;
  ZEPHR simply stopped reading it.
- **Consensus.** Weights, `entry_threshold`, `exit_threshold` and
  `required_agents` are untouched.
- **Risk limits.** `max_order_notional`, `min_trade_notional`,
  `min_expected_edge_bps`, exposure limits, loss limits and data-age limits
  are untouched. ZEPHR was made to respect them, not the reverse.
- **`PaperExecutor`.** Untouched. Its fill model was not adjusted to resemble
  ZEPHR's quotes.
- **The paper boundary.** No API keys, private endpoints, authenticated
  balances or orders, no live execution path.
- **Determinism.** No `time.time()`, `datetime.now()`, `random`, UUID
  generation outside the existing deterministic mechanisms, network call or
  LLM call was added. Every new value is a pure function of configuration and
  the market state on the event.

## 18. Replay and determinism

`Zephr.evaluate` still takes its time from `now_ms` — the logical time carried
on the `OPPORTUNITY_DETECTED` event — and never reads the clock. The new TTL
is a configured integer added to that same logical time, so an opinion's
lifetime is identical under replay.

`source_data_timestamp` now comes from `MarketState.source_data_timestamp_for`,
a pure function of the replayed market state.

One consequence for Phase 2: **`ZephrConfig` gained fields, so the material
configuration digest changes.** Recordings made before this commit will fail
`_check_configuration` against a current digest — correctly, because the cost
model genuinely changed and a replay under the new model is not the same run.
Nothing about the digest mechanism was modified.

## 19. Known test impact

No test was created, modified or executed in this pass. Static reading says:

- `tests/unit/test_pricing.py::TestSizingCurve::test_quote_leg_reports_a_maker_fee_when_passive`
  asserts `maker.spread_bps == 0.0 and taker.spread_bps > 0`. `spread_bps` is
  retained as a deprecated property aliasing `crossed_spread_bps`, which keeps
  exactly those values, so this assertion should still hold.
- `test_thin_edge_yields_no_economical_size` uses a 1 bps gross edge against
  two legs at `taker_bps=1.0` plus a 1 bps hedge allowance, so costs exceed
  the edge with or without the spread term; it should still find no economical
  size.
- `test_net_edge_decays_with_size`, `test_max_economical_size_is_the_largest_feasible_point`,
  `test_thin_book_makes_large_sizes_infeasible` and
  `test_costs_are_charged_on_every_leg` do not depend on the spread term.
- `tests/contract/test_json_safety.py` exercises infinite impact and exhausted
  books; the infeasibility paths that produce them are unchanged in outcome,
  only in the reason reported.
- Any test asserting an exact `expected_net_edge_bps`, an exact ZEPHR
  `confidence`, an exact ZEPHR `signal`, or a specific ZEPHR reason code will
  need recalibrating against the corrected cost model.

These are predictions from reading, not results. They are exactly what the
external validator should check first.

## 20. Open items not addressed in this pass

- Maker execution remains unmodelled (no queue position, no fill probability).
  ZEPHR prices taker only, and says so; a maker-capable ZEPHR is a design
  question, not a hardening one.
- `usable_liquidity` is the whole visible book side, not a depth windowed to a
  bps distance from mid. NORO uses a windowed measure; the two therefore mean
  different things by "liquidity". Unified only by documentation here.
- `market_impact_bps` returns `inf` for zero depth, which propagates to an
  infinite cost and a `-inf` net edge. Correct and serialisation-safe, but it
  means "no depth" and "catastrophic impact" are the same number.
- The residual hedging allowance is a flat configured constant, not a function
  of expected fill asymmetry.

## 21. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing in this pass was executed: no `pytest`, no `ruff`, no `mypy`, no
import check, no script, no container, no replay, no orchestrator run. The
build is static work only, and every statement in this document is a claim
about code that an independent validator must confirm.
