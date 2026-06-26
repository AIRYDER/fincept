# Audit: services/ (Core Backend Services)

## Executive Summary

`services/` contains the five core backend services that form the runtime
backbone of the fincept-terminal platform: `api` (FastAPI HTTP/WebSocket
gateway), `orchestrator` (prediction-to-order pipeline), `jobs` (APScheduler
batch runner), `ingestor` (venue WebSocket adapters + market-data writer), and
`features` (online + offline feature engineering with PIT-correct joins).

The architectural layering is clean: all services depend on the shared
`libs/` packages (`fincept-core`, `fincept-bus`, `fincept-db`,
`fincept-sdk`, `fincept-tools`) and never import from each other directly
except through Redis Streams. The event spine is the canonical contract:
services communicate exclusively via typed `Event` envelopes on named
streams, which makes the system observably decoupled and testable in
isolation.

Overall quality is **high**. The codebase demonstrates strong engineering
discipline: every module opens with a detailed docstring explaining the
"why" behind design decisions, dependency injection is used consistently
to keep modules testable without I/O, Decimal is used for all money
quantities, PIT correctness is enforced with a test-pinned invariant, and
the bit-identical online/offline feature guarantee is explicitly
architected through a shared compute kernel. Test coverage is broad —
every service has meaningful unit tests that use fakes/injections rather
than hitting real Redis or databases.

That said, the audit found a number of real issues:

- **Bugs / correctness**: `main.py` calls `assert_safe_for_runtime()` at
  module import time (line 63) which will crash on import in test
  contexts; the `news_impact.py` route mutates `sys.path` at import time
  (line 31) which is fragile and not thread-safe; `control.py` script
  path resolution walks up `__file__.parents` and raises `HTTPException`
  from a non-route helper (line 63), conflating framework exception
  contexts.
- **Design smells**: `api/main.py` lifespan is 185 lines with 6
  manually-managed background tasks — each with its own cancel/await
  boilerplate — making it one of the most complex lifespan functions in
  the codebase; the `QualityMonitor` in `ingestor/quality.py` tracks
  `_last_top` only on snapshots, meaning cross-venue spread detection is
  structurally Coinbase↔Kraken only (Binance never emits snapshots) —
  documented but a scaling limitation; `CrossFeatures` in
  `features/transforms/cross.py` uses position-based deque alignment
  rather than timestamp alignment, which can misalign beta/correlation
  when venues have different bar arrival cadences.
- **Scaling gaps**: `ingestor/main.py` runs one venue per process with
  no supervisor for multi-venue fan-out; `writer.py` buffers trades/books
  in memory with no backpressure — a slow DB can cause unbounded buffer
  growth; `news.py` `_load_articles` does a Redis pipeline GET per
  article ID (up to 200 round-trips in the pipeline batch); the
  `backtest.py` route holds a global `asyncio.Lock` that serializes all
  backtest runs across all users.
- **Test gaps**: `ingestor/quality_main.py` has no tests (standalone
  entrypoint); `features/main.py` has no tests (entrypoint only);
  `orchestrator/main.py` has no direct tests (the integration is tested
  via `test_regime_integration.py` and `test_sentiment_integration.py`
  but not the main loop itself); `api/settlements_poller.py` has tests
  but the lazy import path (`_build_market_data_source`) is not
  exercised.

None of these are show-stoppers for the current paper-trading scope, but
several will matter at production volume. They are itemised per-service
below with file paths and line numbers.

---

## Service: api

### Purpose

The `api` service is the HTTP/WebSocket gateway for the fincept-terminal
dashboard and operator tools. It exposes REST endpoints for market data,
positions, orders, strategies, news, models, backtesting, research,
regime, health/readiness, and the Quant Foundry gateway. A WebSocket
multiplexer streams live events (positions, fills, predictions, alerts)
to dashboard clients. Background schedulers sync Alpaca positions/marks
and news into Redis so read paths are sub-millisecond.

### Layout

```
services/api/
  src/api/
    __init__.py
    main.py                    # FastAPI app construction + lifespan
    auth.py                    # JWT bearer authentication
    approved_roots.py          # Filesystem path-safety dependency
    background.py              # AlpacaScheduler + NewsScheduler
    deps.py                    # FastAPI dependency providers
    rate_limit.py              # Redis fixed-window rate limiter
    ws.py                      # WebSocket multiplexer over Redis Streams
    settlements_poller.py      # Settlements worker bridge
    promotions.py              # Filesystem-backed agent→model bindings
    feature_importance.py      # LightGBM model.txt parser (no lightgbm dep)
    openbb_health_store.py     # Redis-backed OpenBB health probe store
    symbol_search.py           # Typeahead matcher for symbol input UI
    training.py                # Filesystem-backed training-runs registry
    routes/
      __init__.py
      backtest.py              # POST /backtest/run, GET /backtest/runs
      control.py               # Kill-switch + feature service control
      data.py                  # Universe, symbol search, bars, sources
      health.py                # /health/readiness (detailed system state)
      models.py                # Per-model status, CV provenance, outcomes
      modules.py               # On-demand module control (TASK-0203)
      news.py                  # Book-aware news feed with priority scoring
      news_impact.py           # Experimental news impact model bridge
      orders.py                # Manual order submission + list endpoint
      positions.py             # Live position read endpoints
      quant_foundry.py         # Quant Foundry gateway (TASK-0306)
      quant_foundry_alpha.py   # Alpha Genome Lab recipe sweep (TASK-1005)
      regime.py                # Latest macro regime + classifier inputs
      research.py              # OpenBB + Exa research tool dispatchers
      services.py              # Per-service heartbeat status
      strategies.py            # Strategy registry + config CRUD + lifecycle
  tests/
    conftest.py                # Shared fixtures (fakeredis, auth token)
    test_auth.py               # JWT encode/decode tests
    test_backtest.py           # Backtest run/list/get tests
    test_control.py            # Kill-switch + feature control tests
    test_data.py               # Data route tests
    test_feature_importance.py # model.txt parser tests
    test_health.py             # Readiness endpoint tests
    test_models.py             # Model status + CV provenance tests
    test_models_outcomes.py    # Model outcomes route tests
    test_models_train.py       # Training route tests
    test_modules.py            # Module control tests
    test_news.py               # News feed tests
    test_news_impact.py        # News impact model tests
    test_orders.py             # Order submission/list tests
    test_positions.py          # Position read tests
    test_predictions.py        # Prediction log tests
    test_promotion_endpoints.py# Promotion route tests
    test_promotions.py         # Promotion store tests
    test_provider_data.py      # Provider data record tests
    test_provider_evidence.py  # Provider evidence tests
    test_quant_foundry.py      # Quant Foundry gateway tests
    test_quant_foundry_budget.py
    test_quant_foundry_dossiers.py
    test_quant_foundry_shadow.py
    test_quant_foundry_startup.py
    test_rate_limit.py         # Rate limiter tests
    test_regime.py             # Regime route tests
    test_research.py           # Research dispatcher tests
    test_settlements_poller.py # Settlements worker tests
    test_strategies.py         # Strategy config CRUD tests
    test_symbol_search.py      # Symbol search tests
    test_training.py           # Training store tests
```

### How Each Module Works

#### `main.py` (353 lines)

The FastAPI app is constructed at module level (line 296) with a
`lifespan` async context manager (line 67) that:

1. Configures logging + tracing.
2. Opens a shared Redis client on `app.state.redis`.
3. Starts `AlpacaScheduler` (positions/marks sync, 60s interval) and
   `NewsScheduler` (news + bar snapshots, 30s interval).
4. Starts a heartbeat task (`beat_periodically(redis, "api")`).
5. Conditionally starts up to 4 Quant Foundry background poll tasks
   (runpod results, tournament sweep, settlement sweep, shadow dispatch)
   based on gateway mode + env-var intervals.
6. Conditionally starts the settlements worker task
   (`_poll_settlements_worker`).

The shutdown path (line 143) manually cancels and awaits each task in
reverse order — 6 tasks, each with its own try/except CancelledError
block. This is correct but verbose.

**Issue (P2):** `assert_safe_for_runtime()` is called at module import
time (line 63), outside the lifespan. This means importing `api.main` in
a test or tool context will raise if `JWT_SECRET` is the dev default.
The function is also called inside `lifespan` (line 71) and in
`orchestrator.main._main` and `features.main.run`, which is the correct
place for it. The module-level call is redundant and harmful.

**Issue (P3):** The lifespan function is 185 lines long with significant
duplication in the shutdown path. Each background task follows the same
pattern: `task = asyncio.create_task(...)`, `app.state.X_task = task`,
then in shutdown: `task.cancel()`, `try: await task except
CancelledError: pass`. A helper or a task-group abstraction would halve
the line count and reduce the risk of forgetting a task.

