# Fincept Terminal — Value-Increase Execution Plan

> **Status.** This is an execution plan, not a research report. Every
> recommendation carries a stable **task ID**, an **evidence level**
> (A/B/C/D), a **blast radius**, **acceptance criteria**, **dependencies**,
> and an **expected payoff** so a builder, reviewer, or automation agent
> can pick it up and ship it without re-deriving intent.
>
> **Cutoff.** 2026-06-26. Active frontier is the Quant Foundry vertical
> (per `team/MEMORY.md`).
>
> **Method.** Direct reads of `services/`, `libs/`, `apps/dashboard/`,
> plus cross-references to `featuresmenu.md` and `REVIEW_2026-06-23`.

---

## 1. Central Thesis

Fincept does not need more isolated features first. It needs the
existing parts connected into a **closed, inspectable, replayable
operating loop**:

```text
market data → features → predictions → consensus → risk decision →
paper execution → portfolio impact → model settlement → promotion /
retirement decision → operator briefing.
```

Every recommendation in this plan should make that loop **faster**,
**safer**, **more explainable**, or **more automated**. Anything
that does not earn one of those four words is a lower priority.

The architecture is **already well-built** for a paper-trading
research platform — Decimal money, frozen Pydantic schemas, paper-only
firewall, HMAC-signed RunPod callbacks, three-component operator
widgets bound to a strict design system. The biggest opportunity is
*causally connecting the parts that already exist*, not adding
infrastructure.

---

## 2. Runtime Non-Negotiables (the "Keep It Running" gate)

No task is complete unless **all** of the following pass:

1. `docker compose up` and local service startup.
2. Existing API routes return expected shapes.
3. Redis stream compatibility (consumer groups, XADD schemas).
4. Event schema backwards compatibility (frozen Pydantic +
   `extra='forbid'` discipline).
5. Paper-trading firewall intact (`allow_paper_bridge=False`
   default, `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` env-gated).
6. All dashboard pages load (no blank or 500 routes).
7. Core replay tests pass (`scripts/paper_spine_replay.py`,
   `scripts/openbb_live_proof.py`, `scripts/route_smoke.py`).
8. No silent data corruption (state-version checks,
   paper-tracing IDs, `parent_event_ids` lineage).

A task that improves a single service but breaks the loop is **a
regression**, not a win.

---

## 3. Current Architecture Map

```text
ingestor → features → agents → orchestrator → risk → OMS → portfolio → API/dashboard
   │          │          │          │           │       │         │            │
   ▼          ▼          ▼          ▼           ▼       ▼         ▼            ▼
 Binance    PIT 1m    8 agents   consensus   kill    paper     positions    Next.js
 Coinbase   online    base.py    allocator   switch  filler    store        14 App Router
 Kraken     store     registry   decisions   caps    latency   hydrate      17 pages

                  ┌──────────────────────────────────────┐
                  │       Quant Foundry (active)         │
                  │                                      │
                  │ RunPod → trainer → inference →       │
                  │ shadow ledger → tournament →         │
                  │ promotion gate → paper bridge →      │
                  │ orchestrator                         │
                  │                                      │
                  │ Cross-cutting: conformal gate,      │
                  │ MoE router, drift sentinel, causal   │
                  │ graph, retirement, PBO, budget      │
                  │ guard, outbox/inbox, callbacks.     │
                  └──────────────────────────────────────┘
```

**Foundations (`libs/`):**

- `fincept-core` — schemas, config, events, IDs, clock, heartbeat,
  leadership, tracing, portfolio math, strategy config, prediction
  log, errors, http, logging, storage.
- `fincept-bus` — Redis Streams producer/consumer + canonical stream
  names. **One XADD per call today** (see `producer.py:18-25`).
- `fincept-db` — SQLAlchemy/Alembic + bars/ticks/features/universe/
  audit/provider data/receipts.
- `fincept-tools` — typed tool registry (LLM/data/research/paper-exec)
  + OpenBB/Exa research surfaces.

**Supporting services:**

- `services/jobs` — scheduled EOD/news/model-candidate jobs
  (`daily_eod_load.py`, `news_alpha_candidate_train.py`).
- `services/strategy_host` — filesystem-backed strategy instance
  supervisor (`supervisor.py`, `runner.py`, `runtime.py`,
  `model_resolver.py`).

---

## 4. Evidence Standards

Every recommendation carries an evidence level so reviewers can
weight their trust accordingly.

| Level | Meaning | Example |
| --- | --- | --- |
| **A — Verified** | Direct source read with exact file:line | `producer.py:18-25` does one XADD per call |
| **B — Strong inference** | Source structure confirms gap; not fully traced | Walk-forward re-reads bars per window |
| **C — Needs inspection** | Good idea, not yet verified in code | `walk_forward.py` bar-cache behavior |
| **D — Product hypothesis** | Valuable idea, no code anchor | "operator briefing page" UX surface |

**Blast radius** tells builders how dangerous the change is:

| Radius | Meaning | Schema/API/dashboard? |
| --- | --- | --- |
| **Low** | Local module change | No |
| **Medium** | Service behavior change | No |
| **High** | Event schema / API contract | Yes |
| **Critical** | Trading / risk / accounting | Yes |

---

## 5. Top-10 Highest-Leverage Tasks

These are the *default* path. If a builder is unsure what to pick
up, work down this list.

| # | ID | Task | Why it first |
| --- | --- | --- | --- |
| 1 | OBS-001 | Full paper-spine smoke test in CI | Nothing else is provable without it |
| 2 | OBS-002 | Prometheus `/metrics` + structured logs | You cannot optimize a blind pipeline |
| 3 | ING-001 | Ingestor micro-batching | 5–10× throughput unlock |
| 4 | RISK-001 | Reduce-and-allow risk result | One-class-bug fix |
| 5 | CORE-001 | Latency trace envelope | Tail-latency language for the whole platform |
| 6 | QF-001 | Sentinel → retrain → dossier → promotion automation | The single highest-value close-the-loop |
| 7 | DASH-001 | Operator briefing page | The product surface a quant actually opens |
| 8 | DASH-002 | Model "why abstained" explanation | Trust + auditability |
| 9 | PORT-001 | P&L attribution (alpha vs beta vs fees) | The first thing a P&L-aware operator asks |
| 10 | BACKTEST-001 | Walk-forward CPCV + PBO integration | Quant research rigor gate |

---

## 6. Dependency Graph

```text
OBS-001 (smoke test)
  └─► every other task uses it as gate

OBS-002 (metrics)
  ├─► ING-001 (micro-batch) — needs metrics to prove the win
  ├─► ING-003 (latency heatmap) — needs metrics endpoint
  └─► DASH-004 (operator performance panel)

CORE-001 (latency trace)
  ├─► ING-001 — the batcher must stamp latencies
  ├─► CORE-002 (schema versioning) — trace field is additive
  └─► QF-001 — needs traces for "what did the model see"

CORE-002 (schema versioning)
  ├─► OMS-002 (fill attribution) — adds `evidence_id`
  ├─► PORT-001 (P&L attribution) — adds `lot_id`
  └─► ORCH-002 (decision rationale) — adds `rationale`

QF-001 (close-the-loop automation)
  ├─► DASH-003 (promotion notifications) — surfaces the state
  ├─► CORE-003 (model explainability) — explains the abstention
  └─► PORT-002 (paper bridge feedback) — completes the loop

RISK-001 (reduce-and-allow) — no deps, do early
ORCH-001 (dispersion confidence) — no deps, do early
ING-002 (book resync) — no deps, do early
```

**Rule:** do not start a downstream task until the upstream
dependency passes the smoke test (§2).

---

## 7. Task Registry

### ING — Ingestor

#### ING-001 — Micro-Batch Redis Writes

- **Evidence Level:** A — Verified
- **Blast Radius:** Medium
- **Target:** `services/ingestor/src/ingestor/writer.py`
- **Current Behavior:** `Writer.handle(event)` does one Redis
  `XADD` per tick. On BTC-USDT peak (~200 trades/s + 100s of book
  deltas/s) the per-event round trip dominates.
- **Change:** Add `Writer.batch_handle(events, *, max_batch=64,
  max_wait_ms=2)` using `redis.asyncio` pipeline transactions.
- **Acceptance Criteria:**
  - Event order is preserved inside each stream.
  - Flushes on max batch size **or** max wait time (whichever first).
  - Cancellation flushes the in-flight batch (no lost events).
  - Existing single-event tests still pass.
  - New test exercises burst load (1000 events in <50 ms).
- **Expected Payoff:** 5–10× per-stream throughput, p99 ingest
  latency bounded at the batch window rather than network RTT.

#### ING-002 — Emit Book Resync Requests on Sequence Gaps

- **Evidence Level:** A — Verified
- **Blast Radius:** Medium
- **Target:** `services/ingestor/src/ingestor/quality.py:104-130`
- **Current Behavior:** `LatencyTracker.observe()` *counts* gaps
  in `self.gaps[key]` but does not *emit* anything. The
  `main.py:8-11` docstring explicitly defers gap recovery to
  "TASK-014" (not yet implemented).
