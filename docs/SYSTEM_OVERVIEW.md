# Fincept Terminal — System Overview & Forward Plan

> **Purpose:** ground-truth snapshot of every component currently in the repo, what it does, and how the next two phases of work (F: strategy host, G: paper-spine replay) thread into the existing pipeline.
> **Audience:** a new engineer landing on the project who needs to understand the whole stack in one read.
> **Last updated:** 2026-04-30, after Phase E (shadow deployment) shipped.

---

## 0. TL;DR

- **What this is.** A Python-first, Bet-A "research + execution terminal" (per `docs/ROADMAP.md`): live crypto + EOD-equity ingest → feature store → ML predictions → portfolio decisions → paper OMS → web dashboard.  No live capital, no Qt6, no FPGAs.
- **Where we are.** Foundation libs, data spine, agents, orchestrator, paper OMS, portfolio service, REST API, and Next.js dashboard are all implemented at package level.  Phases A–E of the ML lifecycle are done end-to-end: train → register → CV/holdout report → promote → hot-reload → predict → log → shadow.
- **What's next.** **Phase F** (Strategy host service + strategy ↔ model binding UI): give operators a stable abstraction for "this strategy uses model X with regime weighting Y," so multiple strategies can drive the orchestrator independently.  **Phase G** (Paper-spine replay): a single deterministic fixture that flows one synthetic bar all the way from data → feature → prediction → decision → risk → order → fill → portfolio with a reconstructable audit trail, used as a pre-merge regression gate.
- **What's deliberately not built.** FIX, SIP, multi-monitor Qt UI, FPGA, kernel bypass, hierarchical meta-agents, online RL.  These are explicitly out-of-scope for the MVP track (`ROADMAP.md §1`).

---

## 1. Repo shape

```
fincept-terminal/
├── apps/
│   └── dashboard/                Next.js 14 web UI (operator console)
├── libs/                         Pure-Python shared packages (no service deps)
│   ├── fincept-core/             Schemas, events, config, clocks, IDs, prediction log
│   ├── fincept-bus/              Redis Streams producer/consumer with idempotency
│   ├── fincept-db/               Async SQLAlchemy + Alembic for Postgres/Timescale
│   ├── fincept-sdk/              Read-only Python SDK for notebooks / external scripts
│   └── fincept-tools/            Typed tool registry for LLM agents (audit-safe)
├── services/                     Long-running async Python processes
│   ├── ingestor/                 Crypto WS (Binance/Coinbase/Kraken) + EOD equity loader
│   ├── features/                 PIT feature computer + online (Redis) + offline (Parquet) stores
│   ├── agents/                   ML agents that consume features and emit predictions
│   ├── orchestrator/             Predictions → consensus → target notional → decisions/orders
│   ├── risk/                     Pre-trade checks + portfolio snapshot
│   ├── oms/                      Paper order processor (Alpaca paper adapter under oms/alpaca/)
│   ├── portfolio/                Position/P&L tracker driven off STREAM_FILLS
│   ├── api/                      FastAPI REST + WebSocket gateway for the dashboard
│   ├── backtester/               Event-driven historical replay + walk-forward CV
│   └── jobs/                     Cron-style runners (daily EOD load, etc.)
├── docs/                         Planning, blueprint, roadmap, ADRs, this file
├── spec/                         Contract-first task specs (BUILD_ORDER, CONTRACTS, etc.)
├── experiments/                  Out-of-tree research; news-impact-model lives here
├── docker-compose.yml            Local dev stack (Postgres+Timescale, Redis, MinIO)
├── Makefile                      `make dev`, `make ci`, etc. (POSIX/WSL)
└── scripts/*.ps1                 Windows equivalents (dev-setup, preflight, task-check)
```

Every Python service is a separate uv workspace package.  The dashboard is a single pnpm workspace package.  No service imports another service's source — they communicate over Redis Streams (events) and Postgres (state) only.

---

## 2. Infra primitives (`docker-compose.yml`)

| Service | Image | Role |
|---|---|---|
| `postgres` | `timescale/timescaledb:latest-pg16` | Bars, ticks, audit, training-run metadata.  Hypertables for time-series. |
| `redis` | `redis:7-alpine` (AOF + RDB) | Event bus (Streams) + heartbeats + leadership locks. |
| `minio` | `minio/minio:latest` | S3-compatible object store for model artifacts, parquet feature dumps, backtest reports. |

Single dev command: `make dev` (or `scripts/dev-setup.ps1`) brings the whole stack up.

---

