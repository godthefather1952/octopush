# Phase 2 validation — deterministic replay

Phase 2 made one claim defensible:

> A verified Octopush replay starts from a verified-complete recording, uses a
> compatible event schema, uses the same material configuration, reconstructs
> the original market-input visibility and tick schedule, drives all
> economically relevant logic at the original logical time, restores its
> process-global state when finished, and can optionally produce its own
> durable recorded output.

Anything that violates that sentence is either fixed, or refused by default
and reported by name when the refusal is deliberately overridden. Nothing
degrades exactness silently.

- **Starting SHA:** `6291d956b8e86d63166e2bab679041adce7f592f`
- **Branch:** `phase2-audit`
- **Scope:** event store, recorder, replay engine, replay CLI. Paper trading
  only; no live execution anywhere in the codebase.

---

## 1. Architecture

### Recorder

Bus middleware. Every published event is deep-copied on acceptance and held in
memory until storage *confirms* it.

- A failed flush retains the batch. `events_lost` means "accepted and then
  permanently abandoned", and nothing else.
- Retrying is safe when the commit outcome was never learned: the stores treat
  a byte-identical re-delivery as a no-op and a different event under a known
  id as an error, so a blind retry converges on exactly one copy.
- Pending memory is bounded by `max_pending_events` and **fails closed**:
  `record()` raises, the bus aborts the publish, storage health degrades, the
  kill switch stops trading. Nothing already accepted is trimmed to make room.
- `stop()` finalises COMPLETE only when nothing is pending, INCOMPLETE (with
  the reason and the count) when history is genuinely abandoned, and leaves
  the session OPEN when even that write fails — "unknown" is honest where
  "complete" would be a lie.

### SessionStatus

Persisted, never inferred from `ended_at`:

| Status | Meaning |
|---|---|
| `OPEN` | Recording, or the process died. Whatever the recorder still held is not here. |
| `COMPLETE` | Every accepted event is durably present. The only status exact replay accepts by default. |
| `INCOMPLETE` | Finalised while knowing history was lost. Reason and count recorded. |
| `LEGACY_UNVERIFIED` | Written before this schema existed. Readable, never silently certified. |

Pre-Batch-2 rows read back as `LEGACY_UNVERIFIED` rather than being backfilled
to `COMPLETE`, which would certify history nobody verified.

### EventStore

One contract, identical across in-memory, SQLite and PostgreSQL:

- A session id is used once. A duplicate `start_session` raises
  `SessionAlreadyExists` and leaves the existing row untouched (the primary
  key backstops the cross-process race).
- Appending to an unknown or terminal session is refused.
- A reused event id carrying different content raises `EventIdCollision`,
  naming the fields that differ; a byte-identical re-delivery is the intended
  no-op.
- Batches are atomic by **explicit transaction**, not driver default.

### ReplaySession

Reconstructs a fresh platform and drives recorded inputs through it. Derived
state is recomputed — that is the point: if the output differs, the code
changed.

### ManualClock, tick markers, input watermarks

- Time comes only from a `ManualClock` driven by recorded timestamps.
- One `ORCHESTRATOR_TICK` marker per original tick recovers the original
  decision cadence — never one tick per market event.
- Each marker carries the watermark of inputs TIDAL had *applied* at that
  tick's snapshot, so replay releases exactly the inputs that tick actually
  saw. Publication order is not delivery order under an async bus.
- **One tick = one logical timestamp**: the canonical tick time is
  `market.created_at`, and every economically relevant call takes it
  explicitly rather than reading a clock mid-tick.

---

## 2. Fidelity model

`ReplayStats.fidelity` is a `ReplayFidelity` with seven independent
dimensions. Each starts verified and is demoted by name. Demotions accumulate
— a run can be unverified in several dimensions at once, and a single string
could only ever report one of them.

| Dimension | Question |
|---|---|
| `recording_integrity` | Does the recording contain every event the recorder accepted? |
| `timeline` | Did ticks happen at the original logical boundaries? |
| `input_visibility` | Did each tick see exactly the inputs its original snapshot had? |
| `starting_state` | Did the replay start from the session's true beginning? |
| `schema` | Does this build understand every envelope in the range? |
| `configuration` | Is the configuration materially the same as the one recorded? |
| `external_intelligence` | Were externally-produced inputs replayed? |

```
is_exact  ==  every dimension verified
```

`ReplayStats.timeline_fidelity` remains as a lossy compatibility shim
returning the most fundamental issue (declaration order, not alphabetical) or
`None`. Anything deciding what a replay may claim reads `fidelity`.

---

## 3. Guarantees, and how each is earned

### Session integrity

