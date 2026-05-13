# Fincept Terminal — System Overview & Forward Plan

> **Purpose:** ground-truth snapshot of every component currently in the repo, what it does, and how the next proof phase (paper-spine replay + operator contract smoke) threads into the existing pipeline.
> **Audience:** a new engineer landing on the project who needs to understand the whole stack in one read.
> **Last updated:** 2026-05-08, after agent layer expansion (7 agents), dashboard surface growth, ADR-0006/0009 resolution, ML lifecycle completion, and deterministic paper-spine replay receipt creation.

---

## 0. TL;DR

- **What this is.** A Python-first, Bet-A "research + execution terminal" (per `docs/ROADMAP.md`): live crypto + EOD-equity ingest → feature store → ML predictions → portfolio decisions → paper OMS → web dashboard.  No live capital, no Qt6, no FPGAs.
- **Where we are.** Foundation libs, data spine, agents, orchestrator, risk, paper OMS, portfolio service, REST API, strategy-host service, and Next.js dashboard are all implemented at package level.  The ML lifecycle covers train → walk-forward/holdout report → promote → hot-reload → predict → log → shadow.  Operator surfaces now include strategy config CRUD/lifecycle, manual orders, research/data provider tooling, model promotion/shadow controls, and datasource coverage.
- **What's next.** The paper-spine replay now has a deterministic local receipt via `uv run python scripts/paper_spine_replay.py`. The remaining highest-value proof is **route smoke + service-container replay**: a port-`8010` route smoke should prove dashboard/API contracts, then the replay should be repeated against live Redis/Timescale service wiring.
- **What's deliberately not built.** FIX, SIP, multi-monitor Qt UI, FPGA, kernel bypass, hierarchical meta-agents, online RL.  These are explicitly out-of-scope for the MVP track (`ROADMAP.md §1`).

---

## 1. Repo shape