#### `auth.py` (61 lines)

Simple HS256 JWT bearer authentication. `require_user` is a FastAPI
dependency that parses the `Authorization` header, verifies the token
against `Settings.JWT_SECRET`, and returns the decoded claims. The dev
default secret is intentionally unsafe; `assert_safe_for_runtime` is the
guardrail.

Well-implemented: clean error messages, proper `WWW-Authenticate`
header on 401, no secret leakage in error details.

#### `deps.py` (56 lines)

Three dependency providers: `get_redis` (from `app.state.redis`),
`get_position_store` (wraps Redis in `PositionStore`), and
`get_strategy_config_store` (filesystem-backed singleton). All are
designed for test override via `app.dependency_overrides`.

#### `rate_limit.py` (103 lines)

Redis-backed fixed-window rate limiter using `INCR` + `EXPIRE`. Returns
a `RateLimitState` dataclass with `count`, `remaining`, `reset_sec`.
Raises `RateLimitExceeded` (carrying `retry_after`) when over budget.

Well-implemented: handles the edge case where TTL is missing (repairs
it), uses `max(ttl, 1)` to avoid zero retry-after, and the `INCR` +
`EXPIRE`-on-first pattern is the standard Redis fixed-window idiom.

#### `ws.py` (159 lines)

WebSocket multiplexer over Redis Streams. Clients subscribe to topics
(positions, fills, predictions, alerts) by sending a JSON frame. The
server uses `redis.xread` with a 1s block timeout to watch all
subscribed streams. No consumer groups — WebSockets are transient
broadcast.

Authentication supports both `Authorization` header and `?token=`
query string (browser WebSocket limitation). Unknown topics are silently
dropped.

**Note:** The `$` (live tail) start position means reconnecting clients
miss events during disconnect. This is documented and by design — the
audit log and REST endpoints are the catch-up path.

#### `background.py` (171 lines)

Two periodic schedulers:

- `AlpacaScheduler`: syncs positions + marks from Alpaca every 60s.
- `NewsScheduler`: syncs recent news + per-symbol 1-min bar snapshots
  every 30s.

Both skip themselves when Alpaca credentials aren't configured, do an
initial sync immediately on start (not after the first interval), and
honour `CancelledError` for fast shutdown.

#### `settlements_poller.py` (111 lines)

Bridges the new `fincept_core.datasets.SettlementStore` to the
production prediction log. Runs as a background task in the API lifespan
with a configurable poll interval (`SETTLEMENTS_WORKER_POLL_S`, default
60s). Coexists with the old `quant_foundry.settlement` sweep — both read
the same prediction log but write to separate stores. Full consolidation
is deferred.

#### `promotions.py` (527 lines)

Filesystem-backed agent-to-model bindings with two slots per agent:
`active` (feeds the orchestrator) and `shadow` (A/B candidate). Files at
`models/active/<agent_id>.json` and `<agent_id>.shadow.json`. Append-only
history in `<agent_id>.history.jsonl`.

**Design rationale is well-documented:** filesystem over Redis because
Redis is volatile and "what's deployed" must survive restarts;
filesystem over Postgres because there's no strategies table and the
operator workflow needs `cat`-able state.

#### `training.py` (562 lines)

Filesystem-backed training-runs registry. Manages subprocess-based ML
training with a state machine (queued → running → completed/failed).
`MAX_CONCURRENT_RUNS` (default 1) serializes training to avoid swamping
a workstation CPU. Logs are tailed (200 lines) via the run-detail
endpoint.

**Design rationale is well-documented:** subprocess over in-process to
keep the API wheel light (no lightgbm native binary), filesystem over
Postgres for operator-readable state.

#### `feature_importance.py` (214 lines)

Parses LightGBM `model.txt` to compute split-count feature importance
without importing `lightgbm`. Deliberately avoids the native binary
dependency. Computes only split-count (not gain) — gain-based importance
requires the booster's per-split gain values. A future `feature_importance.json`
sidecar from the trainer would provide richer numbers.

#### `openbb_health_store.py` (173 lines)

Redis-backed persistence for OpenBB health probes. Two keys: `obb:health:last`
(STRING, latest probe) and `obb:health:log` (STREAM, capped at ~720 entries
via `MAXLEN ~ 720`). Best-effort writes — Redis failures never block the
live probe response.

#### `symbol_search.py` (350 lines)

Typeahead matcher for the symbol-input UI. Merges the configured
universe with a curated list of ~150 well-known US equities + major
crypto pairs. Scoring tiers: exact match (1000), exact name match (800),
prefix match (500), word-prefix match (300), substring (200/100),
one-edit Levenshtein (50). Pure, sync, deterministic.

### API Routes & Background Tasks

| Route | Method | Auth | Purpose |
|-------|--------|------|---------|
| `/health` | GET | none | Public liveness check |
| `/health/readiness` | GET | JWT | Detailed system readiness |
| `/data/universe` | GET | JWT | List active universe symbols |
| `/data/symbols/search` | GET | JWT | Typeahead symbol search |
| `/data/sources` | GET | JWT | Datasource registry |
| `/data/bars/{symbol}` | GET | JWT | Historical OHLCV bars |
| `/positions` | GET | JWT | All positions across strategies |
| `/positions/{strategy_id}` | GET | JWT | Positions for one strategy |
| `/orders` | GET | JWT | List latest order state |
| `/orders` | POST | JWT | Submit manual order |
| `/strategies` | GET | JWT | Strategy registry summary |
| `/strategies/configs` | GET/POST | JWT | Strategy config CRUD |
| `/strategies/configs/{id}` | GET/PATCH/DELETE | JWT | Single config |
| `/strategies/configs/{id}/start` | POST | JWT | Enable strategy |
| `/strategies/configs/{id}/stop` | POST | JWT | Disable strategy |
| `/news` | GET | JWT | Book-aware news feed |
| `/news-impact/*` | GET/POST | JWT | Experimental news impact model |
| `/services` | GET | JWT | Per-service heartbeat status |
| `/models` | GET | JWT | Per-model status + CV provenance |
| `/models/{name}/outcomes` | GET | JWT | Model outcome history |
| `/models/train` | POST | JWT | Start training run |
| `/models/train/{run_id}` | GET | JWT | Training run status |
| `/regime` | GET | JWT | Latest macro regime + history |
| `/backtest/run` | POST | JWT | Run backtest synchronously |
| `/backtest/runs` | GET | JWT | List persisted backtest runs |
| `/backtest/runs/{run_id}` | GET | JWT | One run's full report |
| `/research/*` | GET/POST | JWT | OpenBB + Exa research dispatchers |
| `/quant-foundry/*` | GET/POST | JWT/HMAC | Quant Foundry gateway |
| `/quant-foundry/alpha/*` | GET/POST | JWT | Alpha Genome Lab |
| `/modules/*` | GET/POST | JWT | On-demand module control |
| `/kill-switch` | POST/DELETE | JWT | Trip/clear kill-switch |
| `/ws` | WS | JWT | WebSocket event multiplexer |

**Background tasks (lifespan-managed):**

1. `AlpacaScheduler` — positions + marks sync (60s)
2. `NewsScheduler` — news + bar snapshots sync (30s)
3. `beat_periodically(redis, "api")` — heartbeat
4. `_poll_quant_foundry_runpod` — RunPod result polling (15s, conditional)
5. `_poll_quant_foundry_tournament` — tournament sweep (300s, conditional)
6. `_poll_quant_foundry_settlement` — settlement sweep (60s, conditional)
7. `_poll_quant_foundry_shadow_dispatch` — shadow inference batch (300s, conditional)
8. `_poll_settlements_worker` — new settlements worker (60s, conditional)

### Connections

- **Redis**: single shared async client on `app.state.redis`, opened in
  lifespan, closed on shutdown. All routes access it via
  `deps.get_redis`.
- **TimescaleDB**: accessed via `fincept_db` readers
  (`read_bars`, `read_universe`, `audit.list_recent_orders`, etc.) — no
  direct DB connections in the API layer.
- **Alpaca**: accessed via `oms.alpaca` clients in background schedulers
  and the `data.py` route's `AlpacaDataClient`.
- **Quant Foundry gateway**: `QuantFoundryGateway.from_env()` stashed on
  `app.state.quant_foundry_gateway`.
- **Filesystem**: `models/`, `data/training_runs/`, `data/predictions/`,
  `data/settlements/`, strategy config files.

### Config