- **Change:** When a gap is detected, publish a
  `book_resync_request` event on `events.alerts` carrying
  `(venue, symbol, from_seq, to_seq)`. The adapter subscribes
  to this stream and triggers a fresh REST snapshot.
- **Acceptance Criteria:**
  - Gap is detected within 1 message of the discontinuity.
  - Resync event is idempotent (deduped on `(venue, symbol,
    to_seq)`).
  - Adapter subscription path tested with a fake bus.
  - No resync on out-of-order delivery (only on true gaps).
- **Expected Payoff:** Zero book-state corruption incidents
  (each currently costs 2–4 hours of operator investigation per
  `REVIEW_2026-06-23`).

#### ING-003 — Persist Latency Snapshots to a Heatmap

- **Evidence Level:** A — Verified
- **Blast Radius:** Low
- **Target:** `services/ingestor/src/ingestor/quality.py:138-151`
- **Current Behavior:** `LatencyTracker.snapshot()` is per-process
  in-memory only; restart loses it. Computed on a 1024-sample
  ring (verified at `quality.py:89, 96-97`).
- **Change:** Every 10 s, write a Redis sorted set
  `ZADD md.latency:<venue>:<symbol> <ts_ns> <p99_ns>`.
- **Acceptance Criteria:**
  - `/data/coverage` (which the route-smoke receipt shows can
    exceed 5 s) renders a latency heatmap by venue/symbol.
  - Old entries auto-expire (TTL = 24 h).
  - Works under burst load without backpressuring the ingestor.
- **Expected Payoff:** Latency tail is now a *first-class
  observable*, not a per-process log line.

#### ING-004 — Stablecoin Peg Monitor

- **Evidence Level:** A — Verified (current behavior is
  documented at `quality.py:31-32`)
- **Blast Radius:** Low
- **Target:** `services/ingestor/src/ingestor/quality.py:281-308`
- **Current Behavior:** `_check_cross_venue` does not compare
  `BTC-USDT` to `BTC-USD` — the comment explicitly says
  "Tether-stable groupings are deferred to a later config-driven
  enhancement."
- **Change:** Add `STABLE_GROUPS = {"USDT": "USD", "USDC":
  "USD", "BUSD": "USD"}` to `fincept_core.config.Settings`.
  `_check_cross_venue` resolves via that map before deciding
  not to compare.
- **Acceptance Criteria:**
  - USDT depeg > 50 bps fires a `cross_spread` alert
    within one cycle.
  - Stablecoin pairs are opt-in via config.
  - Existing venue-only spreads unchanged.
- **Expected Payoff:** Catches USDT-depeg-style events
  automatically instead of via one-off researcher wiring.

#### ING-005 — Bus Idempotency / Replay-Safety

- **Evidence Level:** A — Verified
- **Blast Radius:** Medium
- **Target:** `libs/fincept-bus/src/fincept_bus/producer.py:18-25`
- **Current Behavior:** `Producer.publish` does one `XADD` per
  call. A re-pointed consumer that re-reads `md.*` from `0-0`
  re-replays every event.
- **Change:** Add `since_id` parameter and use Redis Streams'
  native `id` parameter on `XADD` to make replays deterministic
  from a known checkpoint.
- **Acceptance Criteria:**
  - `paper_spine_replay` produces a receipt reproducible from
    a fixed timestamp.
  - Stream compaction does not break live consumers.
- **Expected Payoff:** Replay tests are reliable; bug
  reproduction is one fixture away.

---

### FEAT — Features Service

#### FEAT-001 — Feature Lineage IDs

- **Evidence Level:** A — Verified
- **Blast Radius:** High (schema change)
- **Target:** `libs/fincept-core/src/fincept_core/schemas.py`
  (FeatureRow), `services/features/src/features/online.py`
- **Current Behavior:** PIT discipline is in `pit.py`; the
  operator cannot ask "which raw events produced this feature
  row?" Called out in `featuresmenu.md` §Feature lineage graph.
- **Change:** Add `parent_event_ids: list[str]` to `FeatureRow`;
  populate in `OnlineRunner.handle_event`.
- **Acceptance Criteria:**
  - Any `FeatureRow` queryable by `parent_event_id` returns
    the row.
  - Lineage graph renders in `/quant-foundry/models/<id>`.
  - Backwards-compatible (extra field, not removed field).
- **Expected Payoff:** Time-to-debug "this number is wrong"
  drops from ~30 min to ~2 min.

#### FEAT-002 — Hierarchical Bar Builder (1m/5m/15m/1h/1d)

- **Evidence Level:** A — Verified
  (`features/main.py:50` consumes only `STREAM_MD_BARS_1M`)
- **Blast Radius:** Medium
- **Target:** new `services/features/src/features/hierarchy.py`
- **Change:** Publish each tier to its own stream
  (`md.bars.5m`, `md.bars.1h`, …). Downstream services consume
  the right tier directly.
- **Acceptance Criteria:**
  - Bars at coarser tiers are bit-identical to a downstream
    re-aggregation.
  - Backtester 5m/15m/1h replays are 4–5× faster.
  - No regression in 1m consumers.
- **Expected Payoff:** Backtester replay speedup; one-time
  compute amortization.

#### FEAT-003 — Calendar Feature Registry (Earnings / FOMC / OPEX)

- **Evidence Level:** C — Needs inspection (no current
  evidence of calendar features in `transforms/`)
- **Blast Radius:** Medium
- **Target:** new `services/features/src/features/calendar.py`
- **Change:** FMP / NASDAQ earnings calendar pull → `days_to_event`
  as a first-class feature. Add FOMC + OPEX schedules.
- **Acceptance Criteria:**
  - Feature store has a calendar source registered.
  - `days_to_event` shows up in `feature_importance` panel.
  - Operator can filter by "next 5 days" earnings.
- **Expected Payoff:** Confidence widget lights up on event days.

#### FEAT-004 — Cross-Asset / Regime-Coupled Features

- **Evidence Level:** A — Verified
  (`transforms/cross.py` is intra-symbol only)
- **Blast Radius:** Low
- **Target:** `services/features/src/features/transforms/cross.py`
- **Change:** Add VIX term-structure slope, SPY–GLD rolling
  correlation, BTC dominance, cross-listed equity basis.
- **Acceptance Criteria:**
  - Features are point-in-time correct.
  - Backtest shows calibration improvement on regime-shift days.
  - Features registered in the feature store.
- **Expected Payoff:** The entire agent stack becomes
  regime-aware (the highest-EV macro features in the literature).

#### FEAT-005 — Online Store TTL + Symbol Caps

- **Evidence Level:** C — Needs inspection (`store.py` not
  fully read)
- **Blast Radius:** Low
- **Target:** `services/features/src/features/store.py`
- **Change:** Add TTL keyed off `ts_event` (30 d online,
  2 y offline). Cap per-symbol memory.
- **Acceptance Criteria:**
  - Online store size stays bounded under continuous use.
  - TTL does not break backtest reads (which use offline path).
- **Expected Payoff:** Predictable memory footprint.

---

### AGENT — Agents Service

#### AGENT-001 — Multi-Agent Supervisor

- **Evidence Level:** A — Verified
  (8 separate `main.py` per agent)
- **Blast Radius:** Low
- **Target:** new `services/agents/src/agents/supervisor.py`
- **Change:** `supervise([GbmPredictor, RegimeAgent, ...])`
  hosts all eight in one process. One Redis client, one
  heartbeat, one tracing context.
- **Acceptance Criteria:**
  - Each agent's existing `setup/run/teardown` still passes.
  - Cold start amortized across 8 agents.
  - Fetch concurrency cap (`asyncio.Semaphore(8)`) for
    sentiment / news agents.
- **Expected Payoff:** ~8× reduction in cold-start cost; one
  cap on the I/O pool.

#### AGENT-002 — Platt-Scaled Confidence Calibration

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new `services/agents/src/agents/calibration.py`
- **Change:** `CalibratedAgent` mixin. Each `(prediction,
  realized_outcome)` pair refits a logistic on the last N
  outcomes; `confidence` is the calibrated P(up|features).
- **Acceptance Criteria:**
  - Per-agent calibration plot in dashboard.
  - Conformal gate abstentions decrease (calibration matches).
  - Existing agent tests still pass.
- **Expected Payoff:** The orchestrator's consensus stops being
  dominated by the most-overconfident agent.

#### AGENT-003 — Agent Registry / Discovery Decorator

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Low
- **Target:** new `services/agents/src/agents/registry.py`
- **Change:** A decorator so a new agent only needs
  `@register_agent` to be queryable, gateable, and promotable.
- **Acceptance Criteria:**
  - Adding an agent requires no edits to API routes.
  - Registry can list / disable / deprecate.
- **Expected Payoff:** Builder velocity for new agents.

#### AGENT-004 — Regime Conditioning on Prediction

- **Evidence Level:** A — Verified
  (regime_agent exists, no field on `Prediction`)
- **Blast Radius:** High (schema change)
- **Target:** `Prediction` schema in
  `libs/fincept-core/src/fincept_core/schemas.py`
