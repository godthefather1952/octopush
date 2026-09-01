# Multi-Agent Trading Floor

A modular, event-driven, multi-agent trading platform. Specialised components
analyse the market independently, an orchestrator combines their outputs,
deterministic risk controls decide whether a trade is permitted, and a
dedicated execution engine manages orders.

It is built to resemble an automated quantitative trading desk rather than a
single model wired to an exchange.

> ## PAPER TRADING ONLY
>
> **This codebase contains no exchange order-submission implementation.**
> Not a disabled one — none. Venue adapters read public market data and
> nothing else; `PaperExecutor` is the only implementation of the execution
> interface; `TradingMode` has exactly one member. See
> [Paper mode security boundary](#paper-mode-security-boundary).

---

## What it does today

```
MARKET REALITY → DATA NORMALIZATION → SPECIALIZED ANALYSIS → OPPORTUNITY DETECTION
    → MULTI-AGENT CONSENSUS → HARD RISK GATES → EXECUTION PLAN → PAPER EXECUTION
    → HEDGE SIMULATION → RECONCILIATION → PORTFOLIO STATE → PERFORMANCE ATTRIBUTION
```

Two venues, BTC and ETH, hunting short-duration cross-venue dislocations. The
question it asks is deliberately narrow:

> Is the same asset temporarily mispriced across venues **after** fees,
> spreads, slippage, liquidity and execution costs?

Not "will BTC go up".

## Quick start

```bash
pip install -e ".[dev]"

# A complete offline paper session against a deterministic synthetic market.
python -m apps.orchestrator

# Dashboard at http://localhost:8080 — clearly marked PAPER MODE.
```

No exchange account, no API key, no Redis, no PostgreSQL. Everything above
runs in-process against a seeded market generator and a SQLite event store.

```bash
# Public, read-only exchange feeds instead of the synthetic market.
TF_FEED=live python -m apps.orchestrator

# List and replay recorded sessions.
python -m replay --list
python -m replay --session session-abc123

# The full stack, with the Redis bus and the PostgreSQL event store.
docker compose up
```

## The components

| Component | Role | Deterministic? |
|---|---|---|
| **TIDAL** | Market data. Order books, microstructure metrics, freshness, latency, sequence gaps, the consolidated cross-venue view. | Yes |
| **NORO** | Fair value. Liquidity-weighted price across venues, and per-venue deviation from it. | Yes |
| **ZEPHR** | Executability. Prices the trade at several sizes against the real book, subtracts every cost, reports the largest economical size. | Yes |
| **LUMEN** | Information environment. Reads headlines and market context, returns structured sentiment, attention and shock readings. | No — Claude |
| **RUNE** | Risk. `RUNE-CORE` is deterministic and decides; `RUNE-AI` comments and cannot. | Core: yes |
| **VESKA** | Execution. Turns approved intents into plans: order type, size, routing, maker/taker, cancellation. | Yes |
| **OKAPI** | Hedging. Watches intended vs actual exposure and works the difference down. | Yes |
| **MARIN** | Reconciliation. Compares what the platform believes against what execution reports. | Yes |
| **Orchestrator** | Coordination, consensus, lifecycle, health, kill switch. | Yes |

### AI enhances the system; it is not the system

Anything fast or mathematically determined stays ordinary code: order books,
fees, slippage, risk limits, position and P&L arithmetic, order state,
reconciliation, hedging.

Claude does what language models are actually good at: news, sentiment,
regime classification, narrative change, unusual conditions, higher-level
contradiction analysis.

```
FAST LOOP                    INTELLIGENCE LOOP
milliseconds                 seconds / minutes / events
market data, pricing,        Claude: news, sentiment,
liquidity, execution,        regime, context
risk, hedging
```

The fast loop never waits on a model. **The platform trades, stops, closes,
reconciles and preserves data with no intelligence provider at all** — that is
the default configuration, and it is the tested one
(`tests/failure/test_failures.py::TestIntelligenceFailures`).

Claude is reached through `IntelligenceProvider`, so no part of the trading
system knows which model produced a piece of intelligence. Swapping providers
is a wiring change.

## Design rules that shaped the code

**A stale signal never becomes a neutral signal.** It becomes `DEGRADED`,
`STALE` or `UNAVAILABLE`, and consumers must branch on that. A missing agent
returns `None`, not a zero. Folding an absent agent in as a zero vote is a
silent, confident lie, and the consensus engine refuses to tell it.

**Hard gates are not outvoted.** Consensus decides whether a trade is worth
putting in front of RUNE. RUNE decides whether it happens. A mandatory gate
that *could not be evaluated* fails, because an unevaluated gate has not been
satisfied.

**`UNKNOWN` is a real order state.** A timed-out operation is not assumed to
have failed. It sits in `UNKNOWN` until something authoritative resolves it,
and MARIN reports it on every run until it is settled.

**Paper execution is pessimistic in the right places.** Depth, latency drift,
queue position, vanishing liquidity, partial fills, cancel races. A paper
engine that fills everything at the touch produces a strategy that only works
on paper.

**Replay is a product feature.** Recorded market inputs are re-published
through a fresh pipeline; everything derived is *recomputed*. Running the same
session against two versions of the code is therefore a direct comparison of
the code.

## Paper mode security boundary

Paper mode is not `LIVE=false`. It is structural:

- **No order-submission code exists.** `VenueAdapter` declares no method
  capable of placing, amending or cancelling an exchange order. A test
  enumerates every adapter and asserts the absence.
- **No exchange credentials, anywhere.** No request signing, no private-key
  custody, no wallet code, no withdrawal or transfer logic, and no
  authenticated exchange endpoint. The one API key the codebase touches is the
  optional `ANTHROPIC_API_KEY` for the intelligence layer, which reaches
  Anthropic and nothing else.
- **`PaperExecutor` is the only `Executor`.** A test asserts
  `Executor.__subclasses__() == [PaperExecutor]`. `Veska` refuses to construct
  with `is_paper == False`.
- **The registry refuses a trading-capable adapter.** `build_adapter` raises if
  an adapter declares `order_submission` or `authenticated`.
- **`TradingMode` has one member.** There is no other mode to configure.
- **The API cannot trade.** Its only non-`GET` endpoint is the kill switch,
  which can only make the system safer. A test asserts this from the OpenAPI
  schema.

Live trading would require a new interface, a new implementation and
deliberate integration work. That friction is the point.

## Risk

`RUNE-CORE` runs every gate on every intent, and records all of them —
including the ones that passed:

```
KILL_SWITCH_CLEAR · SYSTEM_HEALTHY · EXECUTION_HEALTHY · CONSENSUS_THRESHOLD
MARKET_DATA_FRESH · INTENT_NOT_EXPIRED · MIN_EXPECTED_EDGE · LIQUIDITY_SUFFICIENT
HEDGE_AVAILABLE · MAX_ORDER_NOTIONAL · MAX_POSITION_NOTIONAL · MAX_GROSS_EXPOSURE
MAX_NET_EXPOSURE · MAX_LEVERAGE · MAX_VENUE_EXPOSURE · MAX_STRATEGY_EXPOSURE
MAX_DAILY_LOSS · MAX_DRAWDOWN · MAX_UNHEDGED_EXPOSURE · MAX_OPEN_ORDERS · MAX_ERROR_RATE
```

RUNE sizes first and gates second, so a trade that is merely *too large* is cut
down to the headroom that remains rather than thrown away — and the gates still
have the final word on the size actually proposed.

The kill switch has four independent actions (`HALT_NEW_TRADES`, `CANCEL_ALL`,
`FLATTEN`, `DISABLE_EXECUTION`) because they answer different questions.
Engaging is automatic; **clearing is manual**, because a condition that stopped
trading should be understood before trading resumes.

Because clearing is manual, a *measurement* trigger — component health,
observed latency — must fire on several consecutive evaluations before it
engages. A breached limit or a corrupted book engages on the first
observation, because those are true the moment they are seen.

## Transaction costs

A trade is never judged on its gross price difference:

```
gross edge         +40.0 bps
  fees              -11.0     (taker on both venues)
  spread             -3.0     (crossing the touch)
  slippage/impact    -4.2     (walking the book)
  latency            -1.6     (adverse drift in flight)
  hedge              -1.0
  ──────────────────────
expected net edge   +19.2 bps
```

The orchestrator operates on expected net edge. In the default synthetic
market, most detected dislocations are *rejected* by ZEPHR because a 13 bps
gap does not survive 16 bps of costs. That rejection is the system working.

## Attribution

Every simulated trade records why it happened — agent signals, confidences,
weights, consensus, expected edge, expected costs, the risk decision, and what
it actually produced. Agent scorecards accumulate predictive contribution and
hit rate.

**Weights are not adapted automatically.** Early development collects evidence;
performance-aware weighting is a later change with its own safeguards against
overfitting. A test asserts the weights do not move.

## Layout

```
apps/          orchestrator (the head), API, dashboard
agents/        tidal, noro, zephr, lumen, okapi, marin, rune
execution/     veska, paper, oms, router, costs
venues/        base (public data only), venue_a, venue_b, simulated
core/          models, events, bus, clock, state, health, config
strategies/    consensus, cross_venue
risk/          limits, kill_switch
replay/        replay clock and sessions
simulation/    deterministic synthetic market
storage/       event store (memory / sqlite / postgres), recorder, migrations
monitoring/    metrics, attribution, scorecards
tests/         unit, integration, replay, simulation, failure
```

## Tests

```bash
pytest                     # everything
pytest tests/unit          # fast: schemas, books, pricing, risk, execution
pytest tests/failure       # failure injection
pytest tests/replay        # recording and determinism
```

Every test is offline and deterministic: manual clock, in-process bus,
in-memory store, seeded market. The end-to-end suites run several hundred
simulated ticks each, so the full run takes a couple of minutes.

What the suite is really there to prove, before profitability is ever the
objective:

- market data is accurate and books stay synchronised
- disconnected feeds recover; stale data is detected and classified
- events are recorded and replay reproduces the session
- signals expire correctly, and expiry is not neutrality
- risk gates work and cannot be outvoted
- paper execution behaves realistically
- positions reconcile and P&L is correct
- kill switches work
- agents can fail without corrupting state
- **Claude can fail without compromising system safety**

## Configuration

Everything is environment-driven with `TF_` prefixed variables; see
[`.env.example`](.env.example). The defaults run a complete offline session.

## Roadmap

Phases 0–11 of the build plan are implemented: foundation, TIDAL, event
storage and replay, NORO, ZEPHR, RUNE-CORE, VESKA paper execution, MARIN,
orchestration, OKAPI, LUMEN, and full paper trading.

Still ahead:

- **Shadow validation** — compare hypothetical execution against observable
  market outcomes over a meaningful sample.
- **Venue expansion** — 2 → 3 → 5 → 8. The adapter registry and canonical
  symbol layer are built for it; adding a venue is a config block and an entry
  in `venues/registry.py`.
- **Strategy expansion** — spot/perp basis, statistical arbitrage, market
  making. Only after the infrastructure has proven itself.
- **Performance-aware agent weighting**, once there is enough evidence.

Live capital deployment is not on this roadmap. It requires a separate,
explicit project decision and a deliberate new implementation.