- `FINCEPT_JWT_SECRET` — JWT signing key (must override dev default in prod)
- `REDIS_URL` — Redis connection string
- `ALPACA_API_KEY` / `ALPACA_API_SECRET` / `ALPACA_BASE_URL` — Alpaca credentials
- `QUANT_FOUNDRY_ENABLED` — enable Quant Foundry gateway
- `QUANT_FOUNDRY_RUNPOD_POLL_INTERVAL_SECONDS` — RunPod poll interval (default 15)
- `QUANT_FOUNDRY_TOURNAMENT_INTERVAL_SECONDS` — tournament sweep interval (default 300)
- `QUANT_FOUNDRY_SETTLEMENT_INTERVAL_SECONDS` — settlement sweep interval (default 60)
- `QUANT_FOUNDRY_SHADOW_DISPATCH_INTERVAL_SECONDS` — shadow dispatch interval (default 300)
- `SETTLEMENTS_WORKER_POLL_S` — new settlements worker interval (default 60)
- `FINCEPT_APPROVED_DATA_ROOTS` — approved filesystem roots for path safety
- `NEWS_ALPHA_MODEL_DIR` — news alpha model directory
- `MODELS_DIR` — model artifacts directory

### What's Optimally Implemented

- **Dependency injection throughout**: every route's Redis/store access
  goes through `deps.py` providers, making tests trivial via
  `app.dependency_overrides`.
- **JWT auth is clean and correct**: proper `WWW-Authenticate` header,
  no secret leakage, `?token=` fallback for browser WebSocket.
- **Rate limiter is well-designed**: fixed-window is appropriate for the
  use case, handles edge cases (missing TTL repair, zero retry-after
  guard).
- **News priority scoring is sophisticated and well-documented**:
  composite score with half-life decay, adverse boost, percent-of-book
  threshold that scales with account size. The `_score` function is
  extracted as a pure function for unit testing.
- **Approved-roots path safety**: fail-closed, no runtime disable,
  uniform 422 error body with machine-readable code.
- **Training runs as subprocesses**: keeps the API wheel light, isolates
  heavy ML deps, with a clean state machine and concurrency limit.
- **WebSocket multiplexer is simple and correct**: `xread` with short
  block timeout, no consumer groups (transient broadcast), topic
  filtering.

### What Needs Work

- **`main.py` module-level `assert_safe_for_runtime()` (line 63)**: This
  runs at import time, crashing any test or tool that imports `api.main`
  without a safe `JWT_SECRET`. It's redundant with the call inside
  `lifespan` (line 71). **Fix**: remove the module-level call.
- **Lifespan complexity (185 lines)**: 6+ background tasks with
  duplicated cancel/await boilerplate. **Fix**: extract a helper or use
  `asyncio.TaskGroup` (Python 3.11+).
- **`news_impact.py` `sys.path` mutation (line 31)**: Inserts
  `EXPERIMENT_SRC` into `sys.path` at import time. This is not
  thread-safe, can shadow other modules, and makes the import order-
  dependent. **Fix**: use `importlib` or configure packaging properly.
- **`control.py` script path resolution (line 58-63)**: `_script_path`
  raises `HTTPException` from a non-route helper. This works because
  FastAPI catches it, but it's a layering violation — helpers should
  raise domain exceptions that routes translate.
- **`news.py` `_load_articles` (line 90-110)**: Uses a Redis pipeline
  with one GET per article ID (up to 200). A Redis `MGET` would be a
  single round-trip. The pipeline is better than sequential GETs but
  worse than MGET.
- **`backtest.py` global lock (line 56)**: A single `asyncio.Lock`
  serializes all backtest runs. For a single-operator dashboard this is
  fine, but it's a scaling ceiling. The docstring acknowledges this.

### What Might Break

- **`news_impact.py` `sys.path` insertion**: if `EXPERIMENT_SRC` contains
  a module name that collides with an installed package, the import will
  shadow the installed version. This is a latent import-order bug.
- **Quant Foundry background tasks with `asyncio.to_thread`**: the
  gateway methods (`poll_runpod_results`, `run_tournament_sweep`, etc.)
  run in threads. If the gateway is not thread-safe, concurrent access
  from the poll loop and HTTP routes could corrupt state.
- **`settlements_poller.py` lazy import**: `_build_market_data_source`
  imports `quant_foundry.market_data_adapter` and `settlements.market_data_bridge`
  lazily. If either package is not installed, the poller will log
  exceptions every 60s forever (the `except Exception` in the loop
  swallows `ImportError`).

### What Isn't Implemented Yet

- **OAuth / refresh tokens / scopes / per-user permissions**: v1 is
  single-operator only (documented in `auth.py`).
- **Backtest async queue**: backtests run synchronously in the request
  handler. A background queue is a documented follow-up.
- **Alpaca `/v2/assets` integration for symbol search**: the curated
  list is the fallback; full catalog is a future iteration.
- **Gain-based feature importance**: only split-count is available from
  `model.txt` parsing. A `feature_importance.json` sidecar from the
  trainer would provide richer numbers.

### Better Approaches

- **Lifespan**: use `asyncio.TaskGroup` (3.11+) or an `asynccontextmanager`
  helper that registers tasks and auto-cancels them on exit. This would
  eliminate the manual cancel/await boilerplate.
- **News article loading**: replace the pipeline of GETs with a single
  `MGET` call.
- **`news_impact.py`**: package the experiment as a proper installable
  module instead of `sys.path` manipulation.
- **Quant Foundry task management**: a small `BackgroundTaskRegistry`
  class that handles start/stop/cancel would centralize the pattern.

---

## Service: orchestrator

### Purpose

The `orchestrator` service turns `Prediction` events into `Decision` +
`OrderIntent` pairs. It consumes predictions, sentiment signals, and
regime signals from their respective streams, aggregates them via a
per-symbol consensus builder, maps the consensus to a target notional
via a linear allocator, applies a deadband filter against the last
emitted target, and publishes the resulting Decision + OrderIntent to
the bus. The OMS downstream is the risk-gate authority — the orchestrator
can publish all the intents it wants; the gate decides acceptance.

### Layout

```
services/orchestrator/
  src/orchestrator/
    __init__.py             # Package docstring + public re-exports
    main.py                 # Long-running entrypoint with 4 consumers
    consensus.py            # Per-symbol multi-agent prediction aggregator
    allocator.py            # Pure (direction, confidence) -> target notional
    decisions.py            # Pure target + price -> (Decision, OrderIntent)
    router.py               # Stateful async pipeline gluing the above
  tests/
    test_allocator.py       # Allocator pure-function tests
    test_consensus.py       # Consensus builder tests
    test_decisions.py       # Decision + intent builder tests
    test_router.py          # Router pipeline tests
    test_regime_integration.py  # Regime signal → prediction fan-out tests
    test_sentiment_integration.py # Sentiment signal → prediction tests
```

### How Each Module Works

#### `consensus.py` (123 lines)

`ConsensusBuilder` maintains a per-symbol cache of the latest prediction
per `agent_id`. On each `update(prediction)`, it stores the prediction
in `self._latest[symbol][agent_id]`. On `consensus(symbol, now_ns)`, it:

1. Drops stale predictions (those whose `ts_event + horizon_ns < now_ns`,
   or whose `ts_event + max_age_ns < now_ns` if `horizon_ns` is 0).