- **Change:** Add `regime: RegimeLabel` field. MoE router
  (`quant_foundry/moe_router.py`) consumes it.
- **Acceptance Criteria:**
  - Regime-aware predictions are visually distinct in dashboard.
  - Backwards compatible (default = "unknown").
  - MoE router uses regime as one of its routing features.
- **Expected Payoff:** A trending-vs-crash GBM behaves
  appropriately; the orchestrator can size differently.

#### AGENT-005 — Agent Retirement Hook

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/orchestrator/src/orchestrator/consensus.py`
- **Change:** `DossierStatus.RETIRED` terminal state.
  `retire(agent_id, reason)` write that the orchestrator
  honors by dropping all `prediction.agent_id == retired_id`
  from consensus math.
- **Acceptance Criteria:**
  - Retired agents no longer influence target notional.
  - Retired agent's history remains in audit log.
- **Expected Payoff:** Clean lifecycle for dead models.

---

### ORCH — Orchestrator

#### ORCH-001 — Dispersion-Aware Consensus Confidence

- **Evidence Level:** A — Verified
  (`consensus.py:103` does `avg_confidence = total_conf / len(fresh)`)
- **Blast Radius:** Medium
- **Target:** `services/orchestrator/src/orchestrator/consensus.py:96-114`
- **Current Behavior:** When one agent says `direction=1,
  confidence=0.05` and another `direction=-1, confidence=0.95`,
  weighted direction is correct but `avg_confidence` is `0.5`
  — *uncorrelated with direction strength*.
- **Change:** Apply `confidence *= (1 - std(directions) / 2)`
  so disagreement deflates the reported confidence.
- **Acceptance Criteria:**
  - All existing consensus tests still pass.
  - Two opposing high-confidence agents now produce a small
    target notional (currently produces full magnitude).
  - Backtest shows fewer false-positive entries on
    high-disagreement regimes.
- **Expected Payoff:** Largest single improvement to consensus
  math at < 50 LOC.

#### ORCH-002 — Decision Rationale Field

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** High (schema change)
- **Target:** `DecisionEvent` schema in
  `libs/fincept-core/src/fincept_core/schemas.py`
- **Change:** Add `rationale: dict[str, Any]` carrying
  per-agent contribution, MoE weights, allocator inputs.
- **Acceptance Criteria:**
  - Operator can see "why this order?" on every
    `DecisionEvent`.
  - Backwards compatible.
  - Dashboard renders the rationale in `/decisions/<id>`.
- **Expected Payoff:** Auditability of the entire decision
  spine.

#### ORCH-003 — Fractional Kelly Sizing

- **Evidence Level:** A — Verified
  (allocator.py:17 explicitly defers to "TASK-042")
- **Blast Radius:** Medium
- **Target:** new
  `services/orchestrator/src/orchestrator/fractional_kelly.py`
- **Change:** `fractional_kelly_target_notional(direction,
  confidence, var_estimate, fraction=0.25, cap=...)`. Uses
  rolling variance from the features service.
- **Acceptance Criteria:**
  - Kelly output never exceeds `cap_per_symbol`.
  - Backtest on 2008/2020 stress data is more capital-efficient
    than the v1 linear allocator.
  - Kill-switch still hard-overrides.
- **Expected Payoff:** Provably safer sizing (industry
  default).

#### ORCH-004 — Drawdown-Aware Throttle

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/orchestrator/src/orchestrator/allocator.py`
- **Change:** `drawdown_throttle: float` parameter. Scales
  linearly from 1.0 (no drawdown) to 0.25 (max drawdown) over
  the configured band.
- **Acceptance Criteria:**
  - Throttle is driven by `RiskContext.realized_drawdown`.
  - In backtest, a strategy that drifts to -5% halves its
    next intent.
- **Expected Payoff:** Strategies in drawdown de-risk
  automatically.

#### ORCH-005 — MoE Router Hook

- **Evidence Level:** A — Verified
  (`moe_router.py` exists; orchestrator does not call it)
- **Blast Radius:** Medium
- **Target:** `services/orchestrator/src/orchestrator/consensus.py`
- **Change:** Config flag `use_moe_router: bool`. When true,
  the linear combiner is replaced with a routing call.
- **Acceptance Criteria:**
  - Flag is a runtime config (no code change to flip).
  - MoE-abstain returns `None` (orchestrator sits out).
  - All existing consensus tests pass with flag = False.
- **Expected Payoff:** The MoE becomes load-bearing, not
  decorative.

---

### RISK — Risk Service

#### RISK-001 — Reduce-and-Allow Risk Result

- **Evidence Level:** A — Verified
  (`checks.py:29-30` explicitly says v1 is binary)
- **Blast Radius:** High
- **Target:** `services/risk/src/risk/checks.py`,
  `RiskCheckResult` schema
- **Change:** Add `reduce_to: Decimal | None` to
  `RiskCheckResult`. Kill switch still hard-rejects; per-symbol
  cap and gross cap may return a reduced notional.
- **Acceptance Criteria:**
  - `RiskCheckResult` supports `approved`, `rejected`, and
    `reduced` (decided by `reasons == []` and `reduce_to is
    not None`).
  - Kill switch still produces `rejected` only.
  - Gross notional cap is never exceeded by the reduced
    notional.
  - Existing binary approve/reject behavior remains
    backwards compatible.
  - Unit tests for oversized-but-reducible orders.
  - Integration test: `OrderIntent → RiskCheckResult`.
- **Expected Payoff:** Strategies no longer need to know
  about risk caps; orchestrator can shrink to fit.

#### RISK-002 — Net Notional / Leverage Cap

- **Evidence Level:** A — Verified
  (`checks.py:96` uses `copy_abs()` — gross-only)
- **Blast Radius:** Medium
- **Target:** `services/risk/src/risk/checks.py`,
  `RiskContext` schema
- **Change:** Add `signed_notional_by_symbol` to
  `RiskContext`. Add `MAX_NET_LONG_USD` and
  `MAX_NET_SHORT_USD` settings.
- **Acceptance Criteria:**
  - Long + short sequence of intents no longer momentarily
    exceeds gross cap through the absolute-value lens.
  - Test exercises long-then-short sequence.
- **Expected Payoff:** Real net-exposure control.

#### RISK-003 — Auto Kill-Switch on Drawdown

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Critical
- **Target:** new `services/risk/src/risk/drawdown.py`
- **Change:** Rolling realized P&L tracker; engages kill
  switch automatically at `-max_drawdown_pct`.
- **Acceptance Criteria:**
  - Engages within one fill of the threshold.
  - Engagement is recorded in audit log with reason
    `drawdown_breach`.
  - Operator can override / reset.
- **Expected Payoff:** Catastrophic-drawdown protection that
  does not depend on the operator being awake.

#### RISK-004 — Risk Rejection History

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `services/api/src/api/routes/` (new
  `risk.py` extension)
- **Change:** Persist the last N rejection reasons with
  `intent_id`, `symbol`, `notional`, and `reason_code`. Surface
  at `/risk/rejections`.
- **Acceptance Criteria:**
  - Dashboard `/risk/rejections` lists recent rejections
    with drill-down.
  - Retention is configurable (default 30 d).
- **Expected Payoff:** Operator can answer "why was that
  order rejected?" in one click.

#### RISK-005 — Position-Side Snapshot Diff

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Low
- **Target:** `services/risk/src/risk/snapshot.py`
- **Change:** Cache `RiskContext` per `(strategy_id,
  ts_window)` so multiple intents in the same decision
  window share one snapshot build.
- **Acceptance Criteria:**
  - Cache invalidates on `FillEvent` or window end.
  - Existing tests pass.
- **Expected Payoff:** ~10× reduction in snapshot-build cost
  under burst intents.

---

### OMS — Order Management

#### OMS-001 — Per-Order Dedup (Replay Safety)

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new `services/oms/src/oms/dedup.py`
- **Change:** Redis `SET ord.dedup` with `order_id →
  fill_id`. Replayed orders are no-ops.
- **Acceptance Criteria:**
  - `paper_spine_replay` is idempotent: two runs with the
    same fixture produce identical position state.
  - Dedup table TTL = 7 d.
- **Expected Payoff:** Replay correctness; safe rebuilds
  from audit log.

#### OMS-002 — Fill Attribution Metadata

- **Evidence Level:** A — Verified
  (`Fill` schema carries no `evidence_id`)
- **Blast Radius:** High (schema change)
- **Target:** `Fill` schema in
  `libs/fincept-core/src/fincept_core/schemas.py`
- **Change:** Add `evidence_id: str` linking to the tick /
  bar / book snapshot that produced the fill.
- **Acceptance Criteria:**
  - Any fill independently auditable.
  - Dashboard `/fills/<id>` shows the underlying market
    context.
- **Expected Payoff:** Every fill is a black-box event.

#### OMS-003 — Tiered Fee Schedule

- **Evidence Level:** A — Verified
  (`paper.py:46-48` uses fixed 5 bps / 1 bps)
- **Blast Radius:** Medium
- **Target:** `services/oms/src/oms/paper.py:46-48`
- **Change:** `FeeSchedule` config supporting per-symbol
  tiering (e.g. `Binance VIP 3`).