Exact replay refuses anything but `COMPLETE`.
`allow_incomplete_session=True` / `--allow-incomplete-session` replays anyway
and reports `UNVERIFIED_RECORDING_INTEGRITY`.

### Tick cadence and input visibility

A session with no markers in range is refused (`--legacy-timeline`); markers
without watermarks are refused separately (`--legacy-input-visibility`). Every
marker's watermark is validated as it is read — monotonic, integer, and not
above the highest sequence the range recorded.

### Logical time

No component reads a clock during a tick. Paper execution takes `now_ms`
explicitly on every entry point, and `PaperExecutor` contains no clock reads
at all (enforced by a source-inspection test).

### Schema

```
version in SUPPORTED_SCHEMA_VERSIONS   -> readable
anything else                          -> refused before replaying anything
```

There are deliberately **no upcasters**. With one envelope version in
existence, a registry would be speculative scaffolding, and inventing
conversions for versions that do not exist is how a replay quietly
reinterprets history. "Older" is not a synonym for "compatible": an older
unsupported version is refused for the same reason a newer one is.

The check is a **preflight** over the replay-relevant range, before any event
is published, any tick executed, or any durable output started. Discovering
event 400 is unreadable after replaying 399 is not a refusal.

*Known limitation, stated plainly:* `schema_version` describes the **envelope**,
not the payload model. `schema_name` identifies the payload model but is not a
version. Phase 2 therefore guarantees only that current-version envelopes
replay and that unsupported envelope versions fail before replay. Independent
payload-model versioning does not exist today and is not claimed.

### Configuration

The digest is over **material** settings — those able to change what the
platform does with identical market data.

| Outcome | Condition |
|---|---|
| Verified | Both digests exist and match |
| Refused | A current digest was supplied and verification failed |
| Unverified (allowed) | No current digest supplied — the caller never asked |

Refusing when no digest was supplied would force an override on every
programmatic caller for a check they did not request; proceeding silently
would let a run call itself exact having compared nothing. So it proceeds and
`configuration` is demoted.

**Excluded from the digest** (`DIGEST_EXCLUDED_PATHS`): `environment`,
`log_level`, `log_format`, `storage.sqlite_path`, `storage.postgres_dsn`,
`redis_url`, `api_host`, `api_port`.

These are addressed to the *outside* of the computation and are read by no
agent, strategy, risk gate or execution simulator. The exclusion is not
asserted but proved: one recording is replayed twice, once with every excluded
path changed, and every economic output is compared. `storage.backend` stays
material — the store *type* is the one part of that section with a conceivable
behavioural difference (a failing recorder degrades storage health, which the
kill switch reads), and an unproven exclusion is worth less than a false
positive.

Breadth was a real hazard here: digesting the whole settings object would make
"the database is at a different path" a material configuration change, so
every replay of a session recorded on another machine would demand the
override — and a routinely-overridden gate protects nothing.

**No configuration snapshot is persisted.** A digest proves equality; it
cannot reconstruct settings. Exact replay therefore requires the caller to
supply the same material configuration, and the digest verifies that they did.
Replay is **not** self-contained with respect to configuration, and does not
claim to be.

**Secrets:** `model_dump` masks every `SecretStr` to identical asterisks, so
the digest hashes the real value instead — two credentials are distinguishable
wherever a secret is material, and neither the digest nor any refusal message
carries one. Refusals quote digests, never settings.

### External intelligence (P2-16)

LUMEN reads the information environment through an intelligence provider
(Claude, by configuration) and publishes an `AGENT_OPINION` carrying consensus
weight 0.5. Nothing in a recording lets replay recompute what a language model
said, and calling the model again would produce a different answer.

**Policy:** recorded external opinions are replayed as exogenous inputs, like
a recorded book snapshot. Replay never calls an intelligence provider.

The discriminator is the event **source**, not the type: TIDAL, NORO and ZEPHR
publish the same `AGENT_OPINION` type, but theirs are derived deterministically
from market data and must be recomputed — replaying those would mean a code
change to an agent no longer shows up as a different answer.

| Provider | Contract |
|---|---|
| `null` (default) | No opinion is ever published. Nothing to replay; deterministic. |
| `scripted` | Deterministic. Its recorded opinions replay as exogenous inputs. |
| `claude` (or any nondeterministic provider) | Its recorded opinions replay as exogenous inputs. The model is never re-invoked. |

Dropping them (`replay_external_intelligence=False`) is allowed and reported
as `UNVERIFIED_EXTERNAL_INTELLIGENCE`, never as exact.

### REALTIME and STEP semantics

`ReplayMode` is `FAST | REALTIME`. Both advance the logical clock identically
and produce identical economic output; they differ only in whether the host is
delayed between logical instants.

