# Phases 9 + 10 — two kinds of imbalance

Two framework layers, built in one pass because they answer the same shape of
question about two different things.

**Phase 9 frames the platform's response to PORTFOLIO imbalance.** A strategy
intends to carry some exposure; it ends up carrying another. OKAPI measures the
gap and asks for a trade that closes it. Phase 9 gives that request a record: a
lifecycle, a link to the plan and orders that worked it, and an explicit
UNKNOWN state for a hedge whose venue-side truth nobody knows.

**Phase 10 frames the platform's response to INFORMATION imbalance.** The world
knows things the price has not finished expressing. LUMEN reads what is being
said and returns a structured judgement about it. Phase 10 gives that judgement
a provenance trail: what evidence was available, what was asked, what came
back, and which opinion reached the bus.

**Neither changes a current decision.** OKAPI still measures residuals and
proposes hedges exactly as it did; LUMEN still gathers the same context, calls
the provider once, and converts a successful response with the same formula.
Both registries are written to beside code that already decided, and read by
nothing that decides.

They are also independent of each other. There is no `OKAPI → LUMEN` or
`LUMEN → OKAPI` dependency, and there must not be: they operate at different
layers. Analytical opinions meet at the consensus engine; execution and risk
meet at RUNE and VESKA.

| | Phase 9 | Phase 10 |
| --- | --- | --- |
| Imbalance | portfolio exposure | information environment |
| Authority, unchanged | `desired_delta`, `DeltaReport`, `_hedge_venue`, `hedge_available` | `SYSTEM_PROMPT`, `_context`, `_to_opinion`, `IntelligenceProvider` |
| Record added | `HedgeRegistry`, `HedgeTargetRegistry` | `IntelligenceRegistry`, `IntelligenceProviderDirectory` |
| Unresolved state | `HedgeRequestStatus.UNKNOWN`, never auto-resolved | `UNAVAILABLE` vs `FAILED`, never a fabricated neutral opinion |
| Seam built, empty | live hedge execution | external information sources |
| Gates anything? | no — RUNE still calls `hedge_available` | no — LUMEN remains optional |

Full detail: [`phase9-okapi-framework.md`](phase9-okapi-framework.md) and
[`phase10-lumen-framework.md`](phase10-lumen-framework.md). Both carry a
VALIDATION DEFERRED section listing what this pass does **not** establish.
