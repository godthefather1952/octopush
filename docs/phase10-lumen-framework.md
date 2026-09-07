# Phase 10 — LUMEN intelligence framework

**Status: FRAMEWORK CONSTRUCTED. NOT VALIDATED.**

Phase 10 is a strict construction pass. It adds a way for the platform to
answer, after the fact: *what did LUMEN see, who did it ask, what came back, and
which opinion did that produce?*

Until now the answer was nowhere. The context existed for the length of one
provider call, the response existed for the length of one provider call, and
neither survived it.

It changes no analysis. The system prompt, the response schema, the context
selection, the turbulence formula, the signal, the TTL bounds and the health
thresholds are all exactly as they were.

> **The line this phase must not cross.** LUMEN's turbulence formula is the one
> thing LUMEN exists to compute. A record that re-derived it would be a second
> implementation of it, and the untested copy would eventually be read as the
> answer. So the registry stores the provider's *raw readings* and the
> published signal *as published*, and derives neither.

---

## 1. Current LUMEN behaviour — preserved exactly

Unchanged by this phase, in full:

| Concern | Where it lives, still |
| --- | --- |
| Prompt | `SYSTEM_PROMPT`, byte-for-byte |
| Schema | `RESPONSE_SCHEMA`, byte-for-byte |
| Context selection | `_context`: symbol, `as_of_ms`, last 10 headlines published within the hour, coarse market summary |
| Provider call | one call, in `evaluate` |
| Signal | `_to_opinion`: `turbulence = min(1.0, 0.6·attention + 0.4·|direction| + (0.4 if shock))`, `signal = -turbulence` |
| Confidence | clamped to [0, 1] |
| TTL | `max(5_000, min(900_000, ttl_seconds · 1000))` |
| Reason codes | `NEGATIVE_INFORMATION_SHOCK`, `QUIET_INFORMATION_ENVIRONMENT` |
| Failure | no opinion published, ever |
| Malformed | `_to_opinion` returns `None`, counts a failure |
| Publication | `run_once` publishes `AGENT_OPINION` |
| Cadence | `run_forever`, sleeping `poll_interval_s` |
| Health | `_heartbeat`: OFFLINE at threshold, DEGRADED below, else HEALTHY |
| Providers | `NullProvider`, `ScriptedProvider`, `ClaudeProvider`, all unchanged |
| Selection | `build_provider(kind, ...)`, by configured name |

The diff over `agents/lumen/agent.py` removes exactly **one** construct: the
inline `IntelligenceRequest(...)` inside the `analyze(...)` call. It is now
bound to a local first and passed to the same single call — see §5.

`VERSION` stays `lumen-0.1`. It is written into every opinion's
`model_version` field, so bumping it would change a value on the wire for a
purely cosmetic reason. The public surface changed; the published record did
not.

## 2. Slow-loop philosophy

LUMEN runs on the slow loop, measured in seconds, never per market tick. **The
fast loop must never wait on a model.** Nothing in this phase moves work toward
the fast loop, and the registry writes happen inside the existing slow-loop
call.

`IntelligenceLoopStatus` and `IntelligenceLoopSnapshot` describe the loop.
`Lumen.loop_snapshot(now_ms)` derives them from state that already exists —
`last_call_ms`, `consecutive_failures`, the configured interval, the registry's
counters. `run_forever` is untouched, which means RUNNING and SLEEPING are not
distinguishable from outside it: the snapshot reports IDLE before the first
call, DEGRADED while the provider is failing, and SLEEPING otherwise. Adding a
status assignment inside the loop would be rewriting the loop.

## 3. Provider abstraction

`IntelligenceProvider.analyze(request)` remains the interface, unchanged, and
there is deliberately **no second model-provider abstraction**. A future
provider implements the one that exists.

`IntelligenceProviderCapabilities` describes what a provider is like:
structured output, JSON schema support, networked, deterministic, replay-safe,
closable. The shipped three:

| Provider | networked | deterministic | replay_safe |
| --- | --- | --- | --- |
| `NullProvider` | no | yes | yes |
| `ScriptedProvider` | no | yes | yes |
| `ClaudeProvider` | yes | no | no |