- **Acceptance Criteria:**
  - Backtest on Binance-style fee ladder shows tighter
    return estimates.
  - Existing tests pass.
- **Expected Payoff:** Most realistic backtest possible
  without a live venue.

#### OMS-004 — Slippage + Volatility-Dependent Spread

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new `services/oms/src/oms/impact.py`
- **Change:** `SquareRootImpact` slippage
  `slippage = η × σ × sqrt(qty/adv)`. Volatility-dependent
  spread (3 bps calm, 30 bps crash).
- **Acceptance Criteria:**
  - A $250k BTC market order in the test fixture shows
    measurable slippage.
  - Crash-regime spread is wider than trending-regime.
- **Expected Payoff:** Honest edge estimates for momentum
  strategies.

#### OMS-005 — BrokerAdapter Interface

- **Evidence Level:** A — Verified
  (`alpaca/` is a stub; no `BrokerAdapter` protocol)
- **Blast Radius:** Medium
- **Target:** new `services/oms/src/oms/broker.py`
- **Change:** `BrokerAdapter` protocol. `MockBroker`,
  `AlpacaBroker`, future `IBBroker`.
- **Acceptance Criteria:**
  - Orchestrator does not know which broker is plugged in.
  - `AlpacaBroker` and `MockBroker` both satisfy the
    protocol.
- **Expected Payoff:** Future live path is one config flag.

---

### PORT — Portfolio Service

#### PORT-001 — P&L Attribution (Alpha vs Beta vs Fees)

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new
  `services/portfolio/src/portfolio/attribution.py`
- **Change:** Brinson-style decomposition against a
  user-defined benchmark. Decompose P&L into
  (alpha, beta, fees, slippage, residual).
- **Acceptance Criteria:**
  - `/portfolio/<id>/attribution` shows the decomposition
    over a chosen window.
  - Decomposition sums back to total realized P&L.
- **Expected Payoff:** Operators can tell if P&L is skill
  or correlation with SPY.

#### PORT-002 — Streaming P&L Aggregator

- **Evidence Level:** A — Verified
  (no streaming P&L roll-up)
- **Blast Radius:** Medium
- **Target:** `services/portfolio/src/portfolio/state.py`
- **Change:** Maintain `realized_pnl_by_strategy` on every
  fill; mark-to-market `unrealized_pnl_by_strategy` on
  every price tick.
- **Acceptance Criteria:**
  - Dashboard P&L refreshes sub-second without API re-fetch.
  - Aggregates are consistent with the per-position
    `apply_fill_to_position` kernel.
- **Expected Payoff:** Sub-second P&L on the dashboard
  without a back-end round trip per tick.

#### PORT-003 — Tax-Lot Tracking

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** High (schema change)
- **Target:** `Lot` model in `fincept_core.portfolio`
- **Change:** `apply_fill_to_position` emits realized P&L
  per lot with FIFO/LIFO/specific-id choice.
- **Acceptance Criteria:**
  - Sum of per-lot P&L = total realized P&L.
  - Configurable accounting method per strategy.
- **Expected Payoff:** Ready for real-money path.

#### PORT-004 — Corporate Action Consumer

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new `services/portfolio/src/events/corporate_actions.py`
- **Change:** Consumer for split / dividend / merger events
  that adjusts positions and pays dividends into cash.
- **Acceptance Criteria:**
  - 2:1 split on a held position doubles the share count
    and halves cost basis.
  - Dividend payment lands in `cash_usd` within one cycle.
- **Expected Payoff:** Realistic portfolio state on event
  days.

#### PORT-005 — Portfolio State Versioning

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/portfolio/src/portfolio/store.py`
- **Change:** `state_version: int` key; migration check on
  `hydrate`.
- **Acceptance Criteria:**
  - A schema migration refuses to load incompatible state
    without an explicit upgrade path.
- **Expected Payoff:** Zero silent corruption on schema
    changes.

---

### QF — Quant Foundry

#### QF-001 — Close-the-Loop Automation

- **Evidence Level:** A — Verified
  (sentinel, retrain, dossier, promotion exist as separate
  manual steps)
- **Blast Radius:** Critical
- **Target:** new
  `services/quant_foundry/src/quant_foundry/loop.py`
- **Change:** When `drift_sentinel` emits
  `RETRAIN/RETIRE`, the loop:
  1. Calls `gateway.create_job(type=retrain)`.
  2. Waits for callback.
  3. Updates `dossier` with the new `model_version`.
  4. Re-evaluates `promotion` against the new evidence
     packet.
  5. Emits a `state_change` event to the API / dashboard.
- **Acceptance Criteria:**
  - A fixture model whose sentinel goes from `NO_ACTION` to
    `RETIRE` is automatically marked retired in the dossier.
  - A model whose sentinel goes to `RETRAIN` produces a
    retrain job within 60 s.
  - All transitions logged in audit.
  - Operator can pause the loop.
- **Expected Payoff:** The single most leveraged platform
  work — the manual close-the-loop becomes a documented
  automated process.

#### QF-002 — Regime-Conditional Conformal Calibrator

- **Evidence Level:** A — Verified
  (`conformal_gate.py` calibrates one set of residuals per
  model)
- **Blast Radius:** Medium
- **Target:** new
  `services/quant_foundry/src/quant_foundry/conformal_gate_regime.py`
- **Change:** `RegimeConformalCalibrator` keeps one
  calibrator per `(model, regime)` pair.
- **Acceptance Criteria:**
  - Backtest shows better calibration in crash regimes.
  - Existing per-model calibrator untouched (default).
- **Expected Payoff:** Honest uncertainty in regime shifts.

#### QF-003 — Continuous Trust Score (Sentinel)

- **Evidence Level:** A — Verified
  (drift_sentinel returns one of five enum values)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/drift_sentinel.py`
- **Change:** Add `trust_score: float ∈ [0, 1]` alongside
  the recommendation. The orchestrator scales position
  size by trust.
- **Acceptance Criteria:**
  - `trust_score = 0.8` scales a target notional to 80%.
  - Existing enum recommendation unchanged.
- **Expected Payoff:** Smooth de-risking instead of
  jump-cut between 100% and 0%.

#### QF-004 — Learned MoE Router (Logistic on Regime Features)

- **Evidence Level:** A — Verified
  (the comment in `moe_router.py:9-10` promises this and
  the second step is not built)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/moe_router.py`
- **Change:** When `n_settled >= 1000`, fit a logistic
  regression on `(regime, horizon, feature_availability,
  liquidity)` to predict best model.
- **Acceptance Criteria:**
  - Learned router is gated on `n_settled`; below threshold,
    the rule-based router is used.
  - Learned router is persisted in the model registry.
- **Expected Payoff:** Personalized routing that improves
  with data.

#### QF-005 — Causal Graph Streaming Updater

- **Evidence Level:** A — Verified
  (`causal_graph.py` has nodes/edges but no streaming
  update method)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/causal_graph.py`
- **Change:** On each new evidence event, recompute
  conditional independence tests for the affected triple.
- **Acceptance Criteria:**
  - New evidence produces new edges within one cycle.
  - Edges below strength threshold are pruned.
- **Expected Payoff:** The causal graph becomes a live
  research artifact, not a snapshot.

#### QF-006 — Promotion Gate Time-Decay

- **Evidence Level:** A — Verified
  (`promotion.py:226-229` checks only `settled_count`)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/promotion.py`
- **Change:** Add `max_age_days` parameter and
  `last_settlement_ts` check.
- **Acceptance Criteria:**
  - 10 settlements from 6 months ago is insufficient.
  - 10 settlements from this morning is sufficient.
- **Expected Payoff:** The promotion gate stops rewarding
  ancient evidence.

#### QF-007 — Paper-Bridge Feedback Loop

- **Evidence Level:** A — Verified
  (`paper_bridge.py` is one-way; OMS feedback is
  unconsumed)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/paper_bridge.py`
- **Change:** Add `paper_bridge_callback` consumer that
  scores each model by realized P&L over the position's
  lifetime and writes to the leaderboard.
- **Acceptance Criteria:**
  - A filled-and-closed position contributes P&L to the
    model's leaderboard entry.
  - Per-model P&L visible in `/quant-foundry/models/<id>`.
- **Expected Payoff:** The leaderboard becomes the
  authoritative ranking signal.

#### QF-008 — Model "Why Abstained" Explanation

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new
  `services/quant_foundry/src/quant_foundry/explain.py`
- **Change:** `Explanation` object combining
  `(causal edges activated, MoE weights, conformal
  interval, regime, trust score)` into one audit-friendly
  record.
- **Acceptance Criteria:**
  - Every abstention has a human-readable explanation.
  - Dashboard shows the explanation in
    `/quant-foundry/models/<id>/abstain`.
- **Expected Payoff:** Trust + auditability for the
  close-the-loop.

#### QF-009 — Budget Forecast (Not Just a Trip)

- **Evidence Level:** A — Verified
  (`budget.py:107-196` returns binary allowed/refused)