2. Computes `direction = sum(d * c) / sum(c)` (confidence-weighted).
3. Computes `confidence = mean(c)` (mean, not sum, so adding agents
   doesn't inflate confidence beyond 1.0).
4. Returns `AgentConsensus` or `None` if no fresh agents or total
   confidence is 0.

**Well-implemented**: the staleness logic correctly distinguishes
horizon-based expiry from max-age fallback. The confidence-weighted
direction + mean confidence is a sensible v1 aggregation rule.

#### `allocator.py` (63 lines)

Pure function `target_notional(direction, confidence, cap_per_symbol,
confidence_threshold)` → signed `Decimal`. Linear scaling: `signal =
direction * confidence`, below threshold → 0, otherwise
`cap * |signal|` quantized to cents. Validates inputs (direction in
[-1,1], confidence in [0,1], cap non-negative).

**Well-implemented**: pure, documented with doctests, quantizes to cents
to avoid float artifacts, conservative against over-confident signals.

#### `decisions.py` (131 lines)

Two pieces:

- `TargetState`: in-memory dict of last-emitted signed target notional
  per symbol. `delta(symbol, new)` returns the rebalance amount.
  `update(symbol, new)` records the new high-water mark.
- `build_decision_and_intent`: pure function that takes a signed delta
  notional + reference price and builds a `(Decision, OrderIntent)` pair
  sharing a fresh `decision_id`. Quantity = `|delta| / price` quantized
  to 8 decimals. Side = BUY if delta > 0, SELL if delta < 0.

**Well-implemented**: the shared `decision_id` is the join key for
audit/blotter/attribution. The quantity quantum (1e-8) is finer than any
spot tick. The docstring explains position-flip handling.

#### `router.py` (161 lines)

`OrchestratorRouter` is the stateful pipeline. `on_prediction` is the
per-event handler:

1. `consensus.update(prediction)`.
2. `consensus = consensus.consensus(symbol, now_ns())` — returns None if
   no fresh agents.
3. `new_target = target_notional(direction, confidence, cap, threshold)`.
4. `delta = target_state.delta(symbol, new_target)`.
5. **Deadband**: skip if `|delta| < min_delta_usd` (default $100).
6. **Price gating**: skip if `LivePrices` has no price for the symbol.
7. `build_decision_and_intent(symbol, delta, price, ...)`.
8. Publish Decision + OrderIntent to streams.
9. Append audit log entry (suppressed exceptions).
10. `target_state.update(symbol, new_target)`.

**Well-implemented**: deadband prevents OMS churn. Price gating prevents
orders with stale data. Audit-before-update ordering means a crash
between audit and target_state.update leaves the target state stale
(next prediction will re-emit the same delta, which is idempotent at the
OMS level).

#### `main.py` (265 lines)

Long-running entrypoint with 4 consumer tasks + heartbeat:

1. `md.trades` → `LivePrices.update` (price cache for sizing).
2. `sig.predict` → `router.on_prediction` (the actual work).
3. `sig.sent` → `_sentiment_to_prediction` → `router.on_prediction`
   (sentiment signals adapted to the Prediction shape with a 30-min
   horizon).
4. `sig.regime` → `_regime_to_predictions` (fans out one market-wide
   regime signal into N per-symbol Predictions with a 4-hour horizon).

**Sentiment adaptation** (line 87): `SentimentSignal.score` (already in
[-1,1]) becomes `direction`, `confidence` passes through, horizon is
30 minutes. The `agent_id` passes through so consensus tracks a distinct
sentiment source per (symbol, agent_id).

**Regime fan-out** (line 139): one `RegimeSignal` becomes N synthetic
`Prediction`s, one per universe symbol. Each gets the same direction
(from `REGIME_DIRECTION` map) and confidence. Horizon is 4 hours (macro
regimes shift on hours-to-days). Uses a lazy import of
`agents.regime_agent.rules` to avoid a hard dependency on the agents
package.

### API Routes & Background Tasks

No HTTP routes. The orchestrator is a pure bus consumer/producer.

**Background tasks:**
1. `Consumer(md.trades)` — price cache updates
2. `Consumer(sig.predict)` — prediction pipeline
3. `Consumer(sig.sent)` — sentiment → prediction adapter
4. `Consumer(sig.regime)` — regime → prediction fan-out
5. `beat_periodically(redis, "orchestrator")` — heartbeat

### Connections

- **Redis**: single async client, 4 `Consumer` instances share it.
- **TimescaleDB**: `audit.append` for every decision emission.
- **No filesystem access**.

### Config

- `REDIS_URL` — Redis connection string
- `MAX_NOTIONAL_USD_PER_SYMBOL` — per-symbol cap (default from Settings)
- `UNIVERSE` — symbol list for regime fan-out
- `JWT_SECRET` — validated via `assert_safe_for_runtime`

### What's Optimally Implemented

- **Pure functions for allocator and decision builder**: trivially
  testable, no I/O, no side effects. The doctests in `allocator.py` pin
  the scaling behavior.
- **Deadband filtering**: prevents OMS saturation from stable signals.
  The $100 default is documented and configurable.
- **Price-availability gating**: never emits an order with a stale price.
- **Audit-before-target-update ordering**: crash-safe.
- **Sentiment + regime integration via Prediction adaptation**: clever
  and correct — reusing the existing per-symbol consensus pipeline
  ensures cross-source consistency (same deadband, same allocator, same
  audit trail).
- **Lazy import of `agents.regime_agent.rules`**: the orchestrator
  doesn't hard-depend on the agents package, which isn't always
  installed in production deploys.
- **Comprehensive test coverage**: 6 test files covering allocator,
  consensus, decisions, router, and both integration paths.

### What Needs Work

- **`TargetState` is in-memory only**: if the orchestrator restarts, it
  loses all target state and will re-emit intents for every symbol on
  the next prediction. The OMS risk gate should catch duplicates, but
  this is a correctness gap. **Fix**: persist target state to Redis.
- **`ConsensusBuilder` cache grows unbounded**: stale predictions are
  filtered on read but never evicted from `self._latest`. Over time with
  many agents/symbols, the dict grows. **Fix**: periodic eviction or
  use a TTL-based structure.
- **No direct test for `main.py`**: the 4-consumer wiring, signal
  adaptation, and shutdown sequence are not tested. The integration
  tests (`test_regime_integration.py`, `test_sentiment_integration.py`)
  test the adaptation functions but not the main loop.

### What Might Break

- **`TargetState` loss on restart**: a restart during active trading
  will cause a burst of order intents. The OMS should handle this (risk
  gate), but it's a stress scenario.
- **Regime fan-out N predictions**: if the universe is large (100+
  symbols), one regime signal generates 100+ `on_prediction` calls in a
  tight loop, each of which calls `consensus.consensus` (O(agents) per
  symbol). This is O(N * A) per regime event, which is fine for small
  N but could be slow at scale.
- **`LivePrices` has no TTL**: if a symbol stops trading, its last price
  stays forever. The orchestrator will keep using a stale price for
  sizing. **Fix**: add a TTL or staleness check in `LivePrices`.

### What Isn't Implemented Yet

- **Kelly-optimal sizing**: v1 uses linear scaling. Kelly needs
  correlations + covariance estimates (TASK-042).
- **Position-aware rebalancing**: the orchestrator tracks last-emitted
  targets, not actual filled positions. If orders don't fill, the target
  state diverges from reality. Phase H concern.
- **Per-symbol tick sizes**: quantity is quantized to 8 decimals
  universally. Real per-symbol tick sizes are a Phase H concern
  (TASK-074 venue catalog).
- **Regime-adaptive weighting**: `regime.py` is listed as DEFERRED
  (TASK-032 dep).

### Better Approaches

- **Persist `TargetState` to Redis**: a simple `HSET` per symbol would
  survive restarts.
- **Eviction in `ConsensusBuilder`**: a periodic sweep or a max-size
  cap on `self._latest`.
- **Test the main loop**: a test that starts the orchestrator with a
  fakeredis, publishes a prediction, and asserts the decision + intent
  appear on the right streams.

---

## Service: jobs

### Purpose

The `jobs` service is a scheduled batch runner using APScheduler. It
cron-fires two daily jobs: the EOD equity bar loader (22:30 ET, Mon-Fri)
and the news-alpha candidate trainer (23:15 ET, Mon-Fri). Both are
designed to run after market close + settlement lag.

### Layout

```
services/jobs/
  src/jobs/
    __init__.py                     # Re-exports run_daily
    main.py                         # APScheduler entrypoint
    daily_eod_load.py               # EOD equity bar loader orchestrator
    news_alpha_candidate_train.py   # News-alpha candidate model trainer
  tests/
    test_daily_eod_load.py          # EOD loader tests
    test_news_alpha_candidate_train.py # News-alpha trainer tests
```

### How Each Module Works

#### `daily_eod_load.py` (79 lines)

`run_daily(target=None, loader_factory, universe_fn)` resolves the
target date (defaults to yesterday), skips weekends via
`is_us_trading_day`, fetches the active equity universe, and hands off
to the configured `Loader` for that date range. Returns the number of
rows written (0 on skip).

Both `loader_factory` and `universe_fn` are injectable for testing.
The default `loader_factory` is `ingestor.eod_equity.get_loader()` which
returns a `YFinanceLoader` (or `PolygonLoader` when implemented).

#### `news_alpha_candidate_train.py` (230 lines)

`run_daily(...)` orchestrates a 3-step subprocess pipeline: export →
train → evaluate. Each step runs as a subprocess via
`asyncio.create_subprocess_exec`. The base command is resolved from
`NEWS_ALPHA_TRAINER_CMD` env var or defaults to `uv run --package agents
python -m agents.news_alpha_predictor.train`.

Returns a `CandidateTrainingResult` dataclass with the status
(`completed`, `export_failed`, `train_failed`, `evaluate_failed`) and
all exit codes. Short-circuits on the first failure.

All parameters are env-var overridable: horizon, dataset path, output
dir, report path, min rows, min AUC, min validation rows.

#### `main.py` (116 lines)

`build_scheduler()` returns a configured `AsyncIOScheduler` with two
cron jobs. `_run()` starts the scheduler + heartbeat, waits for
SIGINT/SIGTERM, then shuts down. `build_scheduler` is exposed separately
so tests can verify the schedule expression without an event loop.

**Schedule:**
- `daily_eod_load`: 22:30 America/New_York, Mon-Fri, 1h misfire grace.
- `news_alpha_candidate_train`: 23:15 America/New_York, Mon-Fri, 1h
  misfire grace.

### API Routes & Background Tasks

No HTTP routes. The jobs service is a pure scheduler.

**Background tasks:**
1. `AsyncIOScheduler` — cron-fires registered jobs
2. `beat_periodically(redis, "jobs")` — heartbeat

### Connections

- **Redis**: heartbeat only.
- **TimescaleDB**: via `ingestor.eod_equity` (writes bars) and
  `fincept_db.bars.write_bars`.
- **Filesystem**: model output dirs, dataset paths, report paths.
- **Subprocess**: `asyncio.create_subprocess_exec` for the news-alpha
  trainer pipeline.

### Config

- `REDIS_URL` — Redis connection string
- `FINCEPT_UNIVERSE` — validated via `assert_safe_for_runtime`
- `NEWS_ALPHA_TRAINER_CMD` — override trainer base command
- `NEWS_ALPHA_TRAIN_HORIZON` — training horizon (default "30m")
- `NEWS_ALPHA_TRAIN_DATASET_PATH` — dataset path
- `NEWS_ALPHA_CANDIDATE_DIR` — model output directory
- `NEWS_ALPHA_CANDIDATE_REPORT` — evaluation report path
- `NEWS_ALPHA_TRAIN_MIN_ROWS` — minimum rows threshold (default 200)
- `NEWS_ALPHA_MIN_AUC` — minimum AUC threshold (default 0.52)
- `NEWS_ALPHA_MIN_VAL_ROWS` — minimum validation rows (default 40)

### What's Optimally Implemented

- **`build_scheduler` exposed separately**: tests can verify the cron
  expression without spinning up an event loop. Good separation.
- **DST-aware cron timezone**: `America/New_York` ensures the schedule
  tracks NYSE local time correctly across DST transitions.
- **Misfire grace time (1h)**: if the host was asleep at fire time, the
  job still runs within the grace window.
- **Both jobs are injectable**: `daily_eod_load.run_daily` takes
  `loader_factory` and `universe_fn`; `news_alpha_candidate_train.run_daily`
  takes a `runner` callable. Tests don't touch the network or DB.
- **Short-circuit on failure**: the news-alpha trainer stops at the
  first failed step and returns a structured result with all exit codes.
- **Env-var overrides for all parameters**: the news-alpha trainer can
  be reconfigured without code changes.

### What Needs Work

- **No job failure notification**: if `run_daily` fails, it logs but
  doesn't alert. An operator might not notice until the next dashboard
  check. **Fix**: publish an `AlertEvent` on failure.
- **No retry logic**: a failed yfinance fetch (network hiccup) is not
  retried. The next day's run will pick up the missing day if the range
  is extended, but there's no automatic catch-up.
- **No direct test for `main.py`**: the scheduler wiring and shutdown
  sequence are not tested. `build_scheduler` is tested via the test
  suite but `_run` is not.

### What Might Break

- **`uv` not on PATH**: `resolve_trainer_base_command` raises
  `RuntimeError` if `uv` is not found and `NEWS_ALPHA_TRAINER_CMD` is
  not set. This will crash the scheduled job silently (the exception is
  in a subprocess, so the scheduler continues).
- **yfinance rate limiting**: the `YFinanceLoader` is rate-limited at
  ~2k req/h. A large universe could hit the limit. The shortfall
  heuristic (95% threshold) logs a warning but doesn't retry.
- **Subprocess zombies**: if the API process is killed between starting
  a subprocess and its completion, the subprocess could become a zombie.
  The `asyncio.create_subprocess_exec` + `proc.wait()` pattern handles
  this correctly for normal shutdown, but a SIGKILL would orphan the
  child.

### What Isn't Implemented Yet

- **Holiday calendar**: `is_us_trading_day` only checks weekdays. US
  holidays are handled by yfinance returning empty rows (best-effort).
  `pandas_market_calendars` is documented as a follow-up.
- **PolygonLoader**: stub that raises `NotImplementedError`. Full
  implementation is gated on Phase H budget.
- **Job failure alerting**: no AlertEvent publication on failure.
- **Automatic catch-up for missed days**: no mechanism to detect and
  backfill gaps in the EOD bar history.

### Better Approaches

- **Publish AlertEvent on job failure**: a simple `Producer.publish`
  call in the exception handler would surface failures to the
  dashboard's alert lane.
- **Retry with backoff for yfinance**: a single retry after 30s would
  handle transient network failures.
- **Test the main loop**: a test that starts the scheduler with a fake
  job and verifies it fires at the expected time.

---

## Service: ingestor

### Purpose

The `ingestor` service is the market-data ingestion pipeline. It
connects to venue WebSocket feeds (Binance, Coinbase, Kraken), normalizes
raw messages into canonical Pydantic events (`TradeEvent`,
`BookDeltaEvent`, `BookSnapshotEvent`), publishes them to Redis Streams
(`md.trades`, `md.books`), rolls trades into 1-minute OHLCV bars
(`md.bars.1m`), and batches writes to TimescaleDB. A standalone quality
monitor process consumes the same streams and emits `AlertEvent`s for
sequence gaps, clock skew, cross-venue spread anomalies, and staleness.

### Layout

```
services/ingestor/
  src/ingestor/
    __init__.py             # Package docstring
    main.py                 # Entrypoint with reconnect + signal handling
    base.py                 # VenueAdapter ABC
    normalizer.py           # Symbol-format conversion utilities
    binance.py              # Binance spot WebSocket adapter
    coinbase.py             # Coinbase Advanced Trade WebSocket adapter
    kraken.py               # Kraken v2 WebSocket adapter
    writer.py               # Fan-out to Redis Streams + batched Timescale writes
    quality.py              # LatencyTracker + QualityMonitor
    quality_main.py         # Standalone QualityMonitor entrypoint
    eod_equity.py           # Daily EOD equity loader (yfinance/Polygon)
  tests/
    test_base.py            # VenueAdapter tests
    test_binance_normalize.py  # Binance parser tests
    test_coinbase_normalize.py # Coinbase parser tests
    test_kraken_normalize.py   # Kraken parser tests
    test_eod_equity.py      # EOD loader tests
    test_latency.py         # LatencyTracker tests
    test_normalizer.py      # Symbol normalizer tests
    test_quality.py         # QualityMonitor tests
    test_writer.py          # Writer tests
```

### How Each Module Works

#### `base.py` (50 lines)

`VenueAdapter` ABC with three abstract methods: `connect()`,
`stream()` (async generator yielding canonical events), and `close()`.
Implementations must tag every event with `ts_recv = now_ns()`.

#### `normalizer.py` (91 lines)

Symbol-format conversion utilities. `to_canonical` splits a venue symbol
into `BASE-QUOTE` by matching the longest known quote suffix. Venue-
specific converters: `to_binance_symbol` (lowercase, no separator),
`to_coinbase_symbol` (uppercase, dash), `to_kraken_symbol` (XBT
substitution, slash separator). `iso8601_to_ns` converts ISO-8601
timestamps to integer nanoseconds using integer math to avoid float
drift.

**Well-implemented**: the integer-math conversion in `iso8601_to_ns`
(line 88-91) is a deliberate precision choice — `dt.timestamp()` returns
float seconds, so the code uses `int(dt.timestamp()) * 1e9 +
dt.microsecond * 1e3` to stay in integer arithmetic.

#### `binance.py` (161 lines)

Binance spot adapter subscribing to `<sym>@trade` and
`<sym>@depth@100ms` via the combined-stream endpoint. Static parse
methods (`_parse_trade`, `_parse_depth_update`) are extracted for
testing without a WebSocket.

**Binance-specific handling:**
- `m=True` (buyer is market maker) → taker SELL side.
- Exchange timestamps in ms → converted to ns by `* 1_000_000`.
- Depth updates: size 0 = removal, positive = upsert.
- No snapshots (Binance only sends deltas) — documented limitation.

#### `coinbase.py` (208 lines)

Coinbase Advanced Trade adapter subscribing to `market_trades` and
`level2` channels. The L2 channel delivers an initial `snapshot`
followed by `update` messages. Coinbase uses "bid"/"offer" (not "ask").

**Well-implemented**: the `_parse_envelope`/`_parse_trades`/`_parse_l2`
classmethods are extracted for testing. Snapshot vs update dispatch is
clean. The `iso8601_to_ns` helper centralizes timestamp handling.

#### `kraken.py` (238 lines)

Kraken v2 adapter subscribing to `trade` and `book` channels. Kraken-
specific quirks handled:
- `XBT/USD` ↔ `BTC-USD` conversion via `normalizer`.
- Prices as JSON numbers → `Decimal(str(x))` to avoid float artifacts.
- Snapshot vs update dispatched by message-level `type` field.

#### `writer.py` (232 lines)

`Writer` is the fan-out hub. For each event from an adapter:

1. **TradeEvent**: publish to `md.trades`, observe for 1m bar rolling,
   buffer for batched DB write.
2. **BookDeltaEvent**: publish to `md.books`, buffer for batched DB write.
3. **BookSnapshotEvent**: publish to `md.books` (not persisted as deltas).

**1-minute bar rolling** (`_MinuteBar`): trades are accumulated per
(venue, symbol, minute). When a trade from a later minute arrives, the
current bar is published to `md.bars.1m` and written to Timescale. Late
trades (earlier than the current open minute) are logged and ignored for
bar purposes — historical repair is deferred to the EOD/backfill path.

**Batched DB writes**: trades and books are buffered in memory and
flushed to Timescale when the buffer reaches `batch_size` (default 500)
or `flush()` is called. Idempotency via `ON CONFLICT DO NOTHING` on
`(venue, symbol, ts_event, seq)`.

**Well-implemented**: the bar rolling logic is correct (open/high/low/
close/volume/notional/trades/vwap). The batching rationale is well-
documented (round-trip latency dominates throughput). The `flush()`
method drains both buffers + pending bars for graceful shutdown.

#### `quality.py` (351 lines)

Two complementary observers:

**`LatencyTracker`** (sync, in-process): per-(venue, symbol) sequence-
gap totals, max latency, rolling p99 latency. Fire-and-forget, never
raises. Uses a `deque(maxlen=latency_window)` for p99 calculation.

**`QualityMonitor`** (async, event-driven): designed to run as a
separate process consuming `md.trades` / `md.books`. Detects:
- Sequence gaps (seq != prev + 1).
- Clock skew (ts_recv - ts_event > 1s budget).
- Cross-venue spread anomalies (mid divergence > 50 bps).
- Staleness (no events for > 30s).

Alert dedup via `(code, frozenset(tags.items()))` key with 30s TTL.
Lazy GC when the dedup table exceeds 1024 keys.

**Design rules are well-documented:**
- Top-of-book tracked only on snapshots (Binance never emits snapshots,
  so cross-spread is Coinbase↔Kraken only — documented and correct).
- Cross-venue comparison by canonical symbol (BTC-USDT ≠ BTC-USD).
- Alert dedup with TTL + lazy GC.

#### `quality_main.py` (129 lines)

Standalone entrypoint for `QualityMonitor`. Subscribes to `md.trades`
and `md.books` via a Redis Streams consumer group, dispatches events to
`on_trade`/`on_book`, and runs `staleness_check` on a 5s periodic loop.
Deliberately does not wrap the consume loop in try/except — silent
recovery would hide bugs that should page the operator.

#### `eod_equity.py` (336 lines)

Daily EOD equity loader. `YFinanceLoader` is the default (free, lightly
rate-limited). `PolygonLoader` is a paid stub. `get_loader()` factory
picks based on settings.

**DST-aware timestamps**: `trading_day_close_to_ns(d)` pins the close to
NYSE local 16:00 via `zoneinfo.ZoneInfo("America/New_York")`. The
docstring explains why the original spec's `iso_to_ns` on a tz-naive
timestamp would produce different values on different host clocks.

**Dependency injection**: `download_fn` and `write_fn` are injectable
for testing. Tests inject hand-built DataFrames and capture `write_fn`
calls — no network, no DB.

**Shortfall heuristic**: `_expected_rows` approximates trading days ×
symbols. If `fetched / expected < 0.95`, logs a warning.

#### `main.py` (179 lines)

Entrypoint with reconnect + signal handling. The hot loop:
`connect → stream → handle → reconnect` with capped exponential backoff
(1s → 60s). Detects HTTP 451 (geo-block) and surfaces an actionable hint.
One venue per process (`--venue {binance,coinbase,kraken}`).

### API Routes & Background Tasks

No HTTP routes. The ingestor is a pure bus producer (+ standalone quality
monitor consumer).

**Background tasks (main.py):**
1. `run_loop(adapter, writer, latency, stop)` — connect/stream/handle loop
2. `beat_periodically(redis, "ingestor")` — heartbeat

**Background tasks (quality_main.py):**
1. `Consumer.consume([md.trades, md.books])` — quality monitor dispatch
2. `_staleness_loop(monitor, stop)` — periodic staleness sweep (5s)

### Connections

- **Redis**: single async client for the main ingestor; separate client
  for the quality monitor.
- **TimescaleDB**: via `fincept_db.ticks.write_trades` /
  `write_book_deltas` and `fincept_db.bars.write_bars` (batched).
- **WebSocket**: `websockets` library to venue endpoints.
- **Filesystem**: none (EOD loader uses yfinance API, not filesystem).

### Config

- `REDIS_URL` — Redis connection string
- `FINCEPT_UNIVERSE` — symbol list to ingest (must be non-empty)
- `FINCEPT_JWT_SECRET` — validated via `assert_safe_for_runtime`
- `POLYGON_API_KEY` — enables Polygon loader (stub, not yet implemented)

### What's Optimally Implemented

- **Canonical event normalization**: all three venue adapters produce
  the same `TradeEvent`/`BookDeltaEvent`/`BookSnapshotEvent` types,
  making downstream consumers venue-agnostic.
- **Static parse methods**: every adapter's `_parse_*` methods are
  `@staticmethod`/`@classmethod`, extracted for testing without a
  WebSocket. This is excellent testability design.
- **1-minute bar rolling in the writer**: correct OHLCV accumulation
  with VWAP, late-trade handling, and graceful flush on shutdown.
- **Batched DB writes with idempotency**: `ON CONFLICT DO NOTHING` on
  `(venue, symbol, ts_event, seq)` absorbs duplicate publishes from
  reconnects or re-broadcasts.
- **DST-aware EOD timestamps**: `trading_day_close_to_ns` uses
  `zoneinfo.ZoneInfo("America/New_York")` — the docstring explains the
  precision landmine this avoids.
- **Quality monitor design**: two complementary observers (sync in-
  process + async standalone) with well-documented design rules. Alert
  dedup with TTL + lazy GC.
- **Reconnect with exponential backoff + geo-block detection**: the
  HTTP 451 hint for Binance geo-blocking is a thoughtful operator UX
  touch.
- **Comprehensive test coverage**: 9 test files covering all adapters,
  normalizer, writer, quality, latency, and EOD loader.

### What Needs Work

- **`writer.py` unbounded buffer growth**: if the DB is slow, the
  `_trades` and `_books` lists grow without bound until `batch_size` is
  reached (at which point a flush is attempted, which may also be slow).
  There's no backpressure mechanism. **Fix**: add a max-buffer-size
  check that drops or logs when buffers exceed a threshold.
- **`quality.py` `_last_top` only on snapshots**: cross-venue spread
  detection is structurally Coinbase↔Kraken only because Binance never
  emits snapshots. This is documented but limits the feature's
  usefulness. **Fix**: maintain top-of-book from deltas (requires a
  book-state manager, which is intentionally out of scope per the
  docstring).
- **`quality_main.py` has no tests**: the standalone entrypoint's
  consume loop, dispatch, and shutdown sequence are not tested.
- **`main.py` one venue per process**: no supervisor for multi-venue
  fan-out. Running 3 venues requires 3 processes. A future task will
  fan multiple venues into a single supervisor.
- **`eod_equity.py` holiday handling**: `is_us_trading_day` only checks
  weekdays. Holidays are handled by yfinance returning empty rows,
  which is best-effort. `pandas_market_calendars` is documented as a
  follow-up.
- **`iso8601_to_ns` fallback to `now_ns()`**: if the timestamp is empty
  or unparseable, the function returns `now_ns()` silently. Callers
  can't distinguish a real timestamp from a fallback. **Fix**: return
  `None` and let callers decide.

### What Might Break

- **WebSocket reconnection during book snapshot sync**: if the
  connection drops between a snapshot and the first delta, the book
  state is inconsistent. The docstring says "snapshot sync is out of
  scope" but this is a real operational gap.
- **`Decimal(str(x))` on Kraken JSON numbers**: Kraken sends prices as
  JSON numbers (floats). `Decimal(str(0.1))` → `Decimal("0.1")` is
  correct, but `str(float)` can produce scientific notation for very
  small numbers, which `Decimal` handles correctly. This is fine but
  worth noting.
- **`yfinance` API changes**: the `YFinanceLoader` depends on
  `yfinance.download`'s output format (column names, multi-level
  indexing). A yfinance version bump could break `_parse_yfinance_frame`.
  The `_extract_per_symbol_frame` helper is defensive but not
  future-proof.

