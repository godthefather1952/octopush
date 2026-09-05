# TIDAL microstructure abstention

One change, isolated on purpose: **how weak microstructure evidence
participates in consensus**. The microstructure score itself is untouched.

- **Base SHA:** `c9ab846d822c024f1c22276f89b712cc6317af37`
- **Branch:** `phase34-tidal-integration`
- **Production files changed:** `agents/tidal/agent.py`, `core/config/settings.py`,
  `core/config/__init__.py` (export only). Paper trading only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. What the calibration measured

The probe committed in `c9ab846` was executed externally over 500 ticks of the
default two-venue simulation.

| | |
| --- | --- |
| opportunities | 206 |
| complete consensus results | 206 |
| entries / trade intents / RUNE evaluations / orders / fills | **0** |

**NORO** — 206 opinions, abstention rate 1.0000, zero non-zero signals.
Correct for a topology with no independent third venue.

**ZEPHR** — signal 1.0000 at every percentile on all 206. Confidence 1.0000
throughout. Expected net edge p50 19.224 bps, p90 21.363, p95 22.194, max
23.759. Cost p50 13.167 bps. Chosen notional p50 $25,000.
`NO_ECONOMICAL_SIZE` 0%, `EDGE_SURVIVES_EXECUTION` 100%.

**TIDAL** — signal min −0.1263, p25 −0.0784, **p50 −0.0566**, p75 −0.0319,
p95 +0.0117, **max +0.0608**. Confidence 0.9000 on all 206. Volatility penalty
p50 0.0522, p95 0.0598, max 0.0662. Buckets: 186 in `[−0.25, 0)`, 20 in
`(0, 0.10)`, and **nothing at all at or beyond ±0.10**.

**Consensus** — min 0.4858, p50 0.5176, **max 0.5713** against a 0.60
threshold. 206 results ≥ 0.45, 184 ≥ 0.50, 10 ≥ 0.55, **0 ≥ 0.60**.

**Binding** — every one of the 206 had ZEPHR ≥ 0.60 and consensus < 0.60. The
TIDAL signal required to reach entry was **+0.1238** (median and minimum
alike), against an observed maximum of **+0.0608**.

## 2. Why TIDAL was the binding agent

No observed TIDAL state could satisfy the two-agent active threshold. The
required signal was roughly twice the strongest reading the agent ever
produced, so this was not a marginal miss — the active set could not reach 0.60
from anywhere in TIDAL's realised range.

And the readings doing the blocking were not evidence. A median of −0.0566 on a
scale where ±1 is a decisive read is noise around zero: book imbalance and
trade flow fluctuate, and a small negative number is the *absence* of a
directional claim, not the claim "this trade is slightly bad". Published as a
directional vote, that noise carried TIDAL's full weight × confidence
(1.4 × 0.9 = 1.26) into the numerator with a negative sign, against ZEPHR's
1.5 × 1.0 × 1.0. The result was a systematic drag built out of nothing.

This is the same class of error the previous pass fixed for NORO, in a
different disguise. There, an agent with nothing to say contributed weight to
the *denominator*. Here, an agent with nothing to say contributed a small
negative number to the *numerator*. Both make "I have no view" arithmetically
indistinguishable from "I disagree".

## 3. Why the 0.60 threshold was NOT changed

Because the measured failure is semantic. Lowering the threshold would have
made the platform trade while leaving noise in the vote — the next market
regime with slightly different noise would have moved the goalposts again, and
the calibration would have to be redone against a number chosen to fit one
run's arithmetic.

The threshold, the four agent weights, the exit threshold and the required-agent
list are all unchanged.

## 4. Why ZEPHR, NORO and RUNE were NOT changed

- **ZEPHR** — 206/206 survived execution at signal 1.0 with 19–24 bps of
  post-cost edge. It is not the blocker; there is nothing to fix.
- **NORO** — 206 opinions, 206 abstentions, 0 non-zero signals. Exactly as
  designed for a two-venue topology.
- **RUNE** — received **zero** trade intents, because consensus blocked before
  risk ran. There is no evidence it caused anything, and changing a risk limit
  on no evidence is how limits stop meaning anything.
- **Simulation** — unchanged. Two venues is a legitimate operating topology and
  the architecture has to behave sensibly on it; adding a third venue or
  manufacturing directional depth would hide the question rather than answer
  it.

## 5. Missing, abstaining, informative

Three states, already modelled generically by `AgentOpinion.abstain` and
already understood by `ConsensusEngine`. TIDAL now uses that mechanism; no new
abstention model was added, and consensus learned nothing TIDAL-specific.

| State | TIDAL condition | Result |
| --- | --- | --- |
| **Missing** | a leg's book is absent or not usable | `evaluate` returns `None`; a required agent missing suspends the strategy |
| **Abstaining** | book read successfully, `abs(raw) < threshold` | present, fresh, healthy, inconclusive — zero numerator, zero denominator, no contribution row |
| **Informative** | `abs(raw) >= threshold`, either sign | votes normally at its full weight |

An inability to inspect the market is *missing*. Weak evidence is *abstention*.
Keeping those distinct is the whole point.

## 6. The deadband

```
raw_signal = clamp(mean(0.7·imbalance·side_sign + 0.3·flow·side_sign) − vol_penalty)

abstain    = abs(raw_signal) < tidal.informative_signal_threshold
signal     = 0.0 if abstain else raw_signal
```

The score is computed exactly as before — same inputs, same coefficients, same
volatility penalty, applied in the same order. Only the last step is new.

**Threshold default: 0.10.** An information-strength boundary, chosen to clear
the near-zero band the calibration observed (median −0.057, max +0.061) while
leaving materially adverse microstructure free to veto.