```text
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
│   ├── jobs/                     Cron-style runners (daily EOD load, etc.)
│   └── strategy_host/            Filesystem-backed strategy instance supervisor
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

| Service    | Image                               | Role                                                                                     |
| ---------- | ----------------------------------- | ---------------------------------------------------------------------------------------- |
| `postgres` | `timescale/timescaledb:latest-pg16` | Bars, ticks, audit, training-run metadata.  Hypertables for time-series.                 |
| `redis`    | `redis:7-alpine` (AOF + RDB)        | Event bus (Streams) + heartbeats + leadership locks.                                     |
| `minio`    | `minio/minio:latest`                | S3-compatible object store for model artifacts, parquet feature dumps, backtest reports. |

Single dev command: `make dev` (or `scripts/dev-setup.ps1`) brings the whole stack up.

---

## 3. Foundation libraries (`libs/`)

These are dependency-free (other than each other) and are imported by every service.  They are the source of truth for schemas and bus semantics.

### `fincept-core`

The schema and runtime-primitive package.  Public modules:

- **`schemas.py`** — Pydantic v2 models for every event in the system: `Trade`, `OrderBook`, `Bar`, `FeatureRow`, `Prediction`, `RegimeSignal`, `SentimentSignal`, `Decision`, `OrderIntent`, `Fill`, `PositionSnapshot`, `RiskCheck`, `Alert`.
- **`events.py`** — `Event[T]` envelope (typed, includes `ts_event`, `ts_publish`, latency timestamps for the latency budget ledger).
- **`config.py`** — `Settings` (env-driven Pydantic settings; universe, redis URL, etc.).
- **`strategy_config.py`** — filesystem-backed strategy instance configs and append-only history under `strategies/`.
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

NewsAPI + LLM scoring per symbol.  Scores articles via Anthropic/OpenAI, emits `SentimentSignal` to `sig.sentiment`.  Skips startup unless `NEWSAPI_API_KEY` and an LLM key are configured.

#### `sentiment_features` *(optional)*

Bridges `SentimentSignal` events into the online feature store.  Consumes `sig.sentiment`, computes rolling sentiment features, publishes to `features.online`.

#### `information_enricher` *(optional)*

Consumes raw `InformationEvent` from `STREAM_INFO_RAW`, enriches with entity resolution and context, publishes to `STREAM_INFO_ENRICHED`.

#### `news_alpha_predictor` *(optional)*

ML predictor trained on news-alpha features.  Consumes `features.online`, runs inference, emits `Prediction` events.  Model artifacts stored at `models/news_alpha_predictor/`.

#### `news_outcome_labeler` *(optional)*

Consumes `STREAM_FEATURES_ONLINE` and `STREAM_MD_TRADES`, produces outcome labels for news events based on subsequent price movement.  Stores labels for training data generation.

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

### `strategy_host` — live strategy config runner

- **`main.py`** — service entrypoint, heartbeat name `strategy_host`.
- **`supervisor.py`** — watches persistent `StrategyConfig` records and starts/stops runners to match `enabled`.
- **`runner.py`** — instantiates registered strategy classes and emits paper order intents.

Strategy configs live under `strategies/<strategy_id>.json`; every write appends `strategies/<strategy_id>.history.jsonl`.  This survives Redis loss and gives operators a cat-able audit trail for strategy intent.

### `api` — REST + WebSocket gateway

FastAPI app (`api.main`) under `services/api/src/api/`.  Routes (`routes/`):

- **`models.py`** *(largest, ~28KB)* — model lifecycle: `GET /models`, `GET /models/{name}`, `POST /models/train`, `GET /models/runs`, `GET /models/runs/{run_id}`, `GET /models/promote/active` (with shadow), `POST /models/{name}/promote`, `POST /models/promote/rollback`, `POST /models/{name}/shadow`, `POST /models/promote/shadow/clear`, `GET /models/{name}/feature-importance`, `GET /models/{name}/predictions`, `GET /models/{name}/prediction-stats`.
- **`backtest.py`** — `POST /backtest/runs`, `GET /backtest/runs`, `GET /backtest/runs/{id}`.
- **`positions.py`** / **`orders.py`** — portfolio, OMS, and manual-order views.
- **`strategies.py`** — runtime strategy summary plus persistent strategy config CRUD/lifecycle/history: `/strategies/configs`, `/start`, `/stop`, `/history`.
- **`regime.py`** / **`news.py`** — read regime and news data.
- **`research.py`** — Exa and OpenBB read-only research endpoints, OpenBB health, allowlist, and Redis-backed rate limiting.
- **`news_impact.py`** — experimental news-impact lab endpoints; read-only/shadow posture.
- **`data.py`** — universe, symbol search, datasource registry, bars, and coverage.
- **`services.py`** — heartbeat aggregator + dependency-health view.
- **`control.py`** — kill switch and explicit operator control endpoints.

Cross-cutting:

- **`auth.py`** — `require_user` dependency (single-token bearer auth; replace before multi-tenant).
- **`ws.py`** — WebSocket fan-out for live position/P&L pushes.
- **`background.py`** — async training-run spawner (launched by `POST /models/train`, results polled by the dashboard).
- **`promotions.py`** — `PromotionStore` (active + shadow filesystem-backed bindings + history JSONL).
- **`training.py`** — `TrainingStore` (per-run metadata at `models/runs/<run_id>.json`).
- **`feature_importance.py`** — read & shape gain/split importance for the dashboard chart.

### Dashboard (`apps/dashboard`)

Next.js 14 (App Router) + TanStack Query + Tailwind + Radix UI primitives.  Pages under `src/app/`:

- **`/`** (`page.tsx`) — operator home: KPIs, recent alerts, active model, P&L sparkline.
- **`/markets`** — bar chart + symbol selector (TradingView Lightweight Charts).
- **`/models`** — list with active/shadow badges; per-model detail at `/models/[name]` with feature-importance chart, CV/holdout summary, live predictions card, promote + shadow buttons.
- **`/predictions`** — per-symbol live prediction stream.
- **`/backtest`** — run trigger + results browser.
- **`/positions`** — position book + per-symbol P&L.
- **`/orders`** — order/fill blotter.
- **`/risk`** — current limits + breaches.
- **`/strategies`** and **`/strategies/[id]`** — config CRUD, params, lifecycle toggles, model binding, and history.
- **`/portfolio-builder`** — portfolio optimizer / allocation scenario surface.
- **`/research`** — Exa/OpenBB research and provider proofs.
- **`/news`** and **`/news-lab`** — news surface and lab.
- **`/news-impact-lab`** — experimental news-impact workbench.
- **`/optimizer`** — portfolio optimization surface.
- **`/signal-cockpit-demo`** — signal visualization demo.
- **`/reconciliation`** — position reconciliation view.
- **`/login`** — bearer-token paste box.

Reusable components live under `components/widgets/` (KpiTile, EmptyState, PageHeader, …) and `components/models/` (PromoteButton, ShadowButton, PromotionHistoryPanel, RunsPanel, TrainModelDialog, LivePredictionsCard).  All API access goes through `lib/api.ts` with a typed `request<T>()` helper that surfaces `ApiError` (status + parsed body) for inline error toasts.

---

## 5. End-to-end data flow (steady state)

```text
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