### What Isn't Implemented Yet

- **Book-state recovery on reconnect**: snapshot sync is out of scope
  (TASK-014).
- **PolygonLoader**: stub that raises `NotImplementedError`.
- **Multi-venue supervisor**: one venue per process; a future task will
  fan out.
- **Alert routing (PagerDuty/Slack)**: Phase H (TASK-073).
- **Persistent alert log**: Phase H (TASK-074); v1 lives only on the
  `events.alerts` stream.
- **Tether-stable groupings for cross-venue comparison**: deferred to a
  config-driven enhancement.

### Better Approaches

- **Backpressure in `Writer`**: a max-buffer-size check with a log
  warning would surface DB slowness before it causes OOM.
- **`iso8601_to_ns` returning `Optional[int]`**: let callers handle
  missing timestamps explicitly.
- **Test `quality_main.py`**: a test that starts the monitor with a
  fakeredis, publishes a trade with a seq gap, and asserts an alert
  appears on `events.alerts`.
- **`pandas_market_calendars` for holiday handling**: would eliminate
  the empty-frame warnings on holidays.

---

## Service: features

### Purpose

The `features` service computes online and offline feature frames from
bar events. The online runner consumes `md.bars.1m` from Redis Streams,
computes per-symbol features incrementally (returns, momentum,
volatility, cross-venue beta/correlation), and publishes `FeatureFrame`s
to `features.online` + caches them in Redis for fast agent inference.
The offline backfill path replays historical bars through the exact same
compute kernel, guaranteeing bit-identical online vs offline values. A
PIT joiner enforces the no-lookahead invariant for backtesting.