## 3. Foundation libraries (`libs/`)

These are dependency-free (other than each other) and are imported by every service.  They are the source of truth for schemas and bus semantics.

### `fincept-core`

The schema and runtime-primitive package.  Public modules:

- **`schemas.py`** — Pydantic v2 models for every event in the system: `Trade`, `OrderBook`, `Bar`, `FeatureRow`, `Prediction`, `RegimeSignal`, `SentimentSignal`, `Decision`, `OrderIntent`, `Fill`, `PositionSnapshot`, `RiskCheck`, `Alert`.
- **`events.py`** — `Event[T]` envelope (typed, includes `ts_event`, `ts_publish`, latency timestamps for the latency budget ledger).
- **`config.py`** — `Settings` (env-driven Pydantic settings; universe, redis URL, etc.).
- **`clock.py`** / **`ids.py`** — monotonic ns clock + UUIDv7 generators.
- **`heartbeat.py`** / **`leadership.py`** — Redis-backed `beat_periodically` and "exactly-one-leader" locks so HA scale-out doesn't double-publish.
- **`logging.py`** / **`tracing.py`** — `configure_logging(structlog json)` + `configure_tracing(otel)`.
- **`portfolio.py`** — pure dataclasses for position/equity math, shared between portfolio service and backtester so they can't drift.
- **`prediction_log.py`** — JSONL append-only store at `data/predictions/<agent_id>.jsonl` with `append()`, `read()`, `stats()`.  Records every prediction with `(agent_id, model_name, ts_event, horizon_ns, symbol, direction, confidence)`.  **Phase D2** addition; both active and shadow loops write to the same store keyed by `model_name`.

### `fincept-bus`

Redis Streams wrapper.

- **`streams.py`** — canonical stream names + retention caps:
  - `md.trades`, `md.books`, `md.bars.1m` — market data
  - `sig.predict`, `sig.sentiment`, `sig.regime` — agent signals
  - `ord.decisions`, `ord.orders`, `ord.fills`, `ord.positions` — order lifecycle
  - `events.alerts` — operator-facing notifications
  - `features.online` — features fan-out (high-volume, 5M cap)
- **`producer.py`** — `Producer.publish(stream, event)`; idempotent on `event.id`.
- **`consumer.py`** — consumer-group reader with `XAUTOCLAIM` for stale pending messages, structured ack semantics, replay support.

### `fincept-db`

Async SQLAlchemy + Alembic for Postgres/Timescale.  ORM models for bars/ticks/audit; session helpers honoring the same `Settings`.  Used by `ingestor` (writer) and `backtester`/`features` (reader).

### `fincept-tools`

Typed-tool registry for LLM agents.  Every tool declares `name`, `input_schema`, `output_schema`, `side_effect_class` (`read | analyze | propose | paper_exec | live_exec`), and `audit_policy`.  Calls record `(caller_id, run_id, input_hash, output_hash, duration, side_effect_class)`.  Live-execution tools fail-closed.  Currently 81 unit tests pass.

### `fincept-sdk`

Thin read-only SDK for notebooks: query bars, predictions, model registry.  Wraps the api but bypasses HTTP when run in-process.

---

## 4. Service catalogue (`services/`)

Each service is an `async def main()` long-running process with signal handling, OTEL tracing, and a heartbeat loop.

### `ingestor` — market data spine

- **`binance.py` / `coinbase.py` / `kraken.py`** — per-venue WebSocket adapters with reconnect + gap detection (`quality.py`).
- **`normalizer.py`** — venue payload → canonical `Trade` / `OrderBook`.
- **`writer.py`** — batched async insert into Timescale hypertables.
- **`eod_equity.py`** — daily yfinance/polygon free-tier loader for equity bars.
- **`quality_main.py`** — separate process that emits `events.alerts` when ingestion drifts (latency, gap, cross-spread anomalies).

Output: `md.trades`, `md.books`, `md.bars.1m` populated; bars persisted to Postgres.

### `features` — point-in-time feature engineering

