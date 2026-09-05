# Phase 3 + 4 integration — explicit consensus abstention

Phases 3 and 4 each corrected an agent. Putting both corrections in the same
platform exposed a hole in the layer that combines them:

> An agent that answered the question and has nothing to add is neither
> **missing** nor **neutral**. It is **abstaining**, and consensus had no way
> to say so.

- **Base SHA:** `ce519fc86b4a245cda6cc976ab41b7f4ffcf4cf1`
- **Branch:** `phase34-consensus-integration`
- **Scope:** `core/models/agent.py`, `strategies/consensus/engine.py`, one line
  and its documentation in `agents/noro/agent.py`, plus tests. Paper trading
  only; no live execution anywhere.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. What Phase 3 corrected (NORO)

NORO was grading its own homework. The cross-venue detector picks the cheapest
ask and the richest bid; a fair value formed from those same two prices lands
between them by construction, so both legs always looked confirmed — 82 of 82
production entry votes positive, none informative. Phase 3 rebuilt the
benchmark from venues *not* participating in the opportunity, made the weakest
leg govern outright, bounded the depth weighting, and had NORO report honestly
when no independent venue exists.

## 2. What Phase 4 corrected (ZEPHR)

ZEPHR charged half the quoted spread on every taker leg on top of a gross edge
the detector had already measured touch to touch, so the crossing was paid for
twice. Phase 4 made the reference frame explicit, charged each cost exactly
once against it, and tightened ZEPHR's floor to the one RUNE would actually
apply. It passed external validation before the NORO remediation landed and is
untouched here.

## 3. External validation outcome

Independent validation of `ce519fc`:

| Suite | Result |
| --- | --- |
| Python 3.12 full | **2136 passed, 18 failed, 2 skipped** |
| Python 3.11 unit/contract | 1251 passed, 1 failed, 126 skipped |
| Ruff | PASS |
| Mypy core | PASS |
| Paper boundary | PASS |
| Real Redis contracts | PASS |
| Real PostgreSQL contracts | PASS |

The failures cluster. Most are not separate defects.

## 4. Root cause of the no-trade cluster

`ConsensusEngine.combine` computes a weighted mean:

```
score = Σ(weight · confidence · signal) / Σ(weight · confidence)
```

Corrected NORO on a two-venue market reports `signal 0.0`, `confidence 0.1`,
`INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH`. The engine treated that as an
ordinary directional opinion, so NORO contributed:

```
numerator   += 1.5 · 0.1 · 0.0  =  0
denominator += 1.5 · 0.1        =  0.15
```

**A weighted mean divides by the weights it summed.** An agent adding zero to
the numerator while adding its weight to the denominator is not neutral — it
is voting *against* whatever the other agents concluded, in proportion to its
own weight. NORO's honest "I don't know" was arithmetically indistinguishable
from "I disagree".

Concretely, with TIDAL at `weight 1.4 · confidence 1.0 · signal 0.2` and ZEPHR
at `1.5 · 1.0 · 1.0`:

| | numerator | denominator | score |
| --- | --- | --- | --- |
| NORO counted as a neutral vote | 1.78 | 3.05 | **0.584** |
| NORO abstaining | 1.78 | 2.90 | **0.614** |

Against an entry threshold of 0.60 those two land on opposite sides. The
default two-venue synthetic system detected opportunities and produced no
fills, and every downstream test that requires a real trade failed with it.

## 5. Missing vs neutral vs abstain

| State | Meaning | Required-agent completeness | Scoring mass |
| --- | --- | --- | --- |
| **Missing** | no usable opinion — offline, stale, unable to read its inputs | **fails**; trading suspends | none |
| **Informative** | the agent has evidence and is voting on it, including a genuine `signal = 0` | satisfied | full |
| **Abstaining** | the agent evaluated successfully but lacks the independent information a directional claim needs | satisfied | **none** |

Conflating any two of these corrupts the result. Treating an abstention as
missing suspends a strategy whose agents are all working. Treating it as a
neutral vote suppresses the score, which is the defect above.

Confidence is a different dimension and is deliberately **not** used as a
substitute. Setting `insufficient_breadth_confidence = 0` would have produced
the same arithmetic here while hiding the semantics: an informative model can
legitimately report zero confidence, and an abstention is a statement about
participation, not certainty.

## 6. `AgentOpinion` schema change

```python
abstain: bool = False
```

Default `False`, so no agent that never abstains needs any change (TIDAL,
ZEPHR, LUMEN, OKAPI are untouched) and opinions serialised before the field
existed still validate.

## 7. `ConsensusResult` schema change

```python
abstained_agents: list[AgentId] = Field(default_factory=list)
```

Deliberately separate from `missing_agents`: an abstaining agent answered and
is not a hole in the data, so overloading "missing" with it would suspend the
strategy exactly when the agent works as designed. Defaults to `[]` so results
recorded before this field existed still parse.

## 8. Scoring semantics

In `ConsensusEngine.combine`, after the missing and stale/unavailable checks
and after degraded bookkeeping:

```python
if slot.opinion.abstain:
    abstained.append(agent)
    continue
```

- No numerator contribution.
- No denominator contribution.
- Not marked missing.
- Still recorded in `degraded_agents` when its data was degraded — freshness
  and participation are orthogonal, and a degraded abstention is honestly both.