```
host delay = (target_ms - now_ms) / 1000 / speed
```

applied at `_advance_replay_clock` — the moments logical time actually moves,
not once per item returned. Those differ: a tick marker can release a batch of
deferred inputs, so several items surface at one instant. Pacing uses an
injected `host_sleep` (default `asyncio.sleep`), never `ManualClock.sleep`,
which is the replay's own clock and cannot pace anything. A non-positive speed
is refused for REALTIME.

There is deliberately no `STEP` mode: stepping is what `step()` does, and a
mode of the same name implied `run()` behaved differently under it, which it
never did.

**One limit, one unit.** `--max-items N` stops after N *logical replay items*,
where an item is one applied input or one `ORCHESTRATOR_TICK` boundary. Ticks
count — a limit that skipped them would mean different amounts of replay
depending on how much market data sat between ticks. `--step` and
`--max-events` remain as deprecated aliases for the same flag.

### Process-global state

`open()` validates everything **before** installing the deterministic id
generator and the bound logging clock, so a refused session cannot leave the
process altered. A belt-and-braces `try` covers the rest, and
`with` / `async with` covers the whole replay. The CLI releases the session,
the platform and both stores independently in a `finally`, so one failing to
close cannot strand the others.

Nesting is defined (strict LIFO) though not the supported pattern; serial use
is, and does not accumulate.

### Durable replay output

Without `--record-output`, nothing durable is written.

With it, output goes to the **configured durable backend** under a new session
id labelled `replay-of-<source>`, carrying the current config digest, and
finalised `COMPLETE` only after a clean stop. A `memory` backend is **refused**
with a clear message rather than silently recording into a store that vanishes
at exit.

The output recorder does not start until the replay has been *accepted*, so a
refused replay leaves no output session at all — never one finalised COMPLETE
for a run that never happened.

A bounded replay (`--max-items`) records honestly: the label says
`(partial: first N items)`, and the summary reports `is_exact: false`. It is a
complete recording *of that partial replay*, never a reproduction of the source
session.

Source and output routinely share one database. The source is read-only and
proved byte-identical afterwards.

---

## 4. Event-store integrity boundary

`events.session_id` has **no foreign key** to `sessions.session_id`.

Enforcement is at the application layer, inside the same transaction that does
the writing: all three backends refuse an append to an unknown or terminal
session. Orphan-row audit on the real PostgreSQL test database: **0 orphans**.

A schema-level FK was considered and deliberately not added:
`ALTER TABLE ADD CONSTRAINT` fails on any existing database holding orphan
rows, turning a silent gap into a hard startup failure, and SQLite cannot
`ALTER ADD CONSTRAINT` at all — so the backends would diverge again, which is
the defect Batch 2 closed.

**The boundary, stated explicitly:** the `EventStore` API guarantees session
membership. Direct SQL writes are outside the supported integrity boundary.

---

## 5. Replay input set

Every `EventType` is classified, and the suite fails if a new one appears
without a decision — an omitted exogenous input does not crash, it produces a
clean replay of a different session.

| Class | Types |
|---|---|
| Exogenous market input (replayed) | `BOOK_SNAPSHOT`, `BOOK_DELTA`, `TRADE_PRINT`, `VENUE_CONNECTED`, `VENUE_DISCONNECTED`, `VENUE_SEQUENCE_GAP` |
| Exogenous, recorded, not consumed | `MARKET_UPDATE` (raw payload; the parsed events it became are replayed instead) |
| Replay control | `ORCHESTRATOR_TICK` (never published to the bus) |
| Derived (recomputed) | everything else, including `AGENT_OPINION` |
| Exogenous by source | `AGENT_OPINION` from `LUMEN` |

`VENUE_SEQUENCE_GAP` is declared and replayable but has **no publisher today**:
a feed gap is detected inside `LocalOrderBook` and handled there. It is kept in
the input set deliberately — if an adapter ever publishes one it is exogenous
by construction, and having replay already treat it as an input is safer than
discovering the omission from a divergence.

---

## 6. Backend parity and real infrastructure

The session-integrity, schema-compatibility and source-immutability contracts
all run against in-memory, SQLite **and real PostgreSQL**, because the defect
they replace was three backends answering one call three different ways.

- **Real PostgreSQL 16** — used throughout; contract, integrity, schema,
  immutability and record-output suites all green.
- **Real Redis** (`redis://127.0.0.1:6399/0`) — event-bus contract and
  ordering-parity suites green.
- **SQLite** — exercised independently, including the CLI end-to-end suite,
  which runs `python -m replay` in real subprocesses against a real database.

---

## 7. Performance sanity

An 800-tick session, replayed FAST:

| ticks | stored events | read | published | peak pending | store reads | seconds | events/s |
|---|---|---|---|---|---|---|---|
| 100 | 1264 | 628 | 528 | 0 | 3 | 0.35 | 1802 |
| 200 | 2544 | 1273 | 1073 | 0 | 3 | 0.67 | 1907 |
| 400 | 5041 | 2539 | 2139 | 0 | 3 | 1.41 | 1806 |
| 800 | 10278 | 5072 | 4272 | 0 | 3 | 3.08 | 1646 |

- **Linear**: doubling the session doubles the time at every step.
- **Three store reads in total, independent of session size** — the tick-marker
  probe, one preflight scan, and the main iterator. The schema gate was folded
  into the existing scan rather than adding another pass, so P2-8 cost zero
  extra whole-session reads.
- **Pending queue stays bounded**; it is zero in this workload because each
  marker's watermark covers the inputs before it. The deferred path is
  exercised by the concurrent-scheduling suites.
- Peak RSS ~230 MiB for the whole ladder in one process.

REALTIME is intentionally slow and excluded from throughput expectations.

---

## 8. Replay equivalence

Original-vs-replay economic equivalence, comparing tick count, opportunity
states, risk verdicts, orders, fills (quantity and price), fees, positions,
cash, equity, realised/unrealised/gross/net P&L, drawdown, kill-switch state
and reconciliation:

- **800-tick** deterministic session — equivalent.
- **40 / 50 / 55 / 70** historical regression lengths — equivalent.
- **15–95 sweep**: 81/81 tick counts equivalent, plus 800 → **82/82 clean**.
- **Async bus scheduling** (independent concurrent publisher and tick loops) —
  equivalent.
- **Multi-venue concurrent publication** — equivalent.
- **With an exogenous LUMEN opinion arriving between ticks** — equivalent.

The only normalisation is literal identifier *values*: a live run mints random
ids and a replay installs a deterministic generator by design. Structural
order is compared positionally, not reduced to totals.

---

## 9. Known limitations

1. **Payload-model versioning does not exist.** `schema_version` is the
   envelope; `schema_name` is a name, not a version. See §3.
2. **No upcasters.** Any envelope version other than the current one is
   refused rather than converted.
3. **Configuration is not self-contained.** The digest verifies equality; it
   cannot reconstruct settings. See §3.
4. **No crash-resume protocol.** A session id is used once. A crashed session
   stays `OPEN` and is refused by exact replay; a new run uses a new id.
5. **No schema-level foreign key** between events and sessions. See §4.
6. **`VENUE_SEQUENCE_GAP` has no publisher.** See §5.
7. **Live public-feed soak remains externally blocked**, unchanged from
   Phase 1. No `phase1-validated` tag exists, so no `phase2-validated` tag is
   created here.

---

## 10. Phase 2 finding closure

| ID | Severity | Status | Fix |
|---|---|---|---|
| P2-1 | HIGH | CLOSED | Durable `ORCHESTRATOR_TICK` markers; replay ticks at the original boundaries |
| P2-2 | HIGH | CLOSED | One `SessionAlreadyExists` refusal on every backend; existing row untouched |
| P2-3 | MED | CLOSED | Real host pacing via injected sleeper at `_advance_replay_clock`; `--realtime` / `--speed` |
| P2-4 | MED | CLOSED | Validation before global install; `close()` on failed open; CLI `finally` |
| P2-5 | MED | CLOSED | `--record-output` writes to the configured durable backend; memory refused |
| P2-6 | HIGH | CLOSED | Fingerprint comparison over all persisted fields; `EventIdCollision` |
| P2-7 | HIGH | CLOSED | Pending retained until durable confirmation; bounded and fail-closed |
| P2-8 | MED | CLOSED | `SUPPORTED_SCHEMA_VERSIONS` gate, preflighted before any side effect |
| P2-9 | MED | CLOSED | Material config digest compared; exact vs counterfactual |
| P2-10 | LOW | CLOSED | `ReplayMode` is FAST/REALTIME; one `--max-items` over one defined unit |
| P2-11 | HIGH | CLOSED | Original-vs-replay economic equivalence suites |
| P2-12 | HIGH | CLOSED | `record()` stores a deep snapshot |
| P2-13 | HIGH | CLOSED | Ordered commit on the event bus; Redis parity |
| P2-14 | HIGH | CLOSED | Paper execution takes explicit `now_ms`; no clock reads in `PaperExecutor` |
| P2-15 | HIGH | CLOSED | `tick_time = market.created_at`; one tick, one logical timestamp |
| P2-16 | HIGH | CLOSED | External intelligence replayed as exogenous input (found in this audit) |