It is deliberately **not** 0.1238, the signal the algebra demanded for entry.
Tuning a deadband to the entry requirement would make it an entry threshold
wearing a different name, and the point of putting it in TIDAL is that it
answers a question about *information*, not about *entry*. `TidalConfig`
validates it as finite, `>= 0` and `< 1`.

## 7. Boundary semantics

The deadband is the **open** interval `(−threshold, +threshold)`:

| `raw_signal` | Behaviour |
| --- | --- |
| `abs(raw) < threshold` | abstains |
| `raw == +threshold` | **votes**, positive |
| `raw == −threshold` | **votes**, negative |
| `abs(raw) > threshold` | votes |

A threshold of `0.0` therefore never abstains — `abs(x) < 0` is false for every
real number — which restores the pre-change behaviour exactly and gives
external validation a clean control.

## 8. Reason codes

| Code | When |
| --- | --- |
| `MICROSTRUCTURE_INCONCLUSIVE` | abstaining |
| `MICROSTRUCTURE_SUPPORTS` | voting, `raw > 0` |
| `MICROSTRUCTURE_UNSUPPORTIVE` | voting, `raw <= 0` |
| `VOLATILITY_EXCEEDS_EDGE` | `vol_penalty > 0.3`, on votes and abstentions alike |

An abstention emits neither `SUPPORTS` nor `UNSUPPORTIVE` — it is not making
that claim. A fast market is still flagged while abstaining, because the
directional read being inconclusive does not make the volatility less real.

**No reason code affects consensus participation.** Only `abstain` does; the
engine never reads reason strings.

The pre-deadband score is preserved in
`detail["raw_microstructure_signal"]`, alongside
`detail["informative_signal_threshold"]`. Abstention withholds the vote, not
the observation.

## 9. Confidence

Unchanged, and deliberately so. It still means *the market observation is
reliable* — freshness-derived, 0.90 throughout the calibration. Whether that
observation is directionally informative is a separate dimension, carried by
`abstain`. Collapsing the two by driving confidence to zero would hide the
semantics behind a number an informative model could legitimately report.

## 10. Attribution

An abstaining TIDAL opinion produces no `AgentContribution`, so it carries no
contribution share, weight, signal or confidence into `TradeAttribution`, and
the `Scorecard` never sees it. TIDAL is not credited with predicting a trade it
declined to have a view on. When TIDAL votes it receives normal attribution.
No zero-weight rows are fabricated; the abstention is observable through
`ConsensusResult.abstained_agents`.

## 11. Replay and the config digest

Fully deterministic: the decision depends only on the raw signal and a
configured constant. No clock read, randomness, network call, LLM call or
background task. `AgentOpinion.abstain` is serialised, so replay sees the exact
decision that was made rather than recomputing it.

Adding `TidalConfig` changes the **material configuration digest**. Recordings
made before this commit will not qualify for exact same-config replay under the
new settings digest — correctly, because strategy decision semantics genuinely
changed. Digest enforcement is not weakened, and no replay production code was
modified.

## 12. Expected behaviour after this build

On the measured two-venue topology, NORO abstains for want of an independent
benchmark and TIDAL abstains on readings inside the deadband, leaving ZEPHR's
execution economics to decide — subject to the unchanged 0.60 threshold and
then to RUNE. There is no bypass and no two-venue shortcut: if the only voting
agent is unconvinced, no trade happens.

No trade count is encoded in production. External validation will measure the
actual frequency.

## 13. Deferred

- **TIDAL source-timestamp semantics.** `evaluate` stamps
  `self.state.source_data_timestamp` — the market-wide newest observation —
  rather than the oldest among the opportunity's own legs. That is the same
  shape as the P3-7 finding closed for NORO, and `source_data_timestamp_for`
  already exists to fix it. Deliberately **not** combined with this pass: it is
  a freshness question, not a participation one, and mixing them would make the
  calibration effect impossible to isolate.
- **The microstructure formula itself.** The 0.7/0.3 blend and the volatility
  penalty are untouched, so external validation can attribute any change to the
  deadband alone.
- **Whether the deadband should be venue- or symbol-scoped.** A single global
  constant is the simplest thing that answers the measured problem.

## 14. Tests

Added `tests/unit/test_tidal_abstention.py`: weak positive and negative
readings abstain (including every value in the calibration's observed band);
strong readings in both directions vote; exact `±threshold` votes; a zero
threshold never abstains; a missing or unusable book still returns `None`; the
raw score and threshold are preserved in detail; volatility flags ride along on
both votes and abstentions and never decide participation; `TidalConfig`
validation; determinism and serialisation.

Added to `tests/unit/test_consensus.py`: `TestTidalAndNoroBothAbstain` (both
abstentions leave the result complete, both reported as abstaining and neither
missing, ZEPHR the only contribution, score equal to ZEPHR's own, entry allowed
on the unchanged threshold, and a weaker lone ZEPHR still blocked) and
`TestTidalRetainsItsVeto` (a voting TIDAL appears in contributions, an adverse
read lowers the score below ZEPHR alone, a strongly adverse read blocks entry).

Fixed the Ruff `SIM102` nested conditional in
`tests/unit/test_two_venue_consensus_calibration.py`. Test-only, no semantic
change. **The calibration probe is retained and its
`assert entry_count > 0` is left strict** — it is now a genuine integration
regression, and if the new semantics still produce zero entries, CI will print
a fresh diagnostic.

## 15. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

No `pytest`, `ruff`, `mypy`, container, replay or application start was
executed. Every expectation here was derived by reading the implementation.