- No `AgentContribution` row, so it cannot influence attribution shares.

The engine reads **only** the generic `abstain` flag. It contains no reference
to `INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH` or any other agent's
vocabulary: each agent decides when it has nothing to add and says so in the
one field every agent shares.

**All-abstain** needs no special case. `weight_sum == 0` already yields
`score = 0.0`, so `agreement = 0.0` and `entry_allowed` is `False` — a result
can be complete and still, correctly, never traded.

## 9. Required-agent semantics

`complete = not missing_required`, unchanged. Completeness is about whether a
required agent was **present and usable**, never about whether it cast a
directional vote.

```
required + abstaining  →  COMPLETE
required + missing     →  INCOMPLETE
required + stale       →  INCOMPLETE
```

## 10. Attribution semantics

An abstaining agent produced no `AgentContribution`, so it carries no
contribution share, weight, signal or confidence into `TradeAttribution`, and
the `Scorecard` — which reads `attribution.signals` — never sees it. That falls
out of the design rather than needing a special case, and it is the truthful
outcome: scoring an abstention would credit the agent with predicting a trade
it explicitly declined to have a view on.

The abstention remains observable through `ConsensusResult.abstained_agents`.
No zero-weight contribution row is fabricated, because a row carrying weight is
precisely what re-creates the suppression defect.

## 11. Why thresholds and weights were NOT changed

Untouched: TIDAL 1.4, NORO 1.5, ZEPHR 1.5, LUMEN 0.5; `entry_threshold` 0.60;
`exit_threshold` 0.45; `required_agents` unchanged. No RUNE limit or gate
changed.

The weighted mean already renormalises over the agents that actually voted.
With abstention modelled explicitly, TIDAL and ZEPHR determine the score when
NORO has no independent evidence — which is the correct behaviour, and needs no
recalibration to reach. Retuning weights or thresholds to compensate for a
representation bug would have hidden the bug and left the calibration wrong for
every market where NORO *does* have evidence. If external validation shows the
truthful active-agent score still cannot reach the threshold, that is a
calibration question to answer with measurements, later.

## 12. Why no fake third venue was added

`default_market` and the simulated venue set are unchanged. Two venues is a
legitimate operating topology, and the production architecture has to behave
sensibly on it. Adding a third venue to the fixture so that tests trade would
have hidden the real question behind scenery, and left the platform untested on
the topology it actually ships with.

## 13. Tests updated

| File | Change |
| --- | --- |
| `tests/unit/test_consensus.py` | New `TestAbstention` covering a required abstention (complete, reported, excluded from the denominator, no contribution row, inert signal and confidence), a missing required agent, a stale-and-abstaining agent resolving to missing, abstention vs directional neutral scoring differently, all-abstain, a non-required (LUMEN) abstention, and a degraded abstention. New `TestAbstentionDefaults` for the `False` default, legacy-payload validation, round-tripping, and `ConsensusResult` backward compatibility |
| `tests/audit/test_noro_information_value.py` | Two-venue opinion asserts `abstain is True` and survives serialisation; every evidenced verdict (confirm, contradict, genuine zero) asserts `abstain is False` |
| `tests/audit/test_noro_production_behaviour.py` | Every opinion in the real two-venue run is flagged as an abstention |
| `tests/audit/test_noro_causal_alignment.py` | Rebuilt on three venues so alignment is observable: the published opinion carries the benchmark from the snapshot in force at detection, and a later snapshot cannot retro-change an already-published opinion |
| `tests/audit/test_noro_microprice_and_config.py` | Confidence-stability test rewritten with **linear** liquidity increments through and past saturation, asserting monotonicity, no downward discontinuity, flatness after saturation, an unchanged signal and bounded values — replacing a geometric sweep whose fixed 0.02 step bound was tripped at 0.0208 by arithmetic, not by a discontinuity |
| `tests/failure/test_failures.py` | Missing-required-agent injection now suppresses `Noro.evaluate`. Clearing `fair_values` stopped removing NORO when v0.2 began evaluating straight from `self.market`, so the test had quietly stopped injecting anything |
| `tests/integration/test_pipeline.py` | Scorecard test no longer requires NORO to have predictive statistics, and asserts every scored agent genuinely voted; a new test pins that an abstention appears in neither contributions nor weights |

Trade-requiring assertions in `test_failures.py`, `test_pipeline.py`,
`test_replay.py`, `test_replay_batch12_equivalence.py` and
`test_tick_time_invariant.py` are left strict — no skips, no xfails, no removed
fill or order assertions. They are the canaries for whether explicit abstention
restores a tradeable two-venue system.

## 14. Replay compatibility

Determinism is preserved. Abstention is read from the deterministic
`AgentOpinion` payload and nothing else — no clock read, no randomness, no
network or LLM call — so the same opinion stream produces the same consensus
output under replay. No replay production code was modified. Both new fields
carry defaults, so recordings made before this commit parse unchanged; the
`ZephrConfig`/`NoroConfig` digest changes noted in the Phase 3 and Phase 4
documents are unaffected by this pass.

## 15. Validation status

**TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

Nothing here was executed: no `pytest`, no `ruff`, no `mypy`, no import check,
no script, no container, no replay, no application start. The arithmetic in
section 4 is derived from the configured weights and the engine's formula, not
measured. Whether the restored score clears 0.60 on the live simulated market
is the first thing external validation should establish.