- **Blast Radius:** Low
- **Target:** `services/quant_foundry/src/quant_foundry/budget.py`
- **Change:** Add a `forecast` method that returns "at
  current burn rate, you'll hit the ceiling in N days."
- **Acceptance Criteria:**
  - `/quant-foundry/budget` shows current spend, ceiling,
    and forecast.
  - Forecast updates every 5 min.
- **Expected Payoff:** Operators plan GPU spend instead of
  reacting to it.

#### QF-010 — Champion/Challenger Routing

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/orchestrator/src/orchestrator/router.py`
- **Change:** Split routing — 90% to champion, 10% to
  challenger. P&L of each is tracked separately.
- **Acceptance Criteria:**
  - Dashboard shows champion vs challenger performance
    side-by-side.
  - Promotion to "new champion" requires rolling 30-d
    outperformance.
- **Expected Payoff:** Real A/B for free.

#### QF-011 — RunPod Warm Pool

- **Evidence Level:** A — Verified
  (`runpod_client.py` is single-shot)
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/runpod_client.py`
- **Change:** Maintain 1–2 idle pods. Dispatch picks a
  warm pod first, provisions a new one only if all are
  busy.
- **Acceptance Criteria:**
  - Cold start avoided for ≤ 2 concurrent jobs.
  - Pool size is configurable.
  - Idle pods are auto-killed after N min.
- **Expected Payoff:** "Request → first batch" goes from
  90–180 s to seconds.

#### QF-012 — Leaderboard Persistence

- **Evidence Level:** A — Verified
  (`leaderboard.py` and `leaderboard_expanded.py` are
  in-process)
- **Blast Radius:** Low
- **Target:** new
  `services/quant_foundry/src/quant_foundry/leaderboard_store.py`
- **Change:** Persist leaderboard to a Redis sorted set
  keyed by `(model_id, regime, horizon)`.
- **Acceptance Criteria:**
  - Leaderboard survives a process restart.
  - Reads are O(log N).
- **Expected Payoff:** No restart-loses-history bug.

#### QF-013 — Scheduled Shadow Settlement

- **Evidence Level:** A — Verified
  (no scheduler; settlement is on-demand)
- **Blast Radius:** Medium
- **Target:** new
  `services/quant_foundry/src/jobs/shadow_settle.py`
- **Change:** Cron-style "settle the last 24 h of shadow
  predictions against the last 24 h of realized outcomes
  every 5 min."
- **Acceptance Criteria:**
  - Settlement is idempotent (re-running on the same
    window produces identical leaderboard updates).
  - Backpressure-aware: skips windows if the settlement
    queue is full.
- **Expected Payoff:** Leaderboard updates continuously,
  not on demand.

#### QF-014 — Model Registry Cross-Check

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `services/quant_foundry/src/jobs/registry_audit.py`
- **Change:** Daily job that diffs `registry` vs `dossier`
  and raises an alert on drift.
- **Acceptance Criteria:**
  - A retrained-and-registered model whose dossier still
    points to the old version raises an alert.
- **Expected Payoff:** No silent registry/dossier drift.

---

### BACKTEST — Backtester

#### BACKTEST-001 — Walk-Forward CPCV + PBO

- **Evidence Level:** A — Verified
  (`pbo.py` exists; backtester does not surface PBO)
- **Blast Radius:** Medium
- **Target:** `services/backtester/src/backtester/cli.py`
  (or new `sweep.py`)
- **Change:** `backtester run --cpcv --n-paths 16` exposes
  CPCV. Report includes PBO.
- **Acceptance Criteria:**
  - PBO is in the report JSON.
  - CLI matches the documented signature.
- **Expected Payoff:** Standard research-rigor gate; protects
  against overfit strategies.

#### BACKTEST-002 — Per-Symbol Cost & ADV Slippage

- **Evidence Level:** A — Verified
  (`costs.py` is global)
- **Blast Radius:** Low
- **Target:** `services/backtester/src/backtester/costs.py`
- **Change:** Per-symbol fees + ADV-based slippage.
- **Acceptance Criteria:**
  - Backtest on a known dataset shows tighter return
    estimates.
- **Expected Payoff:** Realistic per-symbol edge.

#### BACKTEST-003 — Regime-Filter Backtest

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `services/backtester/src/backtester/regime_filter.py`
- **Change:** `--regime-filter` re-runs against specific
  regimes (using the regime agent output).
- **Acceptance Criteria:**
  - Backtest report tagged with regime distribution.
  - Regime-specific Sharpe computed.
- **Expected Payoff:** "Is this edge regime-specific?" in
  one CLI.

#### BACKTEST-004 — Parameter-Search Surface

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `services/backtester/src/backtester/sweep.py`
- **Change:** `grid_search(strategy, param_grid, ...)` with
  report ranking by Sharpe-with-PBO-penalty.
- **Acceptance Criteria:**
  - 100-run sweep completes in < 5 min on a single core.
- **Expected Payoff:** Today it's a shell loop.

#### BACKTEST-005 — Best-Run Archive

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new `services/backtester/src/backtester/runs/`
  storage path
- **Change:** Every backtest writes
  `(commit_hash, params, report, fingerprint)` to
  `runs/YYYY-MM-DD/<id>.json`.
- **Acceptance Criteria:**
  - Re-running with the same `commit_hash` and `params`
    produces the same report hash.
- **Expected Payoff:** Audit-friendly research log.

#### BACKTEST-006 — Multiprocessing Pool

- **Evidence Level:** C — Needs inspection
  (`engine.py` is single-threaded by inspection but not
  fully read)
- **Blast Radius:** Low
- **Target:** `services/backtester/src/backtester/engine.py`
- **Change:** Multiprocessing pool keyed on strategy.
- **Acceptance Criteria:**
  - 50 strategies × 200 symbols uses all cores.
  - No shared-state races.
- **Expected Payoff:** Sweep speedup proportional to cores.

---

### API — API Gateway

#### API-001 — Prometheus `/metrics` + Structured Logs

- **Evidence Level:** A — Verified
  (no metrics endpoint, no canonical log schema)
- **Blast Radius:** Medium
- **Target:** new `services/api/src/api/observability.py`
- **Change:** `prometheus_client` HTTP endpoint; structured
  log schema with `correlation_id`.
- **Acceptance Criteria:**
  - `/metrics` returns Prometheus format.
  - All services emit JSON logs with `correlation_id`.
  - Logs shippable to Loki / Datadog.
- **Expected Payoff:** Infrastructure for everything that
  follows.

#### API-002 — Safe Operator Error Envelope

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** High (response shape)
- **Target:** new `services/api/src/api/error_envelope.py`
- **Change:** Global FastAPI exception handler returns
  `{"code", "message", "correlation_id"}`. No stack-trace
  leakage.
- **Acceptance Criteria:**
  - All `/api/**` errors are stable-shape.
  - Stack traces are server-side only.
  - `correlation_id` is propagated through logs.
- **Expected Payoff:** Security + operator debuggability.

#### API-003 — OpenBB Readiness Matrix

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/api/src/api/routes/research.py`
- **Change:** `/research/openbb/matrix` returns a
  per-provider, per-capability, per-expected-column grid.
- **Acceptance Criteria:**
  - Each entry has `provider`, `capability`,
    `expected_columns`, `last_success_at`, `last_error`.
- **Expected Payoff:** Operator evidence (called out in
  `featuresmenu.md`).

#### API-004 — Streaming `/data/coverage`

- **Evidence Level:** A — Verified
  (the 5 s timeout is a real blocker per
  `REVIEW_2026-06-23`)
- **Blast Radius:** Medium
- **Target:** `services/api/src/api/routes/data.py`
- **Change:** Server-sent events / chunked JSON. First 100
  symbols immediately, the rest as they compute.
- **Acceptance Criteria:**
  - First batch visible in < 500 ms.
  - Full coverage reachable without 5 s timeout.
- **Expected Payoff:** Removes the data-page blocker.

#### API-005 — Per-Topic WebSocket Subscriptions

- **Evidence Level:** A — Verified
  (the WS router is one channel)
- **Blast Radius:** Medium
- **Target:** `services/api/src/api/ws.py`
- **Change:** Per-topic subscriptions
  (`subscribe sig.* for BTC-USDT only`).
- **Acceptance Criteria:**
  - Dashboard opens one socket per page; no global
    filtering on the client.
  - Subscription throttling on the server.
- **Expected Payoff:** Dashboard network load drops 10×.

#### API-006 — Symbol Search Endpoint

- **Evidence Level:** C — Needs inspection
  (`symbol_search.py` exists but surface unclear)
- **Blast Radius:** Low
- **Target:** `services/api/src/api/routes/` (new
  `symbol.py` extension)
- **Change:** Public, versioned `/symbol/search` with rate
  limiting.
- **Acceptance Criteria:**
  - Documented response shape; rate limit configured.
- **Expected Payoff:** Usable from the dashboard.

#### API-007 — Auth Refresh Tokens

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** High (auth flow)
- **Target:** `services/api/src/api/auth.py`
- **Change:** Refresh-token flow with dual-secret-validity
  window.
- **Acceptance Criteria:**
  - Access token (15 min) + refresh token (30 d).
  - Old refresh secret valid for 24 h after rotation.
- **Expected Payoff:** Production-grade auth.

#### API-008 — Background Tasks as Separate Workers

- **Evidence Level:** A — Verified
  (`main.py` runs Alpaca + news schedulers in-process)
- **Blast Radius:** Medium
- **Target:** new `services/api/src/api/worker.py`
- **Change:** Schedulers run in a separate process
  supervised by the API.
- **Acceptance Criteria:**
  - A scheduler crash restarts without taking down the API.
- **Expected Payoff:** Reliability.

#### API-009 — OpenAPI Spec Validation in CI

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new `tests/api/test_openapi_parity.py`
- **Change:** Compare live OpenAPI spec to a checked-in
  `openapi.json`. Test fails on drift.
- **Acceptance Criteria:**
  - Accidental breaking changes fail tests.
- **Expected Payoff:** No silent contract drift.

---

### DASH — Dashboard

#### DASH-001 — Operator Briefing Page

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/app/briefing/page.tsx`
- **Change:** Natural-language summary of the last 24 h:
  what moved, what models abstained, what promotions
  happened, what risks tripped. Wired to the black-box
  recorder + `OperatorBriefingCard` widget.