**Nothing dispatches on any of it.** `IntelligenceProviderDirectory` never
switches provider on failure, never skips one on its capabilities, and never
retries. Automatic failover would silently change which model produced a
reading, and a comparison across a session would then be comparing two
different analysts. `ClaudeProvider` keeps its own `max_retries`; no second
retry layer sits above it.

`ClaudeProvider` is untouched: model, retries, client lifecycle, request
format, JSON extraction and latency measurement all as they were. No new SDK
use, no new secret.

## 4. Evidence architecture

```
    (future) source ──► IntelligenceEvidence ──► IntelligenceEvidenceBundle ──► LUMEN
```

rather than one bespoke ingestion path per feed.

`IntelligenceEvidence` records provenance: kind, source, title, publication
instant, capture instant, symbol, a body **excerpt**, and an optional publisher
id and reference string. Nothing dereferences the reference. A record retaining
whole articles would be sized by how much news happened rather than by how much
the platform looked at.

`IntelligenceSourceKind` has eight values; three are reachable — `HEADLINE`,
`MARKET_CONTEXT`, `PROVIDER`. The rest are vocabulary.

`IntelligenceEvidenceBundle` describes what one analysis had available.
`complete` means the capture ran to completion. It does **not** mean the
evidence was sufficient or representative — no model here can tell whether the
platform saw the story that mattered.

### `NewsItem` compatibility

`NewsItem` stays exactly where it is, in `agents/lumen/agent.py`, and every
current caller keeps working. `evidence_from_news_item` in
`agents/lumen/source.py` is the pure adapter to the neutral vocabulary.

That is option B of the two the brief offered, and compatibility is why: one
canonical representation is desirable eventually, and breaking every existing
producer to get there today is not.

### Deduplication

`evidence_identity` is deliberately shallow — the publisher's id when there is
one, otherwise source + publication time + title. It catches the same item
arriving twice down the same pipe. It will **not** catch the same story
reported by two outlets, re-headlined, or updated in place. Aggressive semantic
deduplication is a judgement about meaning, and a wrong merge silently deletes
information the platform was given.

### Freshness

`EvidenceFreshness` is FRESH / STALE / UNKNOWN, and every item this build
records is UNKNOWN. `_context`'s existing one-hour window is what decides what
a provider actually sees, and a second freshness rule that could disagree with
it would be worse than none. UNKNOWN is not folded into STALE: "we do not know
how old this is" and "this is old" are different facts.

## 5. Provenance and the one-call rule

`Lumen.evaluate` now reads:

```python
request = IntelligenceRequest(... payload=self._context(symbol) ...)
analysis = self._begin_analysis(symbol, request)
response = await self.provider.analyze(request)     # ONE call, as before
...
self._record_response(analysis, response)
if not response.ok:  ...unchanged...
opinion = self._to_opinion(symbol, response.data)   # unchanged
self._heartbeat()
self._record_outcome(analysis, opinion)
return opinion
```

Two properties this is built to hold:

**One provider call.** Provenance never costs a second request. Against a
non-deterministic model a second call would record a *different* answer than
the one the platform used, which is worse than no record at all.

**One context.** The request is bound to a local so the evidence bundle can be
built from `request.payload` — the object the provider received. Calling
`_context` a second time would read the clock again and could describe a
different `as_of_ms` than the provider actually saw.

`_logical_now` reuses `last_call_ms`, the clock read `evaluate` already made.
**Phase 10 adds no clock read to LUMEN.** The existing slow-loop clock
behaviour is untouched; its logical-time correctness is validation work, not
this phase's.

## 6. Analysis lifecycle

`IntelligenceRunStatus`: CREATED, CAPTURING, REQUESTING, COMPLETED, PUBLISHED,
UNAVAILABLE, FAILED.

Two distinctions the lifecycle preserves because the platform already draws
them:

* **UNAVAILABLE vs FAILED.** A provider that declined or could not be reached
  is an outage; a response that could not be parsed is a defect somewhere.
  Collapsing them makes an API being down look like a bug.
* **COMPLETED vs PUBLISHED.** A malformed response completes the call and
  publishes nothing. `published_opinion_ref` is the field that answers whether
  anything reached the bus.

`IntelligenceAnalysisRecord` carries the provider's raw readings — sentiment,
attention, shock, direction, confidence, TTL, reason codes — copied field by
field. A reading the provider did not supply stays `None` rather than being
defaulted; a record that filled in a plausible zero would be inventing
evidence.

