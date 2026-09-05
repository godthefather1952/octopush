# Two-venue consensus calibration probe

A diagnostic, not a change.

- **Base SHA:** `70e90ca8b031fd18c31477881e0ecc944bd23a88`
- **Branch:** `phase34-consensus-integration`
- **Production changes:** **NONE.** The diff touches `tests/` and `docs/` only.
- **Testing status:** **TESTS NOT RUN — EXTERNAL VALIDATION REQUIRED.**

---

## 1. Production is unchanged

No file under `agents/`, `core/`, `strategies/`, `apps/`, `risk/`,
`execution/`, `simulation/` or `replay/` is modified. No weight, threshold,
formula, venue count or market parameter is touched, and no existing
trade-required assertion is weakened. `git diff --name-only` against the base
returns only `tests/unit/test_two_venue_consensus_calibration.py` and this
document.

## 2. Why the probe exists

External validation of `70e90ca` reports **2170 passed, 16 failed, 2 skipped**
on the full suite (1272 / 1 / 126 on the 3.11 unit and contract job), with
Ruff, Mypy core, the paper boundary and the real Redis and PostgreSQL contract
suites all passing.

The remaining cluster has one shape: the default two-venue simulation produces
opportunities, and zero orders and zero fills.

Explicit abstention removed NORO from the consensus denominator, so the
previously identified suppression — an agent contributing nothing to the
numerator while contributing its weight to the divisor — is gone. Something
else is now binding, and the honest next step is to measure which stage stops
the pipeline rather than guess and patch.

That distinction matters because every plausible guess implies a different and
largely irreversible change: lowering the entry threshold, re-weighting the
agents, loosening a RUNE limit, or adding a third venue to the fixture are not
interchangeable, and three of the four would hide the answer instead of
producing it.

## 3. What the probe does

`tests/unit/test_two_venue_consensus_calibration.py` drives the **real**
platform — the seeded synthetic market, TIDAL, NORO, ZEPHR, the real
`ConsensusEngine`, RUNE and the paper executor — for 500 ticks with no agent
mocked and no setting overridden, subscribes to every event on the decision
path, and reports.

Captured event types:

```
OPPORTUNITY_DETECTED   AGENT_OPINION        CONSENSUS_UPDATED
TRADE_INTENT           RISK_PASS            RISK_FAIL
PAPER_ORDER_CREATED    PAPER_FILL
```

## 4. Metrics collected

**Abstention.** NORO opinion count, abstention rate, and a check that every
two-venue opinion carries `abstain=True`, `signal == 0.0` and
`INSUFFICIENT_INDEPENDENT_VALUATION_BREADTH`. Expected to be ~100% in this
topology, because both usable venues are always the opportunity's own legs.

**Distributions** (count, min, p25, p50, p75, p90, p95, p99, max, mean, stdlib
only): TIDAL signal, ZEPHR signal, consensus score. Plus min/mean/max
confidence for TIDAL and ZEPHR, TIDAL `vol_penalty` at p50/p95, and ZEPHR
`expected_net_edge_bps` at p50/p90/p95/max alongside cost and chosen notional.

**Bucket histograms.** TIDAL signal across `< -0.25`, `-0.25..0`, `0`,
`0..0.10`, `0.10..0.20`, `0.20..0.40`, `>= 0.40`. ZEPHR signal across `< 0`,
`0..0.50`, `0.50..0.75`, `0.75..1.0`, `1.0`, with the `NO_ECONOMICAL_SIZE`
refusal rate and the `EDGE_SURVIVES_EXECUTION` survival rate.

**Threshold proximity.** Max observed score, the closest gap to the unchanged
0.60 entry threshold, and counts at ≥ 0.45, 0.50, 0.55 and 0.60.

**The binding agent.** For every consensus result where NORO abstained and both
TIDAL and ZEPHR voted, the probe recomputes the active-agent weighted score
from the published contribution rows and asserts it matches the published
score to within 1e-9. That check is load-bearing: if NORO were still carrying
mass, every "required TIDAL" figure below would be wrong. It then solves

```
score = (w_t·c_t·s_t + w_z·c_z·s_z) / (w_t·c_t + w_z·c_z)
```

for the TIDAL signal that would reach 0.60, and reports the median observed
signal, the median and minimum required signal, and the median shortfall —
plus a count of results where ZEPHR alone voted ≥ 0.60 and consensus still
came in below it.

**Pipeline counts and blocker.** Opportunities, complete consensus results,
entries, trade intents, risk passes and fails, orders and fills, classified as
`CONSENSUS` / `ORCHESTRATOR` / `RUNE` / `EXECUTION` / `NONE`.

## 5. How the result is delivered

The probe ends with one assertion:

```python
assert entry_count > 0, diagnostic_report
```

It is *intended* to fail while the pipeline is blocked, carrying the full
report as the failure message so CI prints the numbers without anyone
re-running anything. If the platform does reach an entry, it passes silently
and the report is not needed.

The supporting assertions — the run produced something to measure, NORO
abstains everywhere, an abstention is never also reported missing, and the
abstainer carries no scoring mass — exist so a NaN-filled or misattributed
report cannot be mistaken for a finding.

## 6. What happens next

**The builder ran nothing.** No `pytest`, `ruff`, `mypy`, container, replay or
application start. Every number in the report will be produced by external
validation, not by this pass.

The measurements decide the next production change, and this document
deliberately does not pre-empt them. If the report shows consensus scores
clustered just under 0.60 with TIDAL structurally near-neutral, the question is
whether a threshold calibrated against a three-agent ceiling still fits a
two-agent active set. If it shows ZEPHR refusing on economics, the question is
about the cost model. If it shows entries clearing and RUNE rejecting, the
question is a risk limit. Those are different repairs, and choosing between
them on evidence is the entire point of the probe.