- **`pit.py`** — point-in-time correctness guard (rejects features computed after the bar's `ts_event`).
- **`computer.py`** — pure feature transforms (returns, vol, momentum).
- **`store.py`** — bridges to `online.py` (Redis hash, low-latency read) and `offline.py` (parquet at `data/features/`, large-batch read for training).
- **`main.py`** — long-running loop: subscribe to `md.bars.1m` → compute features → write online → append offline → publish `features.online`.

### `agents` — predictive layer

#### `gbm_predictor` *(production)*

The core LightGBM directional classifier.  Three sub-modules:

- **`train.py`** — CLI: train + cross-validate + holdout evaluate + write `models/<name>/{model.txt, meta.json, metrics.json, feature_importance.json}`.  Walk-forward and 80/20 holdout modes.
- **`infer.py`** — `GBMPredictor` agent: load booster + meta, subscribe to `features.online`, emit `Prediction` events.
- **`main.py`** — entrypoint with hot-reload + shadow-deployment support:
  - Polls `models/active/<agent_id>.json` every ~30s; on change builds the new agent, swaps it in, tears down the old one.  A failed reload keeps the previous model running.
  - Polls `models/active/<agent_id>.shadow.json` independently; spawns a parallel `_shadow_loop()` that records predictions to JSONL but **never publishes to Redis**.  Defence-in-depth: the shadow loop has no producer parameter, so no code path can leak shadow signals to the orchestrator.
- **`features.py`** — feature-name canonicalization shared with `train.py`.

#### `regime_agent` *(optional)*

Polls FRED (VIX, yield curve, fed funds), classifies macro regime via simple rule heuristics, publishes `RegimeSignal` to `sig.regime` only on change.  Skips startup if `FRED_API_KEY` is missing.

#### `sentiment_agent` *(optional)*

NewsAPI + Anthropic Messages API.  Scores articles per symbol, emits `SentimentSignal`.  Skips startup if `NEWSAPI_API_KEY` or `ANTHROPIC_API_KEY` is missing.  *Note: the heavier `experiments/news-impact-model` workbench is a research-only branch of this; not wired into the orchestrator.*

### `orchestrator` — predictions to orders

The heart of the trading loop.  Five modules:

- **`consensus.py`** — `ConsensusBuilder` keeps a per-symbol rolling window of recent `Prediction`s and returns `(direction, confidence)` only when enough fresh predictions agree.  Drops stale predictions on `ts_event` age.
- **`allocator.py`** — pure function `target_notional(direction, confidence, gross_cap_usd) -> Decimal`.  Linear in confidence, hard cap on notional per symbol.
- **`decisions.py`** — `TargetState` tracks last-emitted target per symbol; `build_decision_and_intent(symbol, delta_notional, ...)` produces the `Decision` (audit trail) and `OrderIntent` (consumed by OMS).  Deadband prevents churn on micro-deltas.
- **`router.py`** — async glue: subscribe to `sig.predict`, run consensus → allocator → deadband → publish `ord.decisions` + `ord.orders`.
- **`main.py`** — long-running entrypoint with leadership lock so only one orchestrator instance is active at a time.

The orchestrator is **target-portfolio-aware**, not position-aware: it tracks last-emitted target notional, which is good enough as long as orders broadly fill.  Reconciling against actual filled positions is deferred.

### `risk` — pre-trade guardrails

- **`checks.py`** — pre-trade rules: notional cap, per-symbol concentration, gross/net exposure, max DD breach, kill-switch flag.
- **`state.py`** — current portfolio snapshot read from Redis (cached; refreshed on `ord.fills`).
- **`snapshot.py`** — periodic re-publish to `events.alerts` when limits approach.

The risk service either *passes* an `OrderIntent` through to OMS or *rejects* it with a `RiskCheck` event explaining why.  **Phase G will wire this into the paper-spine replay** so a regression in any of these checks fails CI.

### `oms` — order management

- **`processor.py`** — main async loop: consume `ord.orders`, route to broker, publish `ord.fills`.
- **`paper.py`** — internal paper book; instant fills at last trade price.
- **`alpaca/`** — Alpaca paper-broker adapter (separate code path; behind `EXECUTION_MODE=alpaca_paper`).
- **`prices.py`** — last-trade lookup from Redis.
- **`state.py`** — order lifecycle state machine.

Live-execution paths fail-closed: any code that produces orders carries an `execution_mode` tag set to `paper` and the live tool registry is disabled.

### `portfolio` — book of record

- **`store.py`** — append-only fill log + position rollup.
- **`state.py`** — read-only `PositionSnapshot` for the dashboard / risk service.
- **`main.py`** — consume `ord.fills` → update positions → publish `ord.positions`.

### `backtester` — historical replay

The most-developed non-live service.  Eleven modules:

- **`engine.py`** — `BacktestEngine` orchestrates `BarsDataSource` → strategy callbacks → `SimBroker` → `Blotter`.
- **`datasource.py`** — `heapq.merge` of per-symbol bar streams; deterministic `ts_event` order.
- **`broker.py`** — fill-against-bar simulator (open/close/midpoint configurable).
- **`costs.py`** — spread + slippage + fee model with the `costs_v2` BPS-based variant in tests.
- **`blotter.py`** — fills + equity curve append-only store.
- **`strategies.py`** — reference strategies (MA crossover, mean reversion, momentum) and a `Strategy` ABC.
- **`runner.py`** — CLI: `python -m backtester.runner --strategy <name> --start ... --end ...`.
- **`walk_forward.py`** — walk-forward CV harness used by `gbm_predictor.train`.
- **`gbm_features.py`** — exact same feature recipe as the live `features` service, so backtest and live training cannot drift.
- **`ingest.py`** — load bars from Postgres or parquet snapshots.
- **`report.py`** — equity curve + drawdown + Sharpe rendered to JSON for the dashboard.

### `jobs` — cron

- **`daily_eod_load.py`** — kicks the EOD equity loader nightly.
- **`main.py`** — APScheduler-style runner.

### `api` — REST + WebSocket gateway

FastAPI app (`api.main`) under `services/api/src/api/`.  Routes (`routes/`):

- **`models.py`** *(largest, ~28KB)* — model lifecycle: `GET /models`, `GET /models/{name}`, `POST /models/train`, `GET /models/runs`, `GET /models/runs/{run_id}`, `GET /models/promote/active` (with shadow), `POST /models/{name}/promote`, `POST /models/promote/rollback`, `POST /models/{name}/shadow`, `POST /models/promote/shadow/clear`, `GET /models/{name}/feature-importance`, `GET /models/{name}/predictions`, `GET /models/{name}/prediction-stats`.
- **`backtest.py`** — `POST /backtest/runs`, `GET /backtest/runs`, `GET /backtest/runs/{id}`.
- **`positions.py`** / **`orders.py`** — read-only views into portfolio + OMS state.
- **`strategies.py`** — currently a placeholder enum (Phase F replaces this).
- **`regime.py`** / **`news.py`** — read regime + news-impact data.
- **`data.py`** — bar query for the chart widget.
- **`control.py`** — start/stop a strategy (placeholder; will become Phase F's surface).
- **`services.py`** — heartbeat aggregator + dependency-health view.

Cross-cutting:

- **`auth.py`** — `require_user` dependency (single-token bearer auth; replace before multi-tenant).
- **`ws.py`** — WebSocket fan-out for live position/P&L pushes.
- **`background.py`** — async training-run spawner (launched by `POST /models/train`, results polled by the dashboard).
- **`promotions.py`** — `PromotionStore` (active + shadow filesystem-backed bindings + history JSONL).
- **`training.py`** — `TrainingStore` (per-run metadata at `models/runs/<run_id>.json`).
- **`feature_importance.py`** — read & shape gain/split importance for the dashboard chart.

### Dashboard (`apps/dashboard`)

Next.js 14 (App Router) + TanStack Query + Tailwind + shadcn/ui.  Pages under `src/app/`:

- **`/`** (`page.tsx`) — operator home: KPIs, recent alerts, active model, P&L sparkline.
- **`/markets`** — bar chart + symbol selector (TradingView Lightweight Charts).
- **`/models`** — list with active/shadow badges; per-model detail at `/models/[name]` with feature-importance chart, CV/holdout summary, live predictions card, promote + shadow buttons.
- **`/predictions`** — per-symbol live prediction stream.
- **`/backtest`** — run trigger + results browser.
- **`/positions`** — position book + per-symbol P&L.
- **`/orders`** — order/fill blotter.
- **`/risk`** — current limits + breaches.
- **`/strategies`** — placeholder (Phase F target).
- **`/news`** — news-impact research surface.
- **`/login`** — bearer-token paste box.

Reusable components live under `components/widgets/` (KpiTile, EmptyState, PageHeader, …) and `components/models/` (PromoteButton, ShadowButton, PromotionHistoryPanel, RunsPanel, TrainModelDialog, LivePredictionsCard).  All API access goes through `lib/api.ts` with a typed `request<T>()` helper that surfaces `ApiError` (status + parsed body) for inline error toasts.

---

## 5. End-to-end data flow (steady state)

```
            +-----------+      bars       +-----------+
            | Binance   |---------------->|           |
            | Coinbase  |                 | ingestor  |
            | Kraken    |                 |           |
            +-----------+                 +-----+-----+
                                                |
                                  md.trades / md.books / md.bars.1m
                                                v
                                         +-------------+
                                         |  features   | --> features.online (Redis hash)
                                         +------+------+ --> data/features/*.parquet (offline)
                                                |
                                  features.online
                                                v
                                         +-------------+
                                         | gbm_predictor| -active->  sig.predict  ----+
                                         |  (active)   | -shadow-> data/predictions/  |
                                         |  + shadow   |    (NEVER to sig.predict)    |
                                         +-------------+                              |
                                                                                       |
              +-----------+   sig.regime    +--------------+                          |
              |  regime   |---------------->|              |                          |
              |  agent    |                 |              |                          |
              +-----------+                 |              |<--------------------------+
              +-----------+   sig.sentiment | orchestrator |
              | sentiment |---------------->|  (consensus  |
              |  agent    |                 |  + allocator)|
              +-----------+                 +------+-------+
                                                   |
                                  ord.decisions (audit) + ord.orders
                                                   v
                                            +------+------+      ord.orders     +-----+
                                            |    risk     |------------------->| oms |
                                            | (pre-trade) |  (or RiskCheck    +--+--+
                                            +-------------+   reject)            |
                                                                                  | ord.fills
                                                                                  v
                                                                          +-----------+
                                                                          | portfolio |
                                                                          +-----+-----+
                                                                                |
                                                                  ord.positions / WebSocket
                                                                                v
                                                                         +-----------+
                                                                         | dashboard |
                                                                         +-----------+
```

Every arrow is a Redis Stream except the dotted file-system writes (model artifacts, prediction JSONL).  Every event carries `ts_event` (source clock), `ts_publish` (bus arrival), and a `latency_*_ns` ledger that the api can roll up for the operator-facing latency dashboard.

---

## 6. ML lifecycle (Phases A → E, all shipped)

Recap of what runs end-to-end today:

| Phase | What landed | Key surface |
|---|---|---|
| **A — Train+register** | `python -m agents.gbm_predictor.train --input data/X.parquet --cv-folds 5 --out-dir models/<name>` writes booster + meta + metrics + feature importance.  `POST /models/train` runs the same binary as a background task. | `models/<name>/` artifacts; `models/runs/<run_id>.json` audit |
| **B — Listing + detail** | `/models` lists every artifact; `/models/[name]` shows CV/holdout AUC, per-fold table, feature importance chart, training config provenance. | `GET /models`, `GET /models/{name}`, `GET /models/{name}/feature-importance` |
| **C — Promote + rollback** | `PromotionStore` writes `models/active/<agent_id>.json` (current) + `<agent_id>.history.jsonl` (timeline).  Rollback walks the history.  Validation: model.txt + meta.json must exist. | `POST /models/{name}/promote`, `POST /models/promote/rollback`, `GET /models/promote/active` |
| **D1 — Hot-reload** | gbm_predictor polls the active pointer every 30s; rebuilds the agent when the pointer changes; failed reload keeps the old agent serving.  No process restart needed. | Operator sees "hot-reload pending" countdown; existing tests cover pointer-change + reload-failure cases |
| **D2 — Prediction outcome log** | Every prediction lands at `data/predictions/<agent_id>.jsonl` with the model name; api endpoints expose recent rows and aggregated stats per model. | `GET /models/{name}/predictions`, `GET /models/{name}/prediction-stats`; LivePredictionsCard on detail page |
| **E1 — Shadow store + API** | `PromotionStore` extended with `set_shadow / get_shadow / clear_shadow`; refuses `shadow == active`.  `GET /promote/active` includes shadow alongside active. | `POST /models/{name}/shadow`, `POST /models/promote/shadow/clear` |
| **E2 — Agent shadow loop** | `gbm_predictor.main.run()` manages active + shadow slots independently.  Shadow `_shadow_loop()` records to JSONL but has **no producer** — defence-in-depth ensures shadow predictions cannot leak to `sig.predict`.  Failed shadow load is a warning, never fatal. | Hot-reload watcher handles the four transitions: None↔None, None→Path, Path→None, Path→Path' |
| **E3 — Dashboard** | ShadowButton (set/clear with reload countdown), shadow badge on listing card (warn-tinted), shadow row in PromotionHistoryPanel ("recording predictions, not publishing"). | `apps/dashboard/src/components/models/shadow-button.tsx`, listing `page.tsx`, promotion-history-panel updates |

**Test coverage today:** 190 api tests, 93 agent tests, dashboard typecheck clean.

---

## 7. What's next — Phase F (Strategy host) & Phase G (Paper-spine replay)

These are the two pending items in the ML lifecycle backlog.  Together they close the loop from "we have models" to "we have governable strategies that are continuously regression-tested."

### Phase F — Strategy host service + binding UI

#### Why now

Today the orchestrator hard-wires a single agent (`gbm_predictor.v1`) to a single output stream, with consensus and allocator parameters baked into env vars.  This is fine for one model but blocks three operator workflows:

1. **Multiple strategies on one orchestrator.**  An operator wants `momentum-fast` (5-minute horizon, low gross cap) and `mean-revert-slow` (1-hour horizon, higher cap) to run side-by-side without redeploying.
2. **Per-strategy governance.**  Risk wants kill-switch per strategy, not per agent.  The dashboard wants P&L attribution per strategy.
3. **Promotion at strategy level, not agent level.**  Today promotion is "swap the agent's active model."  Tomorrow it should be "swap which model strategy `momentum-fast` consumes."

#### What gets built

**F1 — `services/strategies/`** *(new service)*

```
services/strategies/
├── pyproject.toml
├── src/strategies/
│   ├── __init__.py
│   ├── store.py          Filesystem store for Strategy definitions (mirrors PromotionStore)
│   ├── runner.py         Per-strategy async loop: subscribe -> consensus -> allocate -> publish
│   ├── manifest.py       StrategyDefinition dataclass (name, agent_ids, allocator_cfg, risk_cfg)
│   └── main.py           Multiplexer: load N strategy manifests, run each in its own task
└── tests/
```

A `StrategyDefinition` is a small JSON document at `strategies/<name>.json`:

```jsonc
{
  "name": "momentum-fast",
  "version": 1,
  "consumes_signals": ["sig.predict"],
  "agent_filter": ["gbm_predictor.v1"],
  "regime_weights": {"risk_on": 1.0, "risk_off": 0.3, "neutral": 0.7},
  "allocator": {"gross_cap_usd": 50000, "min_delta_usd": 250, "horizon_ns": 300_000_000_000},
  "risk_overrides": {"max_concentration_pct": 10},
  "execution_mode": "paper",
  "enabled": true
}
```

The strategy runner is the orchestrator's logic factored out: each strategy gets its own `ConsensusBuilder` + `TargetState` + allocator, all reading the **same** `sig.predict` stream but filtering on `agent_id`.  Decisions/orders carry a new `strategy=<name>` tag so the OMS, portfolio, and risk services can attribute correctly.

**F2 — Orchestrator becomes a strategy host**

The current `OrchestratorRouter` is renamed `LegacySingleStrategyRouter` and kept for backward compatibility.  The new `main.py` reads `strategies/*.json` and spawns one runner per enabled strategy.  Hot-reload: same 30s polling pattern as the gbm_predictor active/shadow watcher.

**F3 — API + dashboard surface**

- New routes (extend `routes/strategies.py`, currently a placeholder):
  - `GET /strategies` — list with per-strategy state (enabled, current target notional, last decision time, P&L delta).
  - `GET /strategies/{name}` — full manifest + binding history.
  - `POST /strategies/{name}` — upsert manifest (with the same anti-traversal + schema-validation pattern as PromotionStore).
  - `POST /strategies/{name}/enable` / `/disable` — kill-switch per strategy.
  - `POST /strategies/{name}/bind-model` — declarative wrapper around `POST /models/{name}/promote` that also updates the strategy's `agent_filter` if the agent_id changed.

- New dashboard pages:
  - `/strategies` — list with KPIs (enabled, P&L 24h, last-decision-age).
  - `/strategies/[name]` — manifest editor (JSON or form), binding history, per-strategy P&L chart, enable/disable toggle.

**F4 — Risk + portfolio attribution**

Risk and portfolio services already consume `ord.orders` / `ord.fills`.  Both are extended to key their internal state by `(symbol, strategy)` in addition to `symbol`.  The dashboard's existing `/positions` page stays symbol-centric; a new `/strategies/[name]` view is per-strategy.

#### How this connects to the existing system *optimally*

- **No change to agents.**  `gbm_predictor` keeps emitting `Prediction` events to `sig.predict`.  Strategies *filter* on `agent_id` rather than the agent service publishing to a strategy-specific stream — this keeps agents single-responsibility (predict, don't route).
- **No change to OMS or risk core logic.**  They just see a new optional `strategy` field on every order/fill/risk-check event.  Backward compat: old events without the field are treated as `strategy="legacy"`.
- **Promotion semantics generalize cleanly.**  The shadow slot already proved the "two parallel pipelines, only one publishes" pattern.  Strategy enable/disable is the same idea at a coarser granularity: `enabled=false` means "run the consensus but don't publish decisions."  Same defence-in-depth: the disabled-strategy code path doesn't construct a producer.
- **Dashboard reuses existing widgets.**  PromoteButton, ShadowButton, PromotionHistoryPanel are model-level today; they get a thin adapter `<StrategyManifestEditor />` and a per-strategy variant of `<PromotionHistoryPanel />`.
- **Single new stream optional.**  The simplest implementation publishes decisions to the existing `ord.decisions` / `ord.orders` streams with the new `strategy` field.  A future refinement could fan out to per-strategy streams (`ord.decisions.momentum-fast`) for finer-grained backpressure, but that's a Phase H concern.

#### Definition of done for Phase F

- A single new file `strategies/momentum-fast.json` plus a one-time `make strategies` reload makes the orchestrator drive the OMS for that strategy alone.
- Dashboard `/strategies` shows the strategy as enabled with non-zero P&L delta after a paper-trading window.
- Test coverage: store CRUD + validation, runner consensus/allocator math, end-to-end "set up two strategies, expect two distinct decisions per prediction window."

#### Estimated scope

3 working days for backend (store + runner + routes + tests); 2 days for dashboard; 1 day for risk/portfolio attribution wiring.  Comparable to the Phase E shadow work in size.

---

### Phase G — End-to-end paper-spine replay fixture

#### Why now

Today every service has unit tests (190 api, 93 agents, ~40 backtester, dozens more).  But there is **no single regression that exercises the full data → fill → portfolio path** with a deterministic payload.  Every refactor of `Decision`, `OrderIntent`, or `Fill` schemas relies on careful local testing across every service.  A drift between, say, `Decision.target_notional` (Decimal) and OMS's interpretation (float) currently passes CI.

Phase G is the missing pre-merge gate.

#### What gets built

**G1 — A single `tests/e2e/test_paper_spine_replay.py` (in `services/api/tests/` or a new top-level `tests/e2e/`)**

The fixture:

1. Starts an in-process FakeRedis + a temporary SQLite (or fakedb) substitute for Timescale.
2. Loads a checked-in fixture: `tests/e2e/fixtures/spine_replay/`
   - `bars.jsonl` — 20 bars across 2 symbols, deterministic ts_event.
   - `expected_predictions.jsonl` — what `gbm_predictor` should emit when fed those bars through `features`.
   - `expected_decisions.jsonl` — what the orchestrator should emit given those predictions.
   - `expected_orders.jsonl`, `expected_fills.jsonl`, `expected_positions.jsonl` — downstream expectations.
   - `model.txt`, `meta.json` — a tiny pre-trained LightGBM booster so inference is real, not mocked.
3. Spawns each service's main loop as an asyncio task wired against the fake bus.
4. Publishes `bars.jsonl` to `md.bars.1m`.
5. Waits up to N seconds for `ord.positions` to settle.
6. Asserts every published event matches the expected JSONL file *byte-for-byte after key normalization* (`event.id` random, `ts_publish` monotonic — but `ts_event`, `symbol`, `side`, `qty`, `target_notional`, `latency_ledger` are all deterministic).

**G2 — Audit trail reconstruction utility**

`scripts/replay-audit.py <run_dir>` reads the captured events from a fakebus dump and renders a single chronological table:

```
ts_event_ns         service       event_type     payload (key fields)
1714502400000000000 ingestor      Trade          BTC-USD 60123.45 0.5
1714502460000000000 features      FeatureRow     BTC-USD ret_5m=+0.012 ...
1714502460500000000 gbm_predictor Prediction     BTC-USD long conf=0.62 model=v17
1714502461000000000 orchestrator  Decision       BTC-USD target=+10000 strategy=mf
1714502461100000000 orchestrator  OrderIntent    BTC-USD buy 0.166 limit=60125
1714502461200000000 risk          RiskCheck      pass: notional=10000 conc=8.3pct
1714502461300000000 oms           Fill           BTC-USD bought 0.166 @ 60128
1714502461400000000 portfolio     PositionSnap   BTC-USD qty=0.166 unrealized=-0.5
```

This is the deliverable for the **P0 "End-to-end paper spine replay"** item from `ROADMAP.md §12`.

**G3 — CI integration**

- New job in `.github/workflows/ci.yml`: `make test-e2e` runs after `make test`.
- The job uses `services/api/tests/conftest.py`'s existing FakeRedis fixture (extended) so it runs in <30s.
- Failure output includes the diff between expected and actual JSONL — operator-readable.

**G4 — Local replay utility**

`make replay` (or `scripts/replay.ps1`) reproduces what CI runs but prints the audit table at the end so an engineer doing local development can see the spine flow live.

#### How this connects to the existing system *optimally*

- **No new runtime code paths.**  Phase G is purely test infrastructure.  It exercises *real* `main.run()` loops from each service against an in-process bus.
- **Catches drift early.**  Today a schema rename in `fincept-core/schemas.py` requires a coordinated release across 6 services.  After G, the replay test fails the moment any service serializes the new field but a downstream consumer reads the old one.
- **Self-documenting.**  The fixture JSONL files become the canonical example of "what does the spine look like for one bar?" — far more informative than prose docs.
- **Compatible with Phase F.**  Once F lands, the fixture is extended with two strategy manifests (one enabled, one disabled), and the expected `ord.decisions.jsonl` includes the `strategy` field.  This validates F's defence-in-depth ("disabled strategy doesn't publish") at the spine level.
- **Foundation for future agents.**  A second predictive agent (regime, sentiment) gets added by appending to `expected_predictions.jsonl` — the rest of the spine doesn't change.  This is exactly the pattern needed when news-impact-model graduates from `experiments/` to `services/agents/`.

#### Definition of done for Phase G

- `make test-e2e` passes locally and in CI.
- A deliberate one-line break in `services/oms/processor.py` (e.g., flip qty sign) fails the e2e test with a readable diff, but does **not** fail any unit test.
- `scripts/replay-audit.py` renders a 8-row table identical to the example above.
- `docs/SYSTEM_OVERVIEW.md` (this file) gets a "see `make replay` for live demo" link added.

#### Estimated scope

2 days for fixture authoring + main.run() reuse work; 1 day for audit utility; 0.5 days for CI wiring.  Smaller than F.

---

## 8. Combined sequence

The two phases compose naturally if executed in this order:

1. **Phase F first** so strategies become the unit of governance.
2. **Phase G second** so the resulting two-strategy spine is the regression target.

Doing G first would mean re-authoring the fixture once F lands.  Doing F first means G's fixture covers the future state from day one.

Risk: F is the larger and riskier of the two.  Mitigation: ship F1 + F2 (backend) before F3 (UI) so the spine is governable from the CLI even if the dashboard work slips.

---

## 9. What is deliberately **not** changing

To avoid scope creep on the next two phases, these items stay frozen:

- **Live execution.**  Everything stays in `paper` mode.  The Alpaca paper adapter (`oms/alpaca/`) is the closest we get to a real venue; live capital is gated behind the Phase 5 governance work in `ROADMAP.md`.
- **Schema evolution.**  No fields removed from `fincept-core/schemas.py` until the e2e replay covers them; the optional `strategy` field is an addition, not a breaking change.
- **Single-tenant auth.**  The bearer-token model in `api/auth.py` is unchanged.  Multi-tenant is a Phase 6 concern.
- **Notebook SDK surface.**  `fincept-sdk` doesn't gain new surface area in F or G.

---

## 10. Open questions for the operator before F kicks off

1. Should two strategies share a `ConsensusBuilder` (cheaper, but coupled) or each have their own (clean, more memory)?  Recommendation: **separate** — the memory cost is trivial and isolation is easier to reason about.
2. Should the strategy manifest live on disk (`strategies/<name>.json`, like promotions) or in Postgres?  Recommendation: **disk** — same reasoning as `promotions.py`'s docstring; operator-readable, `cat`-able, no schema migration.
3. Should disabled strategies still record their would-be decisions to a JSONL log (parallel to the shadow-prediction pattern)?  Recommendation: **yes, opt-in** — gives an operator the "what would this strategy have done?" view without spinning up a full backtest.
4. Position-aware vs target-aware orchestrator: still defer?  Recommendation: **yes, defer**.  Bring it up only after Phase G's e2e replay shows orders broadly filling against targets within an acceptable error band.

---

## 11. Quick reference

```
Build:           make dev      # docker stack + uv sync + pnpm install
Run all tests:   make test     # ruff + mypy + pytest
Per-package:     scripts/task-check.ps1 -PackagePath services/api -PytestPath services/api/tests
Bring services:  uv run --package agents python -m agents.gbm_predictor.main
Dashboard:       pnpm --filter dashboard dev
API:             uv run --package api uvicorn api.main:app --reload
```

Repo URL: `https://github.com/AIRYDER/fincept`.  Latest commit on `main` is `641478d` (Phase E shadow deployment).