### Malformed responses

`_to_opinion` returning `None` is unchanged: it logs, counts a failure, and
publishes nothing. `mark_malformed` records that outcome **afterwards**. The
registry never invents a neutral opinion to fill the gap — a fabricated neutral
vote is a lie, and publishing nothing is the honest answer the platform already
gives.

## 7. Provider request and response records

`IntelligenceProviderRequestRecord` is a **summary**: task, provider, model,
token and timeout limits, and a `payload_summary` describing shape (how many
headlines, whether a market summary was present). The system prompt and the
schema are module constants; storing either per call would make the record's
size a function of how often the loop ran.

`IntelligenceProviderResponseRecord` mirrors `IntelligenceResponse`, which
stays the authoritative return type: ok, provider, model, latency, the
`unavailable` flag, the error, and a stringified `data_summary`.

**Neither carries a credential, and neither may learn to.** `describe_provider`
reads a provider's `name` and `model`; it does not read
`ClaudeProvider._api_key`. Claude API key handling stays exactly where it is —
no credential registry, no secret persistence, no database-stored keys.

## 8. Context snapshot

`LumenContextSnapshot` answers "what did LUMEN see?" for one analysis. It
references the stored bundle rather than re-assembling the payload, so it
cannot describe a different input set than the provider was sent.

`IntelligenceMarketContext` types exactly the four values `_context` already
sends: reference price, venues quoting, max cross-venue deviation, short vol.
It is **deliberately narrow**. LUMEN must never see balances, positions,
orders, risk limits, RUNE decisions, wallets or credentials, and widening this
model is how that boundary would erode.

## 9. AgentOpinion linkage

`AgentOpinion` is **not modified**. It has no id field, so `PublishedOpinionRef`
is the tuple that actually identifies one — agent, symbol, creation instant,
expiry, model version, correlation id — plus the published signal and
confidence, copied. Minting a synthetic id would create something that looks
like a key and matches nothing.

`IntelligenceRegistry.link_opinion` attaches the reference and moves the
analysis to PUBLISHED.

## 10. External-intelligence replay boundary

Phase 2 settled this and Phase 10 does not reopen it. **LUMEN opinions are
exogenous inputs.** Replay republishes the recorded `AGENT_OPINION` events and
does **not** call the provider again — it could not do so deterministically,
and a replay that re-asked a non-deterministic model would not be a replay of
anything.

**PROVIDER IS NOT REINVOKED DURING REPLAY.**

`IntelligenceReplayProvenance` records that contract, and for every analysis
this build produces it reads the same way: `replayed_as_external_input=True`,
`provider_reinvoked=False`. It is a model describing the replay engine's
behaviour. **The replay engine is untouched by this phase.**

## 11. LumenSnapshot and LumenReadiness

`LumenSnapshot` is compact metadata: the provider descriptor, call and failure
counts, mean latency, headline count, pending count, and **ids** for recent
analyses. A snapshot embedding every analysis with its evidence would be sized
by how much news the platform had ingested.

`LumenReadiness` reports and gates nothing. Reason codes:
`NO_PROVIDER_CONFIGURED`, `CONSECUTIVE_FAILURES:n`, `NO_EVIDENCE`,
`NO_MARKET_CONTEXT`, `NO_RECENT_ANALYSIS`.

## 12. Optional-agent boundary

**LUMEN REMAINS OPTIONAL.**

* It is **not** in `required_agents`, and this phase does not add it.
* A missing LUMEN opinion does **not** make a consensus incomplete.
* `LumenReadiness.ready == False` **must not stop trading**.

With the shipped `NullProvider` configuration LUMEN reports unready
permanently and the platform trades normally. That is the designed state, not a
fault, and `LumenReadiness.optional` is stated on the record so a reader cannot
mistake an unready LUMEN for a blocked platform.

Provider health and platform health stay separate. `_heartbeat` still decides
OFFLINE, DEGRADED and HEALTHY exactly as before; the readiness model is
observational and does not feed it.

`Platform.start()` is unchanged and blocks on none of this.

## 13. Consensus boundary

LUMEN outputs an `AgentOpinion`. The consensus engine decides how it
contributes, and that arithmetic is untouched.