- **Acceptance Criteria:**
  - Page renders in < 1 s.
  - Briefing includes at least 5 categorized items.
  - Operator can drill into each item.
- **Expected Payoff:** Single highest-VE dashboard feature.

#### DASH-002 — Model Explanation View

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/app/quant-foundry/models/[id]/explain/page.tsx`
- **Change:** Read-only view of the `Explanation` object
  (QF-008).
- **Acceptance Criteria:**
  - Shows causal edges, MoE weights, conformal interval,
    regime, trust score.
  - No-order boundary explicit.
- **Expected Payoff:** Trust + auditability.

#### DASH-003 — Promotion / Retirement Notifications

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/components/notifications/center.tsx`
- **Change:** Persistent toast + notification center for
  `model_promoted`, `model_retired`, `sentinel_retrain`,
  `budget_alarm`.
- **Acceptance Criteria:**
  - WebSocket-driven; survives page refresh.
  - Acknowledging dismisses until next instance.
- **Expected Payoff:** Operator never misses a state
  change.

#### DASH-004 — Operator Performance Panel

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/app/system/performance/page.tsx`
- **Change:** Latency heatmap + per-service resource use
  + slow-symbol table.
- **Acceptance Criteria:**
  - Renders from `/metrics` + ING-003 latency store.
- **Expected Payoff:** Tail-latency visible to operators.

#### DASH-005 — Keyboard Shortcuts

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/hooks/use-hotkeys.ts`
- **Change:** `g p` → positions, `g o` → orders, `?` →
  help. Vim-style two-key sequences.
- **Acceptance Criteria:**
  - No shortcut conflicts; `?` shows the cheatsheet.
- **Expected Payoff:** Power-user velocity.

#### DASH-006 — Saved Views

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `apps/dashboard/src/hooks/use-saved-views.ts`
- **Change:** `localStorage`-backed customizable widget
  grids. Per-user.
- **Acceptance Criteria:**
  - Views persist across sessions; export/import JSON.
- **Expected Payoff:** Personalized operator surfaces.

#### DASH-007 — Model Page Sidebar

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `apps/dashboard/src/app/quant-foundry/models/[id]/layout.tsx`
- **Change:** Sidebar with `shadow / causal / promotion /
  dossier` links.
- **Acceptance Criteria:**
  - All four subpages reachable in 1 click.
- **Expected Payoff:** Cross-page navigation density.

#### DASH-008 — Reconciliation Discrepancy Push

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `apps/dashboard/src/app/reconciliation/page.tsx`
- **Change:** WebSocket-pushed alert when reconciliation
  finds a mismatch; one-click drill-down.
- **Acceptance Criteria:**
  - Alert visible from any page; resolved on acknowledgement.
- **Expected Payoff:** Reconciliation is no longer
  wallpaper.

#### DASH-009 — Strategy Detail Live P&L Attribution

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `apps/dashboard/src/app/strategies/[id]/page.tsx`
- **Change:** Mini-attribution chart on the strategy
  detail page.
- **Acceptance Criteria:**
  - Decomposes into (alpha, beta, fees) over the chosen
    window.
- **Expected Payoff:** Skill vs correlation is visible per
  strategy.

#### DASH-010 — Login Wired to Real Auth

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `apps/dashboard/src/app/login/page.tsx`
- **Change:** Wire to API JWT + refresh-token flow.
  "Remember me" toggle.
- **Acceptance Criteria:**
  - Login succeeds, refresh works, logout clears the
    session.
- **Expected Payoff:** Dashboard is no longer a demo.

---

### STRAT — Strategy Host

#### STRAT-001 — Strategy Twin Replay Test

- **Evidence Level:** D — Product hypothesis
  (per `featuresmenu.md` §Strategy-host twin replay)
- **Blast Radius:** Low
- **Target:** new
  `services/strategy_host/tests/test_twin_replay.py`
- **Change:** Fixture with two strategies (one enabled,
  one disabled). Assert only the enabled strategy
  publishes.
- **Acceptance Criteria:**
  - Disabled strategy produces no bus events.
  - Enabled strategy produces traceable paper orders.
- **Expected Payoff:** Governance boundary is testable.

#### STRAT-002 — Strategy Status Surface

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Low
- **Target:** `services/strategy_host/src/strategy_host/supervisor.py`
- **Change:** `/strategy/<id>/status` returns supervisor
  state (running / paused / restarting / errored).
- **Acceptance Criteria:**
  - Operator can see why a strategy is not emitting.
- **Expected Payoff:** Operator debuggability.

---

### JOBS — Scheduled Jobs

#### JOBS-001 — Reconciliation Job Parity

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Low
- **Target:** `services/jobs/src/jobs/main.py`
- **Change:** Add a daily reconciliation job that diffs
  bus state vs DB state and writes a receipt.
- **Acceptance Criteria:**
  - Receipt is a JSON file under `reports/reconciliation/`.
  - Discrepancy > 0 alerts via DASH-008.
- **Expected Payoff:** Drift is caught automatically.

#### JOBS-002 — Calibration Sweep Job

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `services/jobs/src/jobs/calibration_sweep.py`
- **Change:** Nightly job that recomputes per-agent
  calibration on the last 30 d of settled predictions.
- **Acceptance Criteria:**
  - Calibration report under `reports/calibration/`.
  - Triggers AGENT-002 if drift detected.
- **Expected Payoff:** Calibration stays fresh.

---

### CORE — Cross-Cutting Foundation

#### CORE-001 — Latency Trace Envelope

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** High (event schema)
- **Target:** new `libs/fincept-core/src/fincept_core/trace.py`
- **Change:** Add `latency_trace: dict[stage, ts_ns]` to
  every event envelope.
- **Acceptance Criteria:**
  - Stamped at: ingest, normalize, store, feature,
    signal, risk, OMS, portfolio.
  - Queryable: any event can be traced to its
    end-to-end duration.
- **Expected Payoff:** Tail-latency language for the whole
  platform.

#### CORE-002 — Schema Versioning + Compatibility Shim

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Critical (event schema)
- **Target:** `libs/fincept-core/src/fincept_core/schemas.py`
- **Change:** `schema_version: int = N` on every event.
  Compatibility shim layer for in-flight deserialization.
- **Acceptance Criteria:**
  - Old payloads deserialize under the current schema.
  - Version bump is a deliberate, audited action.
- **Expected Payoff:** Additive evolution without breaking
  the spine.

#### CORE-003 — Stable Error Codes

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `libs/fincept-core/src/fincept_core/errors.py`
- **Change:** `E_DATA_STALE`, `E_RISK_BREACH`,
  `E_PROVIDER_DOWN`, etc. Documented in `errors.md`.
- **Acceptance Criteria:**
  - API errors carry the stable code.
  - Operator docs map codes to remediation.
- **Expected Payoff:** Operator debuggability.

#### CORE-004 — Bus Batch Publish

- **Evidence Level:** A — Verified
  (`producer.py:18-25` does one XADD per call)
- **Blast Radius:** Low
- **Target:** `libs/fincept-bus/src/fincept_bus/producer.py`
- **Change:** `Producer.batch_publish(stream, events)`
  using Redis pipelines.
- **Acceptance Criteria:**
  - 3–5× throughput under burst.
  - Order preserved per stream.
  - Cancellation flushes the in-flight batch.
- **Expected Payoff:** Single afternoon, unlocks every
  service.

#### CORE-005 — Async DB Session

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Medium
- **Target:** `libs/fincept-db/src/fincept_db/engine.py`
- **Change:** Async session via `asyncpg` driver.
- **Acceptance Criteria:**
  - All services that hit the DB are non-blocking.
  - Existing sync paths still work.
- **Expected Payoff:** Removes event-loop blocking.

#### CORE-006 — orjson Serialization