| Phase                           | What landed                                                                                                                                                                                                                                                          | Key surface                                                                                                   |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **A — Train+register**          | `python -m agents.gbm_predictor.train --input data/X.parquet --cv-folds 5 --out-dir models/<name>` writes booster + meta + metrics + feature importance.  `POST /models/train` runs the same binary as a background task.                                            | `models/<name>/` artifacts; `models/runs/<run_id>.json` audit                                                 |
| **B — Listing + detail**        | `/models` lists every artifact; `/models/[name]` shows CV/holdout AUC, per-fold table, feature importance chart, training config provenance.                                                                                                                         | `GET /models`, `GET /models/{name}`, `GET /models/{name}/feature-importance`                                  |
| **C — Promote + rollback**      | `PromotionStore` writes `models/active/<agent_id>.json` (current) + `<agent_id>.history.jsonl` (timeline).  Rollback walks the history.  Validation: model.txt + meta.json must exist.                                                                               | `POST /models/{name}/promote`, `POST /models/promote/rollback`, `GET /models/promote/active`                  |
| **D1 — Hot-reload**             | gbm_predictor polls the active pointer every 30s; rebuilds the agent when the pointer changes; failed reload keeps the old agent serving.  No process restart needed.                                                                                                | Operator sees "hot-reload pending" countdown; existing tests cover pointer-change + reload-failure cases      |
| **D2 — Prediction outcome log** | Every prediction lands at `data/predictions/<agent_id>.jsonl` with the model name; api endpoints expose recent rows and aggregated stats per model.                                                                                                                  | `GET /models/{name}/predictions`, `GET /models/{name}/prediction-stats`; LivePredictionsCard on detail page   |
| **E1 — Shadow store + API**     | `PromotionStore` extended with `set_shadow / get_shadow / clear_shadow`; refuses `shadow == active`.  `GET /promote/active` includes shadow alongside active.                                                                                                        | `POST /models/{name}/shadow`, `POST /models/promote/shadow/clear`                                             |
| **E2 — Agent shadow loop**      | `gbm_predictor.main.run()` manages active + shadow slots independently.  Shadow `_shadow_loop()` records to JSONL but has **no producer** — defence-in-depth ensures shadow predictions cannot leak to `sig.predict`.  Failed shadow load is a warning, never fatal. | Hot-reload watcher handles the four transitions: None↔None, None→Path, Path→None, Path→Path'                  |
| **E3 — Dashboard**              | ShadowButton (set/clear with reload countdown), shadow badge on listing card (warn-tinted), shadow row in PromotionHistoryPanel ("recording predictions, not publishing").                                                                                           | `apps/dashboard/src/components/models/shadow-button.tsx`, listing `page.tsx`, promotion-history-panel updates |

**Test coverage today:** 190 api tests, 93 agent tests, dashboard typecheck clean.

---

## 7. Current next proof phase

The strategy host and strategy config UI/API surfaces now exist.  The next phase is not another scaffold; it is evidence that the current product works as one connected paper-trading system.

### Paper-spine replay fixture

#### Why now

Today every service has unit tests (190 api, 93 agents, ~40 backtester, dozens more).  But there is **no single regression that exercises the full data → fill → portfolio path** with a deterministic payload.  Every refactor of `Decision`, `OrderIntent`, or `Fill` schemas relies on careful local testing across every service.  A drift between, say, `Decision.target_notional` (Decimal) and OMS's interpretation (float) currently passes CI.

This replay is the missing pre-merge gate.

#### What gets built

#### Replay fixture

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

#### Audit trail reconstruction utility

`scripts/replay-audit.py <run_dir>` reads the captured events from a fakebus dump and renders a single chronological table:

```text
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

#### CI integration

- New job in `.github/workflows/ci.yml`: `make test-e2e` runs after `make test`.
- The job uses `services/api/tests/conftest.py`'s existing FakeRedis fixture (extended) so it runs in <30s.
- Failure output includes the diff between expected and actual JSONL — operator-readable.

#### Local replay utility

`make replay` (or `scripts/replay.ps1`) reproduces what CI runs but prints the audit table at the end so an engineer doing local development can see the spine flow live.

#### How this connects to the existing system *optimally*

- **No new runtime code paths.**  Phase G is purely test infrastructure.  It exercises *real* `main.run()` loops from each service against an in-process bus.
- **Catches drift early.**  Today a schema rename in `fincept-core/schemas.py` requires a coordinated release across 6 services.  After G, the replay test fails the moment any service serializes the new field but a downstream consumer reads the old one.
- **Self-documenting.**  The fixture JSONL files become the canonical example of "what does the spine look like for one bar?" — far more informative than prose docs.
- **Covers strategy-host behavior.**  The fixture should include at least one enabled strategy config and one disabled config, proving disabled strategies stay silent.
- **Foundation for future agents.**  A second predictive agent (regime, sentiment) gets added by appending to `expected_predictions.jsonl` — the rest of the spine doesn't change.  This is exactly the pattern needed when news-impact-model graduates from `experiments/` to `services/agents/`.

### Definition of done

- `make test-e2e` passes locally and in CI.
- A deliberate one-line break in `services/oms/processor.py` (e.g., flip qty sign) fails the e2e test with a readable diff, but does **not** fail any unit test.
- `scripts/replay-audit.py` renders a 8-row table identical to the example above.
- A route-smoke receipt records pass/fail/skip for port `8010` API surfaces.

---

## 8. Recommended sequence

1. Resolve API/dashboard contract drift (`venue` vs `venue_default`, coverage public errors, OpenBB health timeout).
2. Add the port-`8010` route smoke receipt.
3. Build paper-spine replay with strategy-host enabled/disabled configs.
4. Add CI/local command wrappers for the replay and smoke checks.
5. Only then expand autonomous research/agent behavior or live-brokerage assumptions.

---

## 9. What is deliberately **not** changing

To avoid scope creep on the next two phases, these items stay frozen:

- **Live execution.**  Everything stays in `paper` mode.  The Alpaca paper adapter (`oms/alpaca/`) is the closest we get to a real venue; live capital is gated behind the Phase 5 governance work in `ROADMAP.md`.
- **Schema evolution.**  No fields removed from `fincept-core/schemas.py` until the e2e replay covers them; the optional `strategy` field is an addition, not a breaking change.
- **Single-tenant auth.**  The bearer-token model in `api/auth.py` is unchanged.  Multi-tenant is a Phase 6 concern.
- **Notebook SDK surface.**  `fincept-sdk` doesn't gain new surface area in F or G.

---

## 10. Current open questions for the operator

1. Should disabled strategies record would-be decisions to a JSONL log, parallel to the shadow-prediction pattern?  Recommendation: **yes, opt-in** — it gives an operator the "what would this strategy have done?" view without spinning up a full backtest.
2. Position-aware vs target-aware orchestrator: still defer?  Recommendation: **yes, defer**.  Bring it up only after the e2e replay shows orders broadly filling against targets within an acceptable error band.
3. Should datasource/provider health become a first-class dashboard page or remain embedded in Markets/Research/Risk?  Recommendation: start embedded, then promote to a provider health center once smoke receipts exist.
4. Should route-smoke receipts live in `reports/`, `docs/`, or CI artifacts?  Recommendation: local dated receipts in `reports/route-smoke/`, with CI artifacts for automated runs.

---

## 11. Quick reference

```text
Build:           make dev      # docker stack + uv sync + pnpm install
Run all tests:   make test     # ruff + mypy + pytest
Per-package:     scripts/task-check.ps1 -PackagePath services/api -PytestPath services/api/tests
Bring services:  uv run --package agents python -m agents.gbm_predictor.main
Dashboard:       pnpm --filter dashboard dev
API:             uv run --package api uvicorn api.main:app --reload --port 8010
```

Repo URL: `https://github.com/AIRYDER/fincept`.  Local repo state has advanced beyond the older Phase E snapshot; verify current commit with `git log --oneline -1` before citing a commit hash in release notes.

## 12. Local Integration Notes — 2026-05-02

- The local API default is now `http://127.0.0.1:8010`; `scripts/start.ps1`, `status.ps1`, `stop.ps1`, and the dashboard API client should be treated as the canonical local port surface.
- Research endpoints are read-only by design: `/research/exa`, `/research/openbb/quote`, `/research/openbb`, `/research/openbb/health`, and `/research/openbb/health/history`.
- The OpenBB dispatcher is bounded by top-level namespace allowlists plus Redis-backed per-user rate limiting. Extend the allowlist deliberately instead of bypassing the route.
- The news-impact routes remain an experimental bridge to `experiments/news-impact-model`; they should produce shadow evidence before feeding orchestrator or OMS paths.
- Strategy configs now live as durable operator state through `StrategyConfigStore`; the next system proof should show config -> strategy-host -> order intent -> OMS -> portfolio, not only unit tests.