### Layout

```
services/features/
  src/features/
    __init__.py             # Package docstring + public re-exports
    main.py                 # Entrypoint for the online feature runner
    online.py               # OnlineRunner: bars-in / FeatureFrame-out
    computer.py             # FeatureComputer: shared compute kernel
    offline.py              # Batch backfill of historical bars
    pit.py                  # PITJoiner: point-in-time joins
    store.py                # OnlineStore (Redis) + OfflineStore (Timescale)
    transforms/
      __init__.py           # Re-exports transform classes
      price.py              # Log/simple returns + multi-window momentum
      volatility.py         # Realized vol + Parkinson + Garman-Klass
      cross.py              # Rolling beta + correlation vs benchmark
  tests/
    test_backfill.py        # Offline backfill tests
    test_computer.py        # FeatureComputer kernel tests
    test_cross.py           # CrossFeatures tests
    test_online_runner.py   # OnlineRunner tests
    test_pit.py             # PITJoiner tests
    test_price.py           # PriceFeatures tests
    test_store.py           # OnlineStore + OfflineStore tests
    test_volatility.py      # VolatilityFeatures tests
```

### How Each Module Works

#### `computer.py` (66 lines)

`FeatureComputer` is the shared compute kernel. State is per-instance:
one `PriceFeatures` per symbol, one `VolatilityFeatures` per symbol, one
shared `CrossFeatures` keyed on the configured benchmark.