- **Evidence Level:** C — Needs inspection
- **Blast Radius:** Low
- **Target:** `libs/fincept-core/src/fincept_core/events.py`
- **Change:** Swap `json` for `orjson`. Cache pre-encoded
  bytes for replay.
- **Acceptance Criteria:**
  - 3–5× speedup on `Event.serialize`.
  - Backwards-compatible wire format.
- **Expected Payoff:** Lower CPU on the hot path.

#### CORE-007 — Distributed Tracing (W3C Propagation)

- **Evidence Level:** A — Verified
  (OpenTelemetry present but no cross-service propagation)
- **Blast Radius:** Medium
- **Target:** `libs/fincept-bus/src/fincept_bus/types.py`
- **Change:** W3C `traceparent` and `tracestate` carried in
  bus envelope.
- **Acceptance Criteria:**
  - A single trace ID follows a decision from ingestor to
    OMS.
  - Visible in Jaeger / Tempo.
- **Expected Payoff:** Distributed debugging.

#### CORE-008 — Tool-Use Audit Black Box

- **Evidence Level:** D — Product hypothesis
  (per `featuresmenu.md` §Tool-call black box)
- **Blast Radius:** Medium
- **Target:** new
  `libs/fincept-tools/src/fincept_tools/audit.py`
- **Change:** Wrap every tool call with caller ID, run ID,
  input hash, output hash, duration, side-effect class.
- **Acceptance Criteria:**
  - Every tool call reconstructable from caller to
    inputs to output/error.
  - Per-tool latency + error rate metrics exported.
- **Expected Payoff:** Auditability of agent actions.

#### CORE-009 — Plugin Discovery for fincept-tools

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `libs/fincept-tools/src/fincept_tools/registry.py`
- **Change:** Entry-point-based discovery so
  `pip install fincept-tools-polygon` just works.
- **Acceptance Criteria:**
  - New provider requires no edits to the core package.
- **Expected Payoff:** Builder velocity.

#### CORE-010 — Rate-Limit-Aware Tool Executor

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** new
  `libs/fincept-tools/src/fincept_tools/rate_limit.py`
- **Change:** Token-bucket rate limiter with per-tool
  cost tracking.
- **Acceptance Criteria:**
  - Exa / OpenBB calls in a hot loop do not exceed
    configured quota.
  - 429 responses back off automatically.
- **Expected Payoff:** Quota safety for research spend.

---

### OBS — Observability / Operations

#### OBS-001 — Full Paper-Spine Smoke Test in CI

- **Evidence Level:** A — Verified
  (script exists at `scripts/paper_spine_replay.py`)
- **Blast Radius:** Low
- **Target:** `.github/workflows/ci.yml`
- **Change:** Every PR runs the paper-spine replay as a
  required check. Receipt is uploaded as a CI artifact.
- **Acceptance Criteria:**
  - A PR that breaks any stage (ingest → feature → signal
    → decision → risk → order → fill → portfolio) fails
    the check.
- **Expected Payoff:** The runtime non-negotiables (§2)
  are enforced.

#### OBS-002 — Prometheus + Structured Logs

- See **API-001** for the implementation. Tracked here as
  the cross-cutting observability gate.

#### OBS-003 — Audit Log Hash Chain

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `libs/fincept-db/src/fincept_db/audit.py`
- **Change:** Merkle root per day over the audit table.
  Tampering detectable.
- **Acceptance Criteria:**
  - Any retroactive edit breaks the chain on the next
    verification.
- **Expected Payoff:** Audit-log immutability.

#### OBS-004 — Secret-Rotation Support

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Critical
- **Target:** `libs/fincept-core/src/fincept_core/config.py`
- **Change:** Hot-reload of secrets; dual-secret-validity
  window (24 h overlap).
- **Acceptance Criteria:**
  - Old and new secrets both valid during the overlap.
  - Reload is non-blocking.
- **Expected Payoff:** Zero-downtime secret rotation.

#### OBS-005 — Cold-Start Prebuilds

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `.github/workflows/` per-service build
- **Change:** Per-service slim Docker image in CI. Cold
  start drops from 2–5 s to 200 ms.
- **Acceptance Criteria:**
  - Each service has a tracked prebuild.
- **Expected Payoff:** Faster CI; faster local dev.

#### OBS-006 — Model-Artifact Lifecycle

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** new
  `services/quant_foundry/src/quant_foundry/artifacts.py`
- **Change:** Retention policy
  `keep_last_n=5, keep_best_by_metric=true`.
- **Acceptance Criteria:**
  - S3 artifact store does not grow unbounded.
  - Best-by-metric model is always retained.
- **Expected Payoff:** Predictable storage cost.

#### OBS-007 — Receipt Pinning (uv.lock, Python, Data Window)

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Low
- **Target:** `scripts/paper_spine_replay.py`
- **Change:** Pin `uv.lock` hash, Python version, and
  data window into every receipt.
- **Acceptance Criteria:**
  - Replays on different lockfiles produce different
    receipts.
- **Expected Payoff:** Reproducibility.

#### OBS-008 — Data Fingerprint on Model Dossier

- **Evidence Level:** D — Product hypothesis
- **Blast Radius:** Medium
- **Target:** `services/quant_foundry/src/quant_foundry/dossier.py`
- **Change:** `data_fingerprint: str` on the dossier.
  Refuse promotion if the dossier's fingerprint does not
  match the live data fingerprint.
- **Acceptance Criteria:**
  - A model trained on different bars is blocked from
    promotion.
- **Expected Payoff:** No silent data drift.

---

## 8. 30/60/90-Day Roadmap

### Phase 0 — Keep It Running (Week 1)

1. **OBS-001** — smoke test in CI.
2. **CORE-001** — latency trace envelope (additive; no
   consumer breaks).
3. **CORE-004** — `Producer.batch_publish`.
4. **ING-001** — micro-batching.
5. **RISK-001** — reduce-and-allow.

### Phase 1 — Observability Before Features (Weeks 2–3)

6. **API-001** — Prometheus + structured logs.
7. **CORE-002** — schema versioning.
8. **ING-003** — latency heatmap.
9. **ING-002** — book resync requests.
10. **API-002** — safe error envelope.
11. **API-004** — streaming `/data/coverage`.

### Phase 2 — Highest ROI Product Value (Weeks 4–6)

12. **QF-001** — close-the-loop automation.
13. **QF-003** — continuous trust score.
14. **QF-002** — regime-conditional conformal calibrator.
15. **DASH-001** — operator briefing page.
16. **DASH-003** — promotion / retirement notifications.
17. **DASH-002** — model "why abstained" view.

### Phase 3 — Smarter Quant Work (Weeks 7–9)

18. **QF-004** — learned MoE router.
19. **QF-005** — causal graph streaming updater.
20. **QF-006** — promotion gate time-decay.
21. **QF-007** — paper-bridge feedback loop.
22. **QF-008** — model "why abstained" explanation.
23. **ORCH-001** — dispersion-aware confidence.
24. **ORCH-003** — fractional Kelly sizing.
25. **ORCH-004** — drawdown-aware throttle.

### Phase 4 — Discipline & Depth (Weeks 10–12)

26. **PORT-001** — P&L attribution.
27. **PORT-002** — streaming P&L aggregator.
28. **PORT-003** — tax-lot tracking.
29. **PORT-004** — corporate action consumer.
30. **RISK-002** — net notional / leverage cap.
31. **RISK-003** — auto kill-switch on drawdown.
32. **BACKTEST-001** — walk-forward CPCV + PBO.
33. **BACKTEST-002** — per-symbol cost & ADV slippage.
34. **BACKTEST-003** — regime-filter backtest.
35. **QF-009** — budget forecast.
36. **QF-011** — RunPod warm pool.
37. **QF-012** — leaderboard persistence.
38. **CORE-007** — distributed tracing (W3C).

---

## 9. Builder Execution Protocol

For every task ID in §7:

1. **Claim** the task in `ROADMAP.md` (or equivalent
   tracking file).
2. **Create** `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER_<n>_<TASK_ID>.md`
   with: files changed, commands run, tests passed/failed,
   remaining risks.
3. **Read** the source targets before writing.
4. **Write** the change with a unit test where
   applicable.
5. **Run** the smoke test (`scripts/paper_spine_replay.py`)
   and the route smoke (`scripts/route_smoke.py`).
6. **Document** the API / dashboard behavior delta.
7. **Document** the rollback path.
8. **Mark** the task `done` only after the §2 non-negotiables
   pass.

### Example Builder Prompt

```text
You are implementing TASK-ID-001: <name>.

Goal: <one-sentence change>.

Constraints:
- <list from Acceptance Criteria>.
- Existing tests must pass.
- Do not break frozen Pydantic schemas.
- Do not add new dependencies without justification.

Output:
- Code change in <target file>.
- Unit tests in <test file>.
- Implementation notes in
  docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER_X_<TASK-ID>.md
- Verification: scripts/paper_spine_replay.py + relevant
  pytest suite.
```

---

## 10. Definition of Done

A task is **done** when:

- [ ] Code implemented at the cited target.
- [ ] Unit test added (or updated) covering the change.
- [ ] Integration test added if the change touches streams
      or API contracts.