LUMEN is given no access to consensus thresholds, weights, RUNE, the portfolio
or order state. `IntelligenceRegistry` affects consensus in no way.

Phase 8 already records an `OpinionReference` on every
`ConsensusEvaluationRecord`, so the chain *analysis → published opinion →
consensus evaluation* is expressible through the two existing references. **No
second consensus trace system was built**, and Phase 8's decision logic is
unchanged.

Attribution likewise: a future tool can connect an analysis id to an opinion to
a `TradeAttribution` through references that already exist. No attribution
arithmetic changed, and **no performance feedback reaches LUMEN** — a platform
that tuned its analyst on its own P&L would be optimising against its own
history.

## 14. Future information-source seam

`IntelligenceSource` (in `agents/lumen/source.py`) names what a future
information adapter must provide: a name, a kind, an `is_external` flag, and
`capture(symbol, now_ms)`.

**NO NEW NETWORK INTELLIGENCE SOURCE IS IMPLEMENTED.** No Reuters, Bloomberg,
X, Reddit, Google News, RSS, CoinDesk or exchange-announcement client. No HTTP,
no credentials, no API keys, no rate limiter, no retry.

That absence is the design. A source seam carrying a half-written credential
path is not a seam, it is a liability — and every real feed brings a trust
question ("do we believe this publisher?") that this phase may not answer and
must not appear to have answered.

`LocalHeadlineSource` wraps the very list `Lumen.headlines` is, so the existing
input is expressible in the new vocabulary. **LUMEN does not use it.**
`evaluate` and `_context` read `self.headlines` exactly as they always have;
migrating the agent onto the interface is a later, validated step.

`IntelligenceSourceHealth` reports whether a source answered. No retry, no
backoff, no probe.

### Provenance is not truth

These models answer *where did this input come from, and when?* They do not
answer *is it correct?* A headline recorded here is a headline that arrived, no
more. Misinformation, duplicated stories and conflicting stories all record
cleanly and are all still unsolved.

## 15. Control vocabulary

`IntelligenceControlKind`: PAUSE, RESUME, RUN_NOW, CLEAR_EVIDENCE.

**Vocabulary only. Nothing is executable** — no dispatcher, no route, no
handler. `CLEAR_EVIDENCE` least of all: a dispatchable control that deletes the
platform's record of what it was told would be a way to erase provenance, built
before anyone decided who may do that or what it must preserve.

## 16. Retention

`IntelligenceRegistry.compact` releases nothing by default, and there is no
arbitrary count limit. LUMEN's own 50-item headline cap is separate and
unchanged.

Even when asked to release: a pending analysis is never released; a FAILED or
UNAVAILABLE analysis is never released, because the record of a provider outage
is exactly what a later reader wants and no aging policy exists to say when it
stops mattering; an analysis whose opinion is the latest for its symbol is
never released; and no evidence bundle referenced by a resident analysis is
released.

---

## VALIDATION DEFERRED

**Nothing in this phase has been validated.** No test was written, changed or
run; no linter, type checker, replay or provider call was executed.

### The provider

* provider request correctness
* response schema enforcement
* malformed responses
* JSON extraction
* provider failure behaviour
* provider timeout behaviour
* provider retry behaviour
* Claude nondeterminism
* `NullProvider` behaviour
* `ScriptedProvider` behaviour
* prompt correctness
* information leakage — that the payload contains nothing it should not

### The evidence

* headline freshness
* headline ordering
* headline deduplication
* evidence provenance
* evidence completeness
* market context correctness

### The reading

* sentiment interpretation
* attention interpretation
* shock interpretation
* direction interpretation
* turbulence formula
* signal sign
* confidence calibration
* TTL behaviour
* reason codes

### The loop and the agent

* slow-loop cadence
* slow-loop failures
* provider health
* optional-agent semantics
* consensus contribution
* stale opinion handling

### Replay

* replayed external opinions
* provider must not run during replay
* opinion provenance

### Framework properties

* registry identity
* snapshot consistency
* logical-time behaviour
* resource retention
* performance
* network failure recovery

### Trust — unframed, and deliberately so

* future source trust
* misinformation handling
* duplicate stories
* conflicting stories

## Testing status

**No Phase 10 tests exist.** Treat every behaviour described here as
constructed and unproven.