`compute(bar)` updates internal state with the bar and returns a merged
`FeatureFrame`. The benchmark's own bar must update the bench deque
BEFORE cross features are queried for it — otherwise the symbol deque
grows one element ahead of the bench and the windows misalign (line 56).

**This is the bit-identical guarantee**: both `OnlineRunner` and
`offline.backfill` use this exact class, so same inputs always produce
the same `FeatureFrame`.

#### `transforms/price.py` (74 lines)

`PriceFeatures`: per-symbol rolling close history → returns + momentum.
`update(close)` appends to a deque and returns `ret_log_1`,
`ret_simple_1`, `mom_5`, `mom_20`, `mom_60`. `None` for any feature that
doesn't have enough history (PIT-correct: never invent values).

**Design choice**: `float` not `Decimal` for returns/momentum because
they're unitless ratios — IEEE-754 double precision is sufficient. Money
quantities stay `Decimal` upstream; conversion happens once at the
input.

**Well-implemented**: handles non-positive previous close (corporate
action artifact) by emitting `None` rather than divide-by-zero or
`log(0)`.

#### `transforms/volatility.py` (113 lines)

`VolatilityFeatures`: three families of per-bar volatility estimators
over configurable windows (5, 20, 30, 60, 240):

- `vol_rs_w`: rolling sample stdev of 1-bar log returns (ddof=1).
- `vol_park_w`: Parkinson estimator from H/L ranges (~5x more efficient
  than close-to-close).
- `vol_gk_w`: Garman-Klass estimator using OHLC (even more efficient,
  can go negative → returns `None`).

All PIT-correct: `None` until the window is filled.

**Well-implemented**: the GK negative-check (line 111) emits `None`
rather than `sqrt(-x)` which would raise. The Parkinson normalization
uses `4 * ln(2)` as documented. The `max(w-1, 1)` in realized vol
(line 83) avoids divide-by-zero for `w=1`.

#### `transforms/cross.py` (91 lines)

`CrossFeatures`: rolling beta + Pearson correlation vs a benchmark
symbol. Maintains two parallel deques: `self._bench_rets` (benchmark)
and `self._sym_rets[symbol]` (per-symbol). For each window, recomputes
beta and correlation over the last `w` aligned-by-position elements.

**Position-based alignment**: the "by-position" alignment assumes 1-min
bars across venues co-arrive at roughly the same wall-clock minute. For
tighter alignment (nanosecond), a future revision can switch to a
ts_event-keyed dict. Spec landmine #5 documents this trade-off.

**Well-implemented**: `None` for beta/corr when either deque has < w
samples (no defaulting to zero). `var_y > 0` guard prevents divide-by-
zero. `w < 2` validation prevents undefined covariance.

#### `online.py` (93 lines)

`OnlineRunner`: consumes `BarEvent`s, dispatches through
`FeatureComputer`, publishes `FeatureFrame` to `features.online`, and
optionally caches in `OnlineStore` (Redis). `handle_event` ignores non-
bar events. `_default_benchmark` picks the first UNIVERSE symbol or
falls back to `DEFAULT_BENCHMARK` ("BTC-USD").

**PIT correctness holds for free**: bars arrive in monotonic `ts_event`
order on a single stream.

#### `offline.py` (89 lines)

`backfill(symbols, freq, start_ns, end_ns, ...)`: reads bars from
Timescale via `read_bars`, drives them through a single
`FeatureComputer`, and writes `FeatureFrame`s to `OfflineStore`.

**Bench-first ordering**: the benchmark symbol is processed first so its
returns populate the cross-feature deque before any other symbol asks
for `beta_*` or `corr_*`. Non-benchmark symbols processed before the
benchmark would emit `None` for cross features even on bars where the
live runner had data — breaking bit-identical.