- [ ] Existing test suite passes locally.
- [ ] `scripts/paper_spine_replay.py` passes.
- [ ] `scripts/route_smoke.py` passes (if API touched).
- [ ] `scripts/openbb_live_proof.py` passes (if OpenBB
      touched).
- [ ] Dashboard behavior delta documented.
- [ ] API behavior delta documented.
- [ ] Rollback path documented.
- [ ] Builder notes written to
      `docs/AAA_GLM_SUPERTEAM_LOGS/BUILDER_X_<TASK-ID>.md`.

---

## 11. Do First / Do Next / Do Later / Do Not Do

| Bucket | Items |
| --- | --- |
| **Do first** | OBS-001, OBS-002, ING-001, RISK-001, CORE-001, CORE-004, QF-001, DASH-001 |
| **Do next** | ORCH-001, ORCH-003, QF-002, QF-003, DASH-002, DASH-003, PORT-001, BACKTEST-001 |
| **Do later** | QF-011, QF-004, PORT-003, PORT-004, OMS-005, CORE-007, CORE-008 |
| **Do not do yet** | Live trading, more agents, more dashboards, framework rewrites, multi-region deploys |

---

## 12. Risk Register

| Risk | Mitigation |
| --- | --- |
| Schema change breaks in-flight consumers | CORE-002 (schema versioning) before any High-blast-radius task |
| Latency trace adds overhead | CORE-001 stamped on a sampled subset first |
| Paper bridge starts emitting live signals | Confirmed disabled by default (`paper_bridge.py:56-57`); keep `QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE` env-gated |
| Operator ingests the dashboard without context | DASH-001 (briefing) and DASH-003 (notifications) must precede any new model surface |
| A test causes a real RunPod spend | All `gateway.create_job` calls go through `BudgetGuard` (`budget.py:107-196`) — never bypass |
| Drift sentinel auto-retires a working model | `qf-001` must default to `MONITOR`, not `RETIRE`; operator approval required for `RETIRE` |
| Backtester multiprocessing introduces non-determinism | Use a seeded RNG per strategy; pin the seed in the receipt (BACKTEST-005) |

---

## 13. What NOT To Do

A value-increase plan is incomplete without the antipatterns.

- **Do not add more agents.** The platform already has 8;
  each adds an integration tax. Better calibration +
  regime conditioning of the existing 8 will outperform
  a 9th.
- **Do not chase HFT latency.** The platform is
  paper-trading. The v1 latency budget (per
  `featuresmenu.md` §Latency budget ledger) is **seconds**,
  not microseconds. A quarter on microsecond ingestor
  rewrites is misallocated capital.
- **Do not add more dashboards.** The 17 top-level pages
  are already a lot. Make the existing pages dense and
  connected (DASH-007) before adding an 18th.
- **Do not add live trading yet.** The paper-first stance
  is correct. Every dollar spent on the live path before
  the operator can explain paper results is risk, not
  value.
- **Do not chase more ML frameworks.** The Quant Foundry
  is already multi-pronged (GBM, news-impact, baseline
  family, MoE, causal). Adding PyTorch Lightning or
  JAX-native training will create a maintenance burden
  for marginal value.
- **Do not rewrite the backtester.** It is good. Add PBO,
  CPCV, and slippage realism to it (BACKTEST-001 →
  BACKTEST-004).
- **Do not skip the smoke test.** A change that does not
  pass `scripts/paper_spine_replay.py` is not a change;
  it is a hypothesis.

---

## 14. Appendix — File/Line Evidence

| ID | Target | Lines | Claim |
| --- | --- | --- | --- |
| ING-001 | services/ingestor/src/ingestor/writer.py | n/a | Writer does one XADD per call (verified at main.py:102) |
| ING-002 | services/ingestor/src/ingestor/quality.py | 104-130 | `observe()` counts gaps but does not emit |
| ING-002 | services/ingestor/src/ingestor/main.py | 8-11 | Docstring defers gap recovery to "TASK-014" |
| ING-003 | services/ingestor/src/ingestor/quality.py | 138-151 | `snapshot()` is in-process only |
| ING-003 | services/ingestor/src/ingestor/quality.py | 89, 96-97 | 1024-sample ring (`deque(maxlen=1024)`) |
| ING-004 | services/ingestor/src/ingestor/quality.py | 31-32 | Tether-stable grouping explicitly deferred |
| ING-005 | libs/fincept-bus/src/fincept_bus/producer.py | 18-25 | One XADD per call |
| FEAT-002 | services/features/src/features/main.py | 50 | Consumes only `STREAM_MD_BARS_1M` |
| AGENT-001 | services/agents/src/agents/base.py | 32-54 | Three-hook lifecycle; one main.py per agent |
| ORCH-001 | services/orchestrator/src/orchestrator/consensus.py | 103 | `avg_confidence = total_conf / len(fresh)` |
| ORCH-003 | services/orchestrator/src/orchestrator/allocator.py | 17 | "Real Kelly-optimal sizing arrives in TASK-042" |
| RISK-001 | services/risk/src/risk/checks.py | 29-30 | "Reduce-and-allow is NOT implemented in v1" |
| RISK-002 | services/risk/src/risk/checks.py | 96 | `intent_notional = (intent_price * intent.quantity).copy_abs()` |
| OMS-001 | services/oms/src/oms/paper.py | 46-48 | Fixed 5 bps taker / 1 bps maker |
| OMS-001 | services/oms/src/oms/paper.py | 44-45 | 50 ms / 15 ms Gaussian latency defaults |
| OMS-002 | services/oms/src/oms/paper.py | 65-92 | `fill()` method |
| QF-001 | services/quant_foundry/src/quant_foundry/promotion.py | 226-229 | `if settled_count < self.min_settled_count` |
| QF-006 | services/quant_foundry/src/quant_foundry/promotion.py | 226-229 | (same) Time-decay missing |
| QF-002 | services/quant_foundry/src/quant_foundry/conformal_gate.py | n/a | One calibrator per model, no regime split |
| QF-003 | services/quant_foundry/src/quant_foundry/drift_sentinel.py | n/a | Enum recommendation (5 values) |
| QF-004 | services/quant_foundry/src/quant_foundry/moe_router.py | 9-10 | Comment promises learned router; not built |
| QF-005 | services/quant_foundry/src/quant_foundry/causal_graph.py | n/a | Snapshot-only; no streaming updater |
| QF-007 | services/quant_foundry/src/quant_foundry/paper_bridge.py | 56-57 | `allow_paper_bridge=False` default |
| QF-009 | services/quant_foundry/src/quant_foundry/budget.py | 107-196 | Binary allowed/refused decision |
| API-001 | services/api/src/api/main.py | 68 | `Redis.from_url(settings.REDIS_URL)` |
| API-001 | services/api/src/api/main.py | 78-80 | Quant Foundry poll + tournament tasks |
| API-002 | services/api/src/api/routes/quant_foundry.py | 5, 18, 258 | HMAC (not bearer) on callbacks |
| API-002 | services/api/src/api/routes/quant_foundry.py | 5 | `QUANT_FOUNDRY_ENABLED=false` default |
| API-004 | services/api/src/api/routes/data.py | n/a | `/data/coverage` 5 s timeout (REVIEW_2026-06-23) |
| API-005 | services/api/src/api/ws.py | n/a | Single WS channel |
| API-008 | services/api/src/api/main.py | 70-75 | Schedulers in-process |
| CORE-001 | libs/fincept-bus/src/fincept_bus/producer.py | 18-25 | Bus envelope without latency trace |
| CORE-002 | libs/fincept-core/src/fincept_core/schemas.py | n/a | Pydantic `extra='forbid'`, no schema_version |
| CORE-004 | libs/fincept-bus/src/fincept_bus/producer.py | 18-25 | Same as ING-005 |
| CORE-007 | libs/fincept-bus/src/fincept_bus/types.py | n/a | Bus types; no W3C propagation |
| CORE-008 | libs/fincept-tools/src/fincept_tools/registry.py | n/a | Function-pointer map; no audit wrapper |
| OBS-001 | scripts/paper_spine_replay.py | n/a | Replay script; not in CI |
| DESIGN | DESIGN.md | 5, 34 | OLED black floor |
| DESIGN | DESIGN.md | 54 | JetBrains Mono primary |
| DESIGN | DESIGN.md | 68 | 4 px base unit |

---

## 15. Closing Note

Fincept Terminal is **already well-architected** for a
paper-trading research platform. The Decimal discipline,
frozen Pydantic schemas, paper-only firewall, HMAC-signed
callbacks, and the close-the-loop Quant Foundry pieces are
the mark of a team that takes correctness seriously.

The biggest opportunity is **not** more infrastructure. It is
**causally connecting the parts that already exist** so that
an operator can sit down, look at the dashboard, and within
30 seconds know:

- what their portfolio is doing,
- why each position exists,
- whether the models are behaving, and
- what to do next.

That 30-second answer is the true cutting-edge product
surface. The tasks in §7 — executed in the order in §8, with
the discipline in §9, the gate in §2, and the protocol in §10
— are how you build it.