**Idempotency**: `OfflineStore` uses `ON CONFLICT DO UPDATE` so re-
running the same range overwrites prior values (right behavior for "fix
a transform bug, re-run").

#### `pit.py` (86 lines)

`PITJoiner`: joins each bar with the latest `FeatureFrame` whose
`ts_event <= bar.ts_event`. Groups bars by `(symbol, freq)`, fetches a
single feature range per group, and walks both lists with a two-pointer
scan — O(N) over both.

**The headline invariant**: if the join would ever return a feature
whose `ts_event > bar.ts_event`, it raises `RuntimeError`. A test pins
this (test_pit.py:81).

**Well-implemented**: the two-pointer scan is efficient. The defensive
sort (line 63) doesn't assume input order. The lookback (1 year) covers
any realistic gap. The `RuntimeError` assertion (line 81) is the entire
point of the class.

#### `store.py` (102 lines)

Two layers:

- `OnlineStore` (Redis): latest-known `FeatureFrame` per `(symbol, freq)`
  with 5-day TTL. Serves agent inference at <10ms. Lossy and ephemeral.
- `OfflineStore` (Timescale): append-only hypertable. Authoritative
  history for backtesting, training, and PIT joins. Idempotent via `ON
  CONFLICT DO UPDATE`.

Both support dependency injection of `write_fn`/`read_fn` for testing
without a database.

#### `main.py` (91 lines)

Entrypoint for the online feature runner. Wires `Consumer(md.bars.1m)`
→ `OnlineRunner.handle_event` → `Producer.publish(features.online)` →
`OnlineStore.put`. Same shutdown pattern as ingestor/quality_main.

### API Routes & Background Tasks

No HTTP routes. The features service is a pure bus consumer/producer.

**Background tasks:**
1. `Consumer.consume([md.bars.1m])` — bar consumption + feature computation
2. `beat_periodically(redis, "features")` — heartbeat

### Connections

- **Redis**: single async client for consume + produce + online store.
- **TimescaleDB**: via `fincept_db.bars.read_bars` (offline) and
  `fincept_db.features.write_features`/`read_features` (offline store).

### Config

- `REDIS_URL` — Redis connection string
- `UNIVERSE` — symbol list (first symbol is the default benchmark)
- `FINCEPT_JWT_SECRET` — validated via `assert_safe_for_runtime`

### What's Optimally Implemented

- **Bit-identical online/offline guarantee**: sharing the
  `FeatureComputer` kernel between `OnlineRunner` and `offline.backfill`
  is the correct architecture. The test
  `test_two_computers_with_same_bar_sequence_are_bit_identical` pins it.
- **PIT-correct features**: every transform emits `None` until enough
  history exists. Never invents values. The `PITJoiner` enforces the
  invariant with a `RuntimeError` and a test pins it.
- **PITJoiner two-pointer scan**: O(N) over both bars and features,
  grouped by `(symbol, freq)` to minimize queries. The defensive sort
  and the `RuntimeError` assertion are excellent defensive programming.
- **Bench-first ordering in backfill**: ensures cross features are
  populated before any symbol asks for them. The docstring explains why.
- **Decimal for money, float for ratios**: the distinction is explicit
  (spec landmine #2). `PriceFeatures` converts `Decimal` to `float` once
  at the input.
- **GK negative-check**: emits `None` rather than `sqrt(-x)`. The
  Parkinson normalization is correct (`4 * ln(2)`).
- **Comprehensive test coverage**: 8 test files covering every module
  including transforms. Tests use fakes/injections, not real Redis/DB.
- **Dependency injection in stores**: `OnlineStore` takes a `Redis`
  client; `OfflineStore` takes `write_fn`/`read_fn` callables. Tests
  inject fakes.

### What Needs Work

- **`CrossFeatures` position-based alignment**: beta/correlation are
  computed over the last `w` elements of both deques by position, not
  by timestamp. If venues have different bar arrival cadences (e.g.,
  Binance has more trades per minute than Kraken), the deques fall out
  of alignment and beta/corr are computed on mismatched time windows.
  This is documented (spec landmine #5) but is a correctness limitation.
  **Fix**: switch to ts_event-keyed dicts for alignment.
- **`FeatureComputer` state grows unbounded**: `self._price` and
  `self._vol` dicts grow per symbol and are never evicted. For a fixed
  universe this is fine, but if symbols are dynamically added (e.g.,
  universe expansion), memory grows. **Fix**: periodic eviction of
  inactive symbols.
- **`main.py` has no tests**: the consume loop, shutdown sequence, and
  `OnlineStore` wiring are not tested.
- **`OnlineStore` TTL (5 days)**: if the feature service is down for
  more than 5 days, the cache empties and agent inference reads return
  `None` until the service restarts. The offline store is the catch-up
  path, but there's no automatic repopulation. **Fix**: a startup hook
  that backfills the online store from the offline store.

### What Might Break

- **`CrossFeatures` deque misalignment**: if BTC-USD has 100 bars and
  ETH-USD has 95 bars (because ETH had fewer trades in some minutes),
  the last-60 windows of both deques cover different time ranges. Beta
  and correlation are computed on mismatched windows. The values are
  mathematically valid but semantically wrong.
- **`VolatilityFeatures` `_realized_vol` for w=1**: `max(w-1, 1)` gives
  `max(0, 1) = 1`, so variance = `(x - mean)^2 / 1 = 0` (since mean = x
  for a single element). This is correct (stdev of a single sample is
  0) but might surprise users who expect `None` for w=1.
- **`PITJoiner` lookback (1 year)**: if the feature store has more than
  1 year of history, the lookback might miss the earliest features. The
  1-year default is generous but not infinite.

### What Isn't Implemented Yet

- **Timestamp-aligned cross features**: position-based alignment is the
  v1 approach; ts_event-keyed alignment is a future revision.
- **Automatic online store repopulation**: no startup hook to backfill
  from the offline store.
- **Feature schema discovery**: the set of feature keys is implicit in
  the transform classes. A schema registry would make downstream
  consumers more robust.
- **Per-symbol eviction in `FeatureComputer`**: no mechanism to evict
  inactive symbols from internal state.

### Better Approaches

- **Timestamp-aligned `CrossFeatures`**: maintain a `dict[ts_event,
  float]` per symbol instead of a `deque`, and align by timestamp
  before computing beta/corr. This would fix the cadence mismatch.
- **Test `main.py`**: a test that starts the runner with a fakeredis,
  publishes a bar, and asserts a `FeatureFrame` appears on
  `features.online` + in the `OnlineStore`.
- **Startup backfill for `OnlineStore`**: on `main.py` startup, read the
  latest features from `OfflineStore` and populate `OnlineStore` for
  each universe symbol.

---

## Cross-Service Observations

### Shared Patterns (Positive)

1. **Consistent shutdown pattern**: every long-running service uses
   `asyncio.Event` + SIGINT/SIGTERM signal handlers + task cancellation
   with `contextlib.suppress(asyncio.CancelledError)`. This is correct
   and uniform across `api`, `orchestrator`, `ingestor`, `features`, and
   `jobs`.

2. **Heartbeat registration**: every long-running service calls
   `beat_periodically(redis, "<service_name>")` so the API's
   `/services` endpoint can report service status.

3. **`assert_safe_for_runtime` guardrail**: every service that runs as a
   long-lived process calls this before starting, preventing accidental
   production deployment with the dev JWT secret.

4. **Dependency injection for testability**: every module that touches
   I/O (Redis, DB, WebSocket, filesystem) accepts injectable dependencies.
   Tests use fakes throughout — no real Redis, no real DB, no network.

5. **Structured logging**: every service uses `fincept_core.logging.get_logger`
   with structured event names (`service.event_name`), making logs
   greppable and machine-parseable.

6. **Detailed module docstrings**: every module opens with a multi-
   paragraph docstring explaining the purpose, design decisions, and
   trade-offs. This is exceptional documentation discipline.

### Shared Patterns (Negative)

1. **No direct tests for `main.py` entrypoints**: `api/main.py`,
   `orchestrator/main.py`, `ingestor/main.py`, `features/main.py`, and
   `jobs/main.py` all lack direct tests of their main loops. The
   individual components are well-tested, but the wiring (consumer
   setup, signal handling, shutdown sequence) is not.

2. **In-memory state not persisted**: `TargetState` (orchestrator),
   `ConsensusBuilder._latest` (orchestrator), `FeatureComputer._price`/
   `_vol` (features), and `Writer._trades`/`_books` (ingestor) are all
   in-memory and lost on restart. This is documented in each case but
   represents a correctness gap for production trading.

3. **Unbounded dict growth**: several services maintain dicts that grow
   without eviction: `ConsensusBuilder._latest`,
   `FeatureComputer._price`/`_vol`, `QualityMonitor._recent_alerts`
   (has lazy GC at 1024), `QualityMonitor._last_ts`/`_last_seq`/
   `_last_top` (no eviction).

4. **Redis client lifecycle**: every service creates a single
   `Redis.from_url(settings.REDIS_URL)` client and closes it on
   shutdown. There's no connection pooling or retry on connection
   failure. For the current single-process scale this is fine, but it
   won't scale to multi-process deployments without a shared pool.

### Dependency Graph

```
api ──→ fincept-core, fincept-bus, fincept-db, fincept-tools
 │      oms, portfolio, quant_foundry, backtester, agents (lazy)
 │
orchestrator ──→ fincept-core, fincept-bus, fincept-db
 │               oms, agents (lazy)
 │
jobs ──→ fincept-core, fincept-bus, fincept-db
 │       ingestor (eod_equity)
 │
ingestor ──→ fincept-core, fincept-bus, fincept-db
 │
features ──→ fincept-core, fincept-bus, fincept-db
```

No service imports from another service directly. All inter-service
communication is via Redis Streams. `jobs` imports from `ingestor`
(`eod_equity`), which is a library-style import (not a runtime
dependency). `api` has lazy imports to `agents` (regime rules) and
`quant_foundry` (gateway). The dependency graph is clean and acyclic.

### Test Coverage Summary

| Service | Source Files | Test Files | Coverage Quality |
|---------|-------------|------------|-----------------|
| api | ~20 | 31 | High — every route + helper has tests |
| orchestrator | 5 | 6 | High — every module + integration paths |
| jobs | 3 | 2 | Medium — job logic tested, main loop not |
| ingestor | 10 | 9 | High — every adapter + writer + quality |
| features | 8 | 8 | High — every module + transform + PIT |

**Untested entrypoints**: `api/main.py`, `orchestrator/main.py`,
`ingestor/main.py`, `ingestor/quality_main.py`, `features/main.py`,
`jobs/main.py`. These are thin wiring modules, but the shutdown
sequence and signal handling are not verified by tests.
