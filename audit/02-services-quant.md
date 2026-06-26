# Audit: services/ (Quant & Financial Backend Services)

## Executive Summary

This audit covers the eight quant/financial backend services that
extend the fincept-terminal platform beyond the core API/orchestrator/
ingestor/features spine documented in `02-services-core.md`:

| Service | Role |
|---|---|
| `quant_foundry` | External ML worker bridge: training → inference → shadow → settlement → tournament → promotion pipeline, dispatching to RunPod or a local mock. |
| `backtester` | Deterministic event-driven historical replay with realistic costs, walk-forward validation, and GBM strategy support. |
| `risk` | Pre-trade limit checks + in-memory kill-switch state; library-first, imported inline by the OMS and backtester. |
| `portfolio` | Live position tracking: consumes Fills, applies shared position math, snapshots Positions to the bus + Redis hash for fast UI reads. |
| `oms` | Order Management System: consumes OrderIntents, simulates fills (paper) or routes to Alpaca (paper/live), maintains order state machine + audit log. |
| `settlements` | Settlement worker MVP: tails the prediction log, fetches realized market data, writes settlement records with cost-adjusted returns + Brier scores. |
| `strategy_host` | Live runtime for `StrategyConfig` instances: reconciles on-disk configs against running asyncio tasks, dispatches strategy hooks on bar/fill/position streams. |
| `agents` | Strategy agents (GBM predictor, regime, sentiment, news-alpha, information enricher, outcome labeler, news-impact) that consume features and emit Prediction events. |

The architectural layering is clean and consistent with the core
services: all services depend on the shared `libs/` packages
(`fincept-core`, `fincept-bus`, `fincept-db`, `fincept-sdk`) and
communicate via Redis Streams. Cross-service dependencies are
explicit and minimal: `risk` depends on `portfolio` (for
`PositionStore`); `oms` depends on `risk` + `portfolio`; `backtester`
depends on `risk` (for the optional risk gate); `strategy_host`
depends on `backtester` (for `build_strategy` and the strategy
registry); `settlements` has no service-level dependencies (only
`fincept-core`); `agents` depends on `features` (for `OnlineStore`).

Overall quality is **high**. The codebase demonstrates the same
engineering discipline as the core services: every module opens with
a detailed docstring explaining the "why", dependency injection is
used consistently, Decimal is used for all money quantities, PIT
correctness is enforced (settlement uses only post-decision-time
prices), and frozen Pydantic models with `extra="forbid"` are used
pervasively in `quant_foundry` for audit integrity. Test coverage is
broad — every service has meaningful unit tests with fakes/injections.

Key findings:

- **`quant_foundry`** is the most complex service in the codebase
  (~40 modules, 1783-line gateway facade). It implements a full ML
  governance pipeline with budget guards, HMAC-signed callbacks,
  leakage sentinels, dossier registries, tournament scoring,
  retirement detection, Mixture-of-Experts routing, and promotion
  gates. The design is shadow-only by default — no prediction reaches
  `sig.predict` until a human-approved promotion. The gateway's
  `__init__` is 70+ lines of wiring, which is a lot but justified by
  the number of components.
- **`backtester`** is well-architected with PIT-correct fill ordering
  (orders submitted on bar T-1 fill against bar T), realistic
  transaction costs (sqrt impact model, per-symbol overrides, borrow
  accrual), and a shared position kernel (`fincept_core.portfolio`)
  that guarantees bit-identical results between offline backtests and
  live paper trading. The walk-forward module is 683 lines and
  supports expanding-window CV with purge + embargo gaps.
- **`risk`** is intentionally minimal (3 modules, 119 lines of core
  logic) and library-first. The kill-switch state is in-memory only
  (no persistence), which is documented as acceptable for v1 but a
  Phase H concern. The `check_intent` function is pure given a
  `RiskContext`, making it trivially testable.
- **`portfolio`** is a thin consume-Fill → apply-math → publish
  pipeline. Strategy attribution for fills requires an audit-log
  query per fill (no in-memory cache), which is noted as a potential
  bottleneck.
- **`oms`** supports both a paper simulator (`PaperFiller`) and a
  real Alpaca paper-broker integration. The Alpaca router has a
  submit-then-poll lifecycle with a background poller for pending
  orders. Both routers share the same risk gate. The `main.py` is
  462 lines with two router implementations — could benefit from
  extracting shared structure.
- **`settlements`** is a compact 284-line worker with a clean
  async/sync split. The market-data bridge wraps `quant_foundry`'s
  sync `BarDataAdapter` into the async contract via
  `asyncio.to_thread`, keeping the dependency direction one-way.
- **`strategy_host`** implements a supervisor that reconciles
  on-disk strategy configs against running asyncio tasks. The
  runner tails bars/fills/positions and dispatches strategy hooks.
  Model hot-reload is supported via an `active.json` pointer file
  watched every 30s. The `cancel()` context method is a stub (logs
  only) — documented as TASK-066 territory.
- **`agents`** houses 8+ agent implementations. The `gbm_predictor`
  is the primary ML agent with offline training, online inference,
  hot-reload, and feature-health diagnostics. Optional agents
  (sentiment, regime, news-alpha) gracefully skip startup when their
  API keys are missing. The `news_impact_agent` mutates `sys.path`
  at import time to load the experimental model, which is fragile.

Issues found are itemised per-service below with file paths and line
numbers.

---

## Service: quant_foundry

### Purpose

`quant_foundry` is the safe bridge to external quant ML workers. It
owns the full ML governance pipeline: strict cross-boundary contracts
(schemas), deterministic ID generation, HMAC callback signing,
durable outbox/inbox, budget guards, a leakage/overfit sentinel,
artifact import with hash verification, dossier registration,
shadow prediction settlement, tournament scoring, retirement
detection, Mixture-of-Experts routing, and human-gated promotion.

The ML pipeline flow is:
**training → inference → shadow → settlement → tournament → promotion**

In `local_mock` mode, the full loop runs synchronously. In `runpod`
mode, jobs are dispatched to RunPod serverless endpoints and results
return via signed HMAC callbacks.

### Layout

```
services/quant_foundry/
  pyproject.toml
  src/quant_foundry/
    __init__.py                    # Public surface: make_idempotency_key, sign_callback, verify_callback
    schemas.py                     # Cross-boundary Pydantic contracts (frozen, extra="forbid")
    ids.py                         # ID + idempotency key generation, payload hash
    signatures.py                  # HMAC callback signing/verification + replay protection
    gateway.py                     # 1783-line facade wiring all components
    outbox.py                      # Durable job outbox (JSONL per job)
    inbox.py                       # Durable callback inbox (JSONL per callback)
    callbacks.py                   # Callback processor + durable shadow/dossier stores
    mock_dispatcher.py             # Local mock proving the Fincept↔worker loop
    runpod_client.py               # RunPod HTTP client + dispatcher + budget guard
    runpod_training.py             # RunPod training worker handler
    real_trainer.py                # Real training logic
    real_inference.py              # Real inference logic
    alpha_genome.py                # Alpha Genome Lab recipe sweep
    budget.py                      # BudgetGuard: monthly GPU spend tracking + kill switch
    artifacts.py                   # Pull-based, hash-verified artifact import (file://, s3://)
    dossier.py                     # DossierRecord + DossierStatus + DossierBuilder
    registry.py                    # Durable, immutable dossier registry (append-only JSONL)
    sentinel.py                    # Leakage & Overfit Sentinel (shuffled labels, time-reversed, PBO)
    training_manifest.py           # Operator-facing training dispatch contract
    local_training_dispatch.py     # Staging pipeline for baseline training jobs
    shadow_inference.py            # Shadow inference dispatch + FeatureSnapshot
    shadow_ledger.py               # Shadow prediction ledger (append-only JSONL)
    shadow_settlement.py           # Orchestrator: store + settle shadow prediction batches
    settlement.py                  # Settlement ledger (filesystem-backed JSONL)
    settlement_sweep.py            # Periodic settlement sweep worker
    outcomes.py                    # SettlementRecord + CostModel (versioned, immutable)
    metrics.py                     # Pure settlement math: realized_return, brier_score, etc.
    market_data_adapter.py         # Sync adapter for fetching close prices (fincept_db.bars + Alpaca fallback)
    tournament.py                  # Model tournament scoreboard (net edge, DSR, calibration, penalties)
    tournament_sweep.py            # Periodic tournament scoring sweep
    leaderboard.py                 # In-memory ranked leaderboard
    leaderboard_expanded.py        # Expanded leaderboard with horizon/regime/cluster slices
    retirement.py                  # Model retirement detection (calibration decay, edge loss, staleness)
    promotion.py                   # Promotion review queue (human approval gate)
    paper_bridge.py                # Shadow→paper prediction bridge (circuit breaker logic)
    baseline_family.py             # LightGBM baseline model family (train → validate → sentinel → package)
    moe_router.py                  # Mixture-of-Experts model router
    causal_graph.py                # Causal graph builder + feature extraction
    callback_metrics.py            # Durable callback rejection-rate store
    feature_snapshot_export.py     # Compact PIT feature snapshot export for shadow inference
    feature_lake.py                # Feature lake (FeatureRow, FeatureValue)
    feature_availability.py        # Feature availability reporting
    dataset_manifest_builder.py    # Dataset manifest construction
    conformal_gate.py              # Conformal prediction gate
    drift_sentinel.py              # Drift detection sentinel
  tests/
    52 test files covering all modules
```

### API Surface

The gateway (`QuantFoundryGateway`) exposes the operator-facing
surface called by `api/routes/quant_foundry.py`:

- `from_env()` — construct from env vars (`QUANT_FOUNDRY_ENABLED`,
  `QUANT_FOUNDRY_MODE`, `QUANT_FOUNDRY_SHADOW_ONLY`,
  `QUANT_FOUNDRY_CALLBACK_SECRET`, `QUANT_FOUNDRY_BASE_DIR`).
- `create_job(...)` — enqueue a training or inference job to the
  outbox; in `local_mock` mode, dispatches synchronously.
- `receive_callback(...)` — verify HMAC signature, record in inbox,
  process the callback (fail-closed on bad signatures).
- `shadow_health()` — returns shadow pipeline health (callback
  rejection rate, settlement lag, pending counts).
- `list_dossiers()`, `get_dossier(...)` — dossier registry reads.
- `leaderboard()`, `expanded_leaderboard()` — tournament results.
- `promotion_queue()` — pending promotion reviews.
- `approve_promotion(...)`, `reject_promotion(...)` — human gates.
- `settle_sweep()` — trigger a settlement sweep.
- `tournament_sweep()` — trigger a tournament scoring sweep.
- `retirement_recommendations()` — model retirement flags.
- `alpha_genome_sweep(...)` — Alpha Genome Lab recipe sweep.

The package `__init__.py` re-exports: `make_idempotency_key`,
`get_placeholder_schema`, `sign_callback`, `verify_callback`.

### Dependencies

```toml
dependencies = ["fincept-core", "pydantic>=2.7", "httpx>=0.27"]
```

`quant_foundry` has no service-level dependencies — it only depends
on the shared `fincept-core` library. The `market_data_adapter.py`
reads from `fincept_db.bars` but imports it lazily to avoid a hard
dependency. `lightgbm` is used in `baseline_family.py` but is not
listed in `pyproject.toml` dependencies — it's expected to be
available in the environment when baseline training is run.

**Gap**: `lightgbm` is imported at module level in
`baseline_family.py:35` (`import lightgbm as lgb`) but is not in
`pyproject.toml` dependencies. If the module is imported in an
environment without lightgbm, it will raise `ImportError`.

### Background Tasks

The gateway does not run its own background tasks — sweeps are
triggered on-demand by the API route or by an external scheduler.
The `settlement_sweep.py` and `tournament_sweep.py` modules are
designed for periodic invocation but are called explicitly.

### Test Coverage

52 test files covering all modules. Notable test files:
- `test_gateway_real_ml_e2e.py` (25KB) — end-to-end ML pipeline.
- `test_gateway_runpod_loop.py` (17KB) — RunPod dispatch loop.
- `test_real_trainer_inference_e2e.py` (33KB) — full train+infer E2E.
- `test_sentinel.py` (25KB) — leakage sentinel checks.
- `test_tournament.py` (27KB) — tournament scoring + gating.
- `test_promotion.py` (22KB) — promotion review queue.
- `test_paper_bridge_integration.py` (30KB) — shadow→paper bridge.

### Configuration

Environment variables (read directly, not via `fincept_core.Settings`):
- `QUANT_FOUNDRY_ENABLED` (default `"false"`) — master switch.
- `QUANT_FOUNDRY_MODE` (default `"local_mock"`) — `"local_mock"` or
  `"runpod"`.
- `QUANT_FOUNDRY_SHADOW_ONLY` (default `"true"`) — shadow-only mode.
- `QUANT_FOUNDRY_CALLBACK_SECRET` (default `""`) — HMAC secret.
- `QUANT_FOUNDRY_BASE_DIR` (default `"reports/quant-foundry"`) —
  durable state root.
- `QUANT_FOUNDRY_SETTLEMENTS_DIR` (default
  `"data/quant-foundry/settlements"`) — settlement ledger root.
- `QUANT_FOUNDRY_CALLBACK_METRICS_DIR` (default
  `"data/quant_foundry"`) — callback metrics root.
- `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, `RUNPOD_BASE_URL`,
  `RUNPOD_TIMEOUT_SECONDS`, `RUNPOD_COST_PER_DISPATCH_CENTS` —
  RunPod client config (runpod mode only).

### Integrations

- **RunPod**: External GPU worker dispatch via `runpod_client.py`.
  HMAC-signed callbacks with replay protection (timestamp skew +
  job_id binding).
- **Alpaca**: Optional fallback market data reader in
  `market_data_adapter.py`.
- **fincept_db.bars**: Primary market data source for settlement.
- **API layer**: `api/routes/quant_foundry.py` and
  `api/routes/quant_foundry_alpha.py` call the gateway facade.

### Key Design Invariants

1. **Shadow-only by default**: `ShadowPrediction` schema enforces
   `authority: shadow_only` and `extra="forbid"` — no trading fields
   (quantity, side, broker) are ever accepted (`schemas.py:148-175`).
2. **Frozen + extra="forbid"**: All cross-boundary payloads use
   `ConfigDict(frozen=True, extra="forbid")` (`schemas.py`).
3. **HMAC callback signing**: `signatures.py` implements signing +
   verification with replay protection (timestamp skew validation +
   job_id binding).
4. **Budget guard**: `budget.py` tracks cumulative monthly GPU spend
   in JSONL, enforces hard ceilings, and has a global kill switch.
5. **Pull-based artifact import**: `artifacts.py` — Fincept fetches
   from allowlisted URIs (`file://`, `s3://`); workers never push.
   Hash-verified, size-limited, content-type validated, quarantine
   staging, path-traversal rejected.
6. **Dossier immutability**: `dossier.py` computes `content_hash`;
   `registry.py` enforces append-only, hash-checked immutability.
7. **PIT settlement**: `settlement.py` uses only prices observed
   after the prediction's decision time; `pending_time` (horizon not
   elapsed) and `pending_data` (market data missing) are distinct.
8. **Leakage sentinel**: `sentinel.py` checks shuffled labels,
   time-reversed features, future-leak injection, purged-fold
   verification, PBO estimates, train/live gap, feature stability.
9. **Human-gated promotion**: `promotion.py` requires human approval
   + evidence packets (dossier, settlement, sentinel receipt).

### Issues

1. **Gateway complexity**: `gateway.py` is 1783 lines with a 70+ line
   `__init__` that wires ~15 components. The lazy-init pattern
   (`_dossier_registry`, `_expanded_leaderboard`, etc. all start as
   `None` and are initialized on first access) makes the control flow
   hard to trace. Consider splitting into sub-facades.

2. **Missing `lightgbm` dependency**: `baseline_family.py:35`
   imports `lightgbm` at module level but it's not in
   `pyproject.toml` dependencies. Importing this module without
   lightgbm installed will crash.

3. **`os` import at bottom of file**: `baseline_family.py:591` has
   `import os  # noqa: E402` at the bottom of the file, used by
   `_package_artifact`. This is fragile — the import should be at
   the top.

4. **Full-file scan for idempotency**: `settlement.py:276-286`
   `_find()` scans all model files for every settlement attempt to
   check idempotency. Documented as acceptable at MVP volumes but
   will not scale.

---

## Service: backtester

### Purpose

The backtester is a deterministic event-driven historical replay
engine. It replays bars from Parquet (or Timescale), simulates fills
against a `SimBroker` with realistic transaction costs, tracks
positions + equity, and produces a typed `BacktestReport` with
Sharpe, drawdown, per-symbol stats, and trade-level detail. It also
supports expanding-window walk-forward validation with purge + embargo
gaps and a GBM strategy adapter that computes OHLCV-derivable features.

### Layout

```
services/backtester/
  pyproject.toml
  src/backtester/
    __init__.py                    # Public: BacktestEngine, BarsDataSource, Blotter, CostModel, SimBroker
    engine.py                      # Main event loop (PIT-correct fill ordering, risk gate, borrow accrual)
    broker.py                      # Fill-against-bar simulator (MARKET, LIMIT, STOP, STOP_LIMIT, partial fills)
    costs.py                       # Transaction-cost simulator (spread, sqrt impact, fees, borrow)
    datasource.py                  # Historical bar replay (heapq.merge across symbols)
    blotter.py                     # Append-only fills + equity curve + rejections
    report.py                      # Performance metrics (Sharpe, drawdown, per-symbol, trades)
    runner.py                      # High-level parquet→engine→report wrapper + run persistence
    strategies.py                  # Baseline strategies (BuyAndHold, MovingAverageCrossover, GBMStrategy)
    gbm_features.py                # OHLCV-derivable feature kit for GBM strategy
    walk_forward.py                # Expanding-window walk-forward evaluation (683 lines)
    ingest.py                      # Vendor bar payload → canonical parquet conversion
  tests/
    12 test files
```

### API Surface

- `BacktestEngine(strategy, datasource, *, broker, blotter, features, risk_settings)` —
  drive a strategy against a historical bar stream; `async run() -> Blotter`.
- `BarsDataSource(symbols, freq, start_ns, end_ns, *, bar_reader)` —
  replay bars in monotonic `ts_event` order; `async replay() -> AsyncIterator[BarEvent]`.
- `SimBroker(cost_model, *, adv_pct)` — stateful fill-against-bar
  broker; `submit(intent) -> Order`, `cancel(order_id) -> bool`,
  `on_bar(bar) -> list[Fill]`.
- `CostModel(...)` — transaction-cost parameters with per-symbol
  overrides; `apply(side, price, quantity, ...) -> (exec_price, fee)`.
- `Blotter(...)` — append-only fills + equity curve + rejections.
- `run_backtest(*, parquet_path, strategy_name, ...)` — end-to-end
  runner; returns `RunResult` with report + blotter + manifest.
- `compute_metrics(blotter, *, bars_per_year) -> BacktestReport`.
- `STRATEGY_REGISTRY` — maps strategy names to classes.
- `make_folds(...)` — pure index math for walk-forward folds.
- `walk_forward_backtest(...)` — top-level walk-forward coroutine.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-db", "fincept-sdk", "risk",
  "pydantic>=2.7", "polars>=1.0", "lightgbm>=4.5", "numpy>=2.0"]
```

Depends on `risk` for the optional pre-trade risk gate
(`risk.checks.check_intent`), `fincept-db` for `read_bars`, and
`fincept-sdk` for the `Strategy` / `StrategyContext` ABCs.

### Background Tasks

None — the backtester is a synchronous/async library invoked by the
API route (`POST /backtest/run`) or CLI (`scripts/run_backtest.py`).
No long-running processes.

### Test Coverage

12 test files:
- `test_engine.py` — event loop, position math, PIT ordering.
- `test_broker.py` — fill rules for all order types, partial fills, TIF.
- `test_costs_v2.py`, `test_costs.py`, `test_borrow_cost.py` — cost model.
- `test_walk_forward.py` — walk-forward CV with purge + embargo.
- `test_gbm_strategy.py`, `test_gbm_features.py` — GBM strategy adapter.
- `test_risk_gate.py` — risk gate integration.
- `test_ingest.py` — vendor payload conversion.

### Configuration

No environment variables — all config is passed explicitly to
constructors. Run persistence goes to `reports/backtests/<run_id>/`.

### Integrations

- **API**: `api/routes/backtest.py` calls `run_backtest()`.
- **Risk**: `risk.checks.check_intent` gates intents when
  `risk_settings` is provided to the engine.
- **Strategy Host**: `strategy_host` imports `build_strategy` from
  `backtester.runner` and the `STRATEGY_REGISTRY`.
- **fincept_core.portfolio**: Shared `apply_fill_to_position` kernel
  guarantees bit-identical position math between backtest and live.

### Key Design Invariants

1. **PIT-correct fill ordering**: Orders submitted on bar T-1 fill
   against bar T (no instant-fill cheating). The `_submitted_this_bar`
   set excludes newly-submitted orders from the broker's first-bar
   fill scan (`engine.py:210-226`).
2. **Realistic costs**: Sqrt-root impact model
   (`impact_bps = coef * sqrt(participation_pct)`), per-symbol
   overrides, volatility-scaled spread, borrow accrual on shorts
   prorated per elapsed interval (`costs.py`).
3. **Shared position kernel**: `engine.py:251` calls
   `fincept_core.portfolio.apply_fill_to_position` — same kernel as
   the live portfolio service.
4. **Risk gate parity**: When `risk_settings` is set, the engine
   runs the same `risk.check_intent` gate as the live OMS
   (`engine.py:83-103`), so backtest results reflect exactly what
   the live OMS would allow.
5. **Walk-forward independence**: Per-fold returns are concatenated
   (not equity/positions carried across folds) — standard academic
   treatment (`walk_forward.py:10-17`).

### Issues

1. **Global `asyncio.Lock` in API**: The `POST /backtest/run` route
   (in `api/routes/backtest.py`, documented in `02-services-core.md`)
   holds a global lock that serializes all backtest runs. Not a
   backtester bug per se, but a scaling concern.

2. **`_bars_per_year_for_freq` hardcoded**: `runner.py:309-317` has
   a hardcoded freq→bars/year map. Custom frequencies fall back to
   the 1m default (525,600), which may produce wrong Sharpe
   annualization.

3. **`os` import at bottom of `baseline_family.py`**: See
   quant_foundry issue #3 — same file, same problem.

---

## Service: risk

### Purpose

The risk service provides pre-trade limit checks and kill-switch
state tracking. It is library-first by design: the OMS imports it
and calls `check_intent` inline so risk decisions add zero RTT. A
separate-process risk service (Phase H) can wrap the same checks
behind a stream consumer without touching this surface.

### Layout

```
services/risk/
  pyproject.toml
  src/risk/
    __init__.py                    # Public: check_intent, RiskContext, build_context, KillSwitchState
    checks.py                      # Pure-logic pre-trade limit checks (kill switch, per-symbol cap, gross cap)
    snapshot.py                    # Build RiskContext from PositionStore + price callable
    state.py                       # In-memory KillSwitchState (fed by AlertEvents)
  tests/
    test_checks.py, test_snapshot.py, test_state.py
```

### API Surface

- `check_intent(intent, *, ctx, settings, last_price) -> RiskCheckResult` —
  pure function; approves/rejects an `OrderIntent` against limits.
- `RiskContext` — frozen dataclass: `notional_by_symbol`,
  `gross_notional`, `kill_switch_engaged`.
- `build_context(*, store, get_price, kill_switch, strategies) -> RiskContext` —
  async helper; reads positions from `PositionStore`, multiplies by
  latest prices.
- `KillSwitchState` — in-memory boolean flag; `apply(event: AlertEvent)`
  flips on `kill_switch_engaged` / `kill_switch_cleared` codes.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-bus", "portfolio",
  "redis>=5.0", "pydantic>=2.7"]
```

Depends on `portfolio` for `PositionStore` (used in `build_context`).

### Background Tasks

None — the risk service is a library. The kill-switch state is
updated by the OMS's alert consumer task (which calls
`KillSwitchState.apply` on each `AlertEvent`).

### Test Coverage

3 test files covering checks, snapshot, and state. Tests use
`fakeredis` and inject price callables.

### Configuration

Limits come from `fincept_core.config.Settings`:
- `MAX_NOTIONAL_USD_PER_SYMBOL`
- `MAX_GROSS_NOTIONAL_USD`

### Integrations

- **OMS**: `oms/main.py` imports `check_intent`, `build_context`,
  `KillSwitchState` and runs the gate on every `OrderIntent`.
- **Backtester**: `backtester/engine.py` imports `check_intent` and
  runs the same gate when `risk_settings` is provided.
- **API**: `api/routes/control.py` publishes kill-switch
  engage/clear alerts that feed `KillSwitchState`.

### Key Design Invariants

1. **Pure check function**: `check_intent` is pure given a
   `RiskContext` — no I/O, no side effects (`checks.py:58-119`).
2. **Kill switch short-circuits**: If `kill_switch_engaged` is True,
   every intent is rejected before any other check runs
   (`checks.py:74-79`).
3. **No reference price = reject**: Intents without a reference price
   (neither `limit_price` nor `last_price`) are rejected with
   `no_reference_price` (`checks.py:82-88`).
4. **Unobservable positions dropped**: `snapshot.py:59-61` drops
   positions with no reference price from the context — conservative
   (softer limit, never tighter).

### Issues

1. **Kill-switch state not persisted**: `state.py:16-25` documents
   that the flag is in-memory only. On OMS restart, the flag resets
   to False. Acceptable for v1 (operator should re-publish), but a
   Phase H concern.

2. **No daily-loss check**: `checks.py:9-10` notes the daily-loss
   check is deferred (needs a realized-P&L tracker that doesn't yet
   exist).

3. **Reduce-and-allow not implemented**: `checks.py:28-31` — the
   `reduced_notional_usd` field on `RiskCheckResult` is not
   populated. The gate is binary approve/reject only.

---

## Service: portfolio

### Purpose

The portfolio service consumes `Fill` events from the bus, applies
the shared position math, and snapshots `Position` events to the
`ord.positions` stream + a Redis hash for fast UI reads. It is the
single writer to positions — no contention to resolve.

### Layout

```
services/portfolio/
  pyproject.toml
  src/portfolio/
    __init__.py                    # Public: PortfolioState, PositionStore, apply_fill
    store.py                       # Redis-backed live position cache (HKEY per strategy)
    state.py                       # In-memory portfolio state + apply_fill helper
    main.py                        # Entrypoint: consume ord.fills → apply_fill → publish ord.positions
  tests/
    test_state.py, test_store.py
```

### API Surface

- `PositionStore(redis)` — async Redis hash wrapper; `put(position)`,
  `get(strategy_id, symbol) -> Position | None`,
  `get_all(strategy_id) -> dict[str, Position]`,
  `known_strategies() -> set[str]`.
- `PortfolioState()` — in-memory `{strategy_id: {symbol: Position}}`;
  `hydrate(store)` loads from Redis on startup.
- `apply_fill(fill, *, state, store, resolve_strategy) -> Position | None` —
  the single entry point for the consume-Fill loop.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-bus", "redis>=5.0", "pydantic>=2.7"]
```

No service-level dependencies — only shared libs.

### Background Tasks

- **Consumer**: `Consumer(ord.fills, group="portfolio")` — the main
  consume-Fill loop.
- **Heartbeat**: `beat_periodically(redis, "portfolio")` — service
  health heartbeat.

### Test Coverage

2 test files: `test_state.py` (apply_fill logic), `test_store.py`
(Redis hash operations with fakeredis).

### Configuration

- `REDIS_URL` (via `fincept_core.Settings`).
- Redis hash layout: `positions:{strategy_id}` (field=symbol,
  value=Position JSON). Strategies index: `portfolio:strategies`.

### Integrations

- **OMS**: Publishes fills to `ord.fills` which the portfolio
  service consumes.
- **Risk**: `risk.snapshot.build_context` reads from
  `PositionStore.get_all` to build the risk context.
- **API**: `api/routes/positions.py` reads from the Redis hash for
  sub-millisecond lookups.
- **WebSocket**: `api/ws.py` tails `ord.positions` for real-time
  dashboard updates.
- **Audit log**: `main.py:52-67` resolves strategy_id by querying
  `fincept_db.audit.read_by_correlation(fill.order_id)`.

### Key Design Invariants

1. **Shared position kernel**: `state.py:83` calls
   `fincept_core.portfolio.apply_fill_to_position` — same kernel as
   the backtester engine, guaranteeing bit-identical results.
2. **Single writer**: The portfolio service is the only writer to
   positions — no optimistic concurrency or version counters needed
   (`store.py:16-18`).
3. **Hydrate on startup**: `PortfolioState.hydrate(store)` loads
   existing positions from Redis so a restarted service picks up
   where the previous instance left off (`state.py:54-58`).

### Issues

1. **Audit-log query per fill**: `main.py:52-67` — strategy
   attribution requires a Postgres audit query per fill (no
   in-memory cache). Documented as a potential bottleneck; an LRU
   cache is deferred.

2. **Out-of-order fills**: `store.py:20-23` notes that out-of-order
   fills (`ts_event` decreasing) are a TASK-074 concern. The math
   is path-independent only for fills processed in order.

3. **No direct test for `main.py`**: The entrypoint/consumer loop is
   not directly tested (only `state.py` and `store.py` have tests).

---

## Service: oms

### Purpose

The OMS consumes `OrderIntent` events, runs the pre-trade risk gate,
and either simulates fills (paper `PaperFiller`) or routes to Alpaca
(paper/live). It maintains the order state machine, publishes state
transitions + fills to the bus, and appends everything to the audit
log.

### Layout

```
services/oms/
  pyproject.toml
  src/oms/
    __init__.py                    # Public: PaperFiller, LivePrices, can_transition, process_intent
    main.py                        # Entrypoint with sim + Alpaca routing (462 lines)
    processor.py                   # Pure OrderIntent → (Order_states, Fill?) pipeline
    paper.py                       # Paper-trading fill simulator (spread + fee + Gaussian latency)
    state.py                       # Order lifecycle state machine (VALID_TRANSITIONS)
    prices.py                      # In-memory latest-price cache (LivePrices)
    alpaca/
      __init__.py                  # Public: AlpacaClient, submit_intent, poll_pending_orders
      client.py                    # Thin async REST wrapper (POST/GET/DELETE /v2/orders)
      runtime.py                   # Submit + poll lifecycle (instant poll + background poller)
      symbols.py                   # Symbol-format mapping (BTC-USD ↔ BTC/USD)
      data.py                      # Alpaca market data helpers
      marks.py                     # Alpaca position marks sync
      news_sync.py                 # Alpaca news sync
      sync_runner.py               # Sync runner orchestration
  tests/
    9 test files
```

### API Surface

- `process_intent(intent, *, prices, filler) -> IntentResult` —
  pure synchronous function: runs the OMS state machine, returns
  ordered `Order` snapshots + optional `Fill`.
- `PaperFiller(...)` — simulate venue fills against a live mid price;
  `fill(order, mid) -> Fill`.
- `LivePrices()` — per-symbol latest trade price cache; `update(symbol, price)`,
  `get(symbol) -> Decimal | None`.
- `can_transition(frm, to) -> bool` — order state machine validator.
- `AlpacaClient(http, api_key, api_secret)` — async REST wrapper.
- `submit_intent(intent, *, client, pending) -> IntentResult` —
  POST to Alpaca, poll briefly for instant fills.
- `poll_pending_orders(*, client, pending, on_filled, on_terminal, stop)` —
  background loop for pending Alpaca orders.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-bus", "fincept-db", "portfolio",
  "risk", "redis>=5.0", "pydantic>=2.7", "httpx>=0.27"]
```

Depends on `risk` (for `check_intent`, `build_context`,
`KillSwitchState`), `portfolio` (for `PositionStore`), and
`fincept-db` (for audit logging).

### Background Tasks

- **Price consumer**: `Consumer(md.trades, group="oms")` →
  `LivePrices.update`.
- **Alert consumer**: `Consumer(events.alerts, group="oms")` →
  `KillSwitchState.apply`.
- **Intent consumer**: `Consumer(ord.orders, group="oms")` →
  risk gate → `process_intent` (sim) or `submit_intent` (Alpaca).
- **Alpaca poller** (Alpaca mode only): `poll_pending_orders` —
  periodic query for pending Alpaca orders.
- **Heartbeat**: `beat_periodically(redis, "oms")`.

### Test Coverage

9 test files:
- `test_processor.py` — intent → states → fill pipeline.
- `test_paper.py` — paper fill simulator.
- `test_state.py` — order state machine.
- `test_prices.py` — live price cache.
- `test_alpaca_client.py`, `test_alpaca_runtime.py`,
  `test_alpaca_symbols.py` — Alpaca integration (with `respx`).
- `test_main_handlers.py` — main handler functions.
- `test_news_sync.py` — Alpaca news sync.

### Configuration

- `TRADING_MODE` (must be `"paper"` for v1; `oms/main.py:414-417`).
- `OMS_ROUTER` (`"sim"` or `"alpaca"`; `main.py:425-432`).
- `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_BASE_URL` (Alpaca mode).
- `REDIS_URL`.

### Integrations

- **Redis Streams**: Consumes `ord.orders`, `md.trades`,
  `events.alerts`; publishes `ord.orders`, `ord.fills`.
- **Risk**: Inline `check_intent` gate on every intent.
- **Portfolio**: `PositionStore` for risk context snapshot.
- **Audit**: `fincept_db.audit.append` for every intent, state, fill.
- **Alpaca**: Real broker REST API (paper or live).

### Key Design Invariants

1. **Paper-only enforcement**: `main.py:414-417` raises
   `RuntimeError` if `TRADING_MODE != "paper"`.
2. **Risk gate before fill**: Both sim and Alpaca routers run
   `check_intent` before any fill attempt (`main.py:199-209`,
   `main.py:294-305`). Rejections publish a single
   `Order(status=REJECTED)` with reasons in tags.
3. **Pure processor**: `processor.py` is synchronous with no I/O —
   the only place that touches Redis/DB is `main.py`.
4. **State machine**: `state.py` defines `VALID_TRANSITIONS` —
   illegal jumps are rejected by `can_transition`.
5. **Audit everything**: Every intent, state transition, and fill is
   appended to the audit log (`main.py:85-122`).

### Issues

1. **`main.py` size**: 462 lines with two router implementations
   (sim + Alpaca) that share significant structure. The shared
   audit/publish/risk-gate plumbing could be extracted into a
   common base.

2. **No-mid rejection in paper**: `processor.py:89-95` — if
   `LivePrices` has no mid price for the symbol, the order is
   rejected. Documented as intentional ("fail loudly") but could
   surprise during warmup.

3. **`STOP` not supported in paper**: `paper.py:72-73` notes STOP
   orders are not yet supported in the paper filler (only MARKET and
   LIMIT). STOP_LIMIT falls through to the LIMIT path.

4. **Alpaca poller interval**: `runtime.py:51`
   `DEFAULT_BACKGROUND_INTERVAL_S = 5.0` — polls every 5s for
   pending orders. Rate-limit implications at scale are not
   documented.

---

## Service: settlements

### Purpose

The settlements worker tails `fincept_core.prediction_log` and writes
settlement records to `fincept_core.datasets.SettlementStore`. For
each prediction whose horizon has elapsed, it fetches realized market
data, computes the gross/net return (after v1.default costs), and
calculates the Brier score component. The worker is idempotent —
already-settled predictions are skipped, and `pending_data` records
are retried on subsequent ticks.

### Layout

```
services/settlements/
  pyproject.toml
  src/settlements/
    __init__.py                    # Public: tick, tick_sync, make_async_market_data_source
    worker.py                      # Settlement worker MVP (284 lines)
    market_data_bridge.py          # Bridge from quant_foundry.BarDataAdapter to async contract
  tests/
    test_worker.py, test_market_data_bridge.py
```

### API Surface

- `tick(now_ns, *, predictions_dir, settlements_dir, market_data_source) -> list[SettlementRecord]` —
  async: settle every due prediction in one pass.
- `tick_sync(now_ns, *, predictions_dir, settlements_dir, market_data_source) -> list[SettlementRecord]` —
  sync wrapper for replay fixtures.
- `make_async_market_data_source(bar_adapter) -> Callable` — wraps a
  sync `BarDataAdapter` into the async `market_data_source` contract
  via `asyncio.to_thread`.

### Dependencies

```toml
dependencies = ["fincept-core", "pydantic>=2.7"]
```

Minimal — only `fincept-core`. The `market_data_bridge.py` types
`bar_adapter` as `Any` to avoid importing `quant_foundry` from the
settlements package (dependency direction stays one-way).

### Background Tasks

None — the worker is a `tick` coroutine designed for periodic
invocation by the API's settlements poller or an external scheduler.

### Test Coverage

2 test files: `test_worker.py` (16KB, comprehensive worker logic),
`test_market_data_bridge.py` (bridge behavior).

### Configuration

- `predictions_dir` — where `<agent_id>.jsonl` prediction logs live.
- `settlements_dir` — where settlement records are written.
- v1.default cost model: 5 bps fee + 3 bps spread + 0 bps slippage
  (hardcoded in `worker.py:55-57`).

### Integrations

- **fincept_core.prediction_log**: Reads `PredictionRow` from
  `<agent_id>.jsonl` files.
- **fincept_core.datasets.SettlementStore**: Writes
  `SettlementRecord`s.
- **quant_foundry.BarDataAdapter**: Wrapped via
  `market_data_bridge.py` for market data (the bridge is in the
  settlements package to keep the dependency one-way).
- **API**: `api/settlements_poller.py` calls `tick` periodically.

### Key Design Invariants

1. **Idempotent**: Already-settled predictions (status=`settled`)
   are skipped. `pending_data` records are retried but not
   duplicated (`worker.py:211-213`, `worker.py:221-224`).
2. **PIT correctness**: Only predictions whose horizon has elapsed
   (`ts_event + horizon_ns <= now_ns`) are settled
   (`worker.py:91-92`).
3. **pending_data vs settled**: Missing market data produces
   `pending_data` (not an error); the next tick retries
   (`worker.py:220-225`).
4. **Cost model versioning**: Records carry `cost_model_version`
   (`DEFAULT_COST_MODEL_VERSION`); re-settling with a different
   version appends a new record (history preserved).

### Issues

1. **Hardcoded cost model**: `worker.py:55-57` — fee/spread/slippage
   are hardcoded constants. Changing the cost model requires a code
   change, not a config change.

2. **Full-file scan for idempotency**: `worker.py:96-117`
   `_existing_status` scans the agent's entire ledger for the most
   recent matching record. Acceptable at MVP volumes but won't scale.

3. **No background process**: The worker is a single `tick` — no
   long-running process. Periodic invocation is the API's
   responsibility.

---

## Service: strategy_host

### Purpose

The strategy host is the live runtime for `StrategyConfig` instances.
A `Supervisor` reconciles the on-disk `StrategyConfigStore` against
running asyncio tasks: starts a runner when a config flips
`enabled=True`, cancels it when disabled, and restarts it when
runtime-relevant fields change. Each runner tails `md.bars.1m` /
`ord.fills` / `ord.positions`, dispatches strategy hooks, and
publishes `OrderIntent`s to `ord.orders`.

### Layout

```
services/strategy_host/
  pyproject.toml
  src/strategy_host/
    __init__.py                    # Public: Supervisor, run_strategy, LiveStrategyContext
    main.py                        # Entrypoint: Redis + Supervisor + heartbeat
    supervisor.py                  # Reconciles StrategyConfigStore against running tasks (292 lines)
    runner.py                      # Per-strategy live runtime (481 lines)
    runtime.py                     # LiveStrategyContext: per-strategy StrategyContext impl
    model_resolver.py              # Resolve model_binding → on-disk model directory
  tests/
    test_supervisor.py, test_runner.py, test_runner_reload.py,
    test_runtime.py, test_model_resolver.py
```

### API Surface

- `Supervisor(store, redis, runner)` — reconciles configs against
  tasks; `run(stop)` is the main loop.
- `run_strategy(config, redis, stop)` — per-strategy runner
  entrypoint; tails streams, dispatches hooks, publishes intents.
- `LiveStrategyContext(...)` — per-strategy `StrategyContext`
  implementation; `submit(intent)` enqueues for async drain,
  `cancel(order_id)` is a stub, `get_feature` is a no-op.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-bus", "fincept-sdk",
  "backtester", "redis>=5.0", "pydantic>=2.7"]
```

Depends on `backtester` for `build_strategy` and the
`STRATEGY_REGISTRY` (strategy class instantiation).

### Background Tasks

- **Supervisor**: `supervisor.run(stop)` — polls
  `StrategyConfigStore` every `poll_interval_sec`, reconciles
  running tasks.
- **Per-strategy runners**: One `run_strategy` task per enabled
  config, each with its own consumer group
  (`strategy_host:<strategy_id>`).
- **Heartbeat**: `beat_periodically(redis, "strategy_host")`.

### Test Coverage

5 test files:
- `test_supervisor.py` (17KB) — reconciliation logic, restart on
  config change, crash recovery.
- `test_runner.py` (26KB) — runner stream wiring, hook dispatch,
  intent publishing.
- `test_runner_reload.py` (18KB) — model hot-reload.
- `test_runtime.py` — LiveStrategyContext.
- `test_model_resolver.py` — model binding resolution.

### Configuration

- `REDIS_URL`.
- `StrategyConfigStore` filesystem path (via
  `fincept_core.strategy_config.get_strategy_config_store()`).
- `MODELS_DIR` (default `./models`) — where model artifacts live.
- `ACTIVE_MODELS_DIR` (default `models/active`) — where promotion
  pointers live.

### Integrations

- **Redis Streams**: Reads `md.bars.1m`, `ord.fills`,
  `ord.positions`; writes `ord.orders`.
- **Backtester**: `build_strategy` + `STRATEGY_REGISTRY` for
  strategy instantiation.
- **Strategy configs**: `fincept_core.strategy_config.StrategyConfigStore`
  (filesystem-backed).
- **Model promotion**: `models/active/<binding>.json` pointer files
  written by `api/routes/models.py` `POST /models/{name}/promote`.

### Key Design Invariants

1. **Runtime signature restart**: The supervisor only restarts a
   runner when runtime-relevant fields change (class_name, symbols,
   params, model_binding) — not on every upsert
   (`supervisor.py:18-31`).
2. **Crash recovery**: If a runner crashes, the supervisor logs the
   exception and restarts it on the next poll tick
   (`supervisor.py:42-48`).
3. **Per-strategy consumer groups**: Each strategy gets its own
   group (`strategy_host:<strategy_id>`) so they don't share message
   offsets (`runner.py:53-59`).
4. **Sync hooks, async publish**: `submit(intent)` enqueues into a
   per-context list; the runner drains and publishes in submission
   order after each hook returns (`runtime.py:26-39`).
5. **Outstanding-order ledger**: The runner tracks
   `order_id → OrderIntent` in memory to attribute fills without an
   audit-log dependency (`runner.py:25-39`).

### Issues

1. **`cancel()` is a stub**: `runtime.py:46-49` — `cancel(order_id)`
   logs the request but does nothing. Documented as TASK-066
   territory. No current strategy uses cancel.

2. **`get_feature` is a no-op**: `runtime.py:51-58` — all current
   strategies compute features themselves. Future strategies needing
   the online feature store will require an implementation.

3. **Single-leader assumption**: `main.py:10-14` — the host is
   single-leader (only one instance should run). Leadership gating
   is deferred; `start.ps1` spawns exactly one.

4. **Outstanding-order ledger doesn't survive restart**: `runner.py:42-50`
   — on restart, in-flight fills are ignored. Quantity correctness
   is preserved (positions rebuilt from `ord.positions` stream), but
   fills from orders submitted by a previous host instance are not
   dispatched to strategy hooks.

5. **`sys.path` manipulation in news_impact_agent**: While not in
   strategy_host itself, the `news_impact_agent` (in `agents`)
   mutates `sys.path` at import time (`main.py:37-38`), which
   affects the strategy_host if it imports the agent.

---

## Service: agents

### Purpose

The agents service houses strategy agents that consume features and
emit `Prediction` events. Each agent is a long-running async process
that reads live features from the online feature store, runs
inference (statistical model, ML model, LLM, etc.), and emits
predictions to `STREAM_SIG_PREDICT`. The orchestrator consumes
predictions, applies regime weighting + consensus, and emits
`Decision` events to the OMS.

### Layout

```
services/agents/
  pyproject.toml
  src/agents/
    __init__.py                    # Public: Agent (abstract base)
    base.py                        # Agent ABC: setup, run (async generator), teardown
    baselines/
      __init__.py                  # Public: LogRegBaseline, fit_logreg_baseline, predict_proba, roc_auc
      logreg.py                    # Stdlib-only logistic regression baseline (no sklearn dep)
    gbm_predictor/
      __init__.py                  # Public: FEATURES, GBMPredictor, load_live
      main.py                      # Long-running entrypoint with hot-reload (735 lines)
      train.py                     # Offline LightGBM trainer (509 lines)
      infer.py                     # GBMPredictor agent + online inference loop
      features.py                  # Feature spec + online lookup (FEATURES, aliases, defaults)
    regime_agent/
      __init__.py
      main.py                      # Long-running entrypoint (FRED polling, 1h cadence)
      rules.py                     # Rule-based regime classifier (risk_off, high_vol, risk_on, neutral)
      fred.py                      # FRED API client
    sentiment_agent/
      __init__.py
      main.py                      # Long-running entrypoint (NewsAPI + Anthropic LLM)
      llm.py                       # LLM sentiment scoring
      news.py                      # NewsAPI client
    news_alpha_predictor/
      __init__.py
      main.py                      # Long-running entrypoint
      train.py                     # News-alpha ML trainer
      infer.py                     # NewsAlphaPredictor inference
      evaluate.py                  # News-alpha model evaluation
      features.py                  # News-alpha feature spec
    information_enricher/
      __init__.py
      main.py                      # Long-running entrypoint (raw info → enriched stream)
      enrich.py                    # Information event enrichment logic
    news_outcome_labeler/
      __init__.py
      main.py                      # Long-running entrypoint (outcome labeling for news events)
      store.py                     # NewsOutcomeStore
    sentiment_features/
      __init__.py
      main.py                      # Long-running entrypoint (sentiment → feature store bridge)
      store.py                     # SentimentFeatureStore
    news_impact_agent/
      __init__.py
      main.py                      # Shadow-mode news-impact signal producer
  tests/
    20 test files
```

### API Surface

- `Agent` (ABC) — abstract base: `setup()`, `run() -> AsyncIterator[BaseModel]`,
  `teardown()`. `agent_id` is class-level.
- `GBMPredictor(model_dir, redis, cadence_s, freq, symbols)` —
  LightGBM directional classifier agent.
- `LogRegBaseline` — stdlib-only logistic regression (sanity-check
  baseline, not production).
- Agent entrypoints: `python -m agents.<name>.main` for each agent.

### Dependencies

```toml
dependencies = ["fincept-core", "fincept-bus", "features",
  "redis>=5.0", "pydantic>=2.7", "lightgbm>=4.5", "numpy>=2.0",
  "polars>=1.0", "httpx>=0.27"]
```

Depends on `features` (for `OnlineStore`), `fincept-bus` (for
`Consumer`/`Producer`), and `lightgbm`/`numpy`/`polars` for ML.

### Background Tasks

Each agent runs as a long-lived async process:
- **gbm_predictor**: Polls OnlineStore at fixed cadence (default 60s),
  scores each universe symbol, publishes `Prediction` to
  `STREAM_SIG_PREDICT`. Hot-reloads model every 30s via
  `active.json` pointer.
- **regime_agent**: Polls FRED (VIX, T10Y2Y, DFF) at 1h cadence,
  publishes `RegimeSignal` to `STREAM_SIG_REGIME` on regime change.
- **sentiment_agent**: Polls NewsAPI, scores via Anthropic LLM,
  publishes `SentimentSignal` to `STREAM_SIG_SENT`.
- **news_alpha_predictor**: Consumes `STREAM_FEATURES_ONLINE`,
  runs ML inference, publishes `Prediction` to `STREAM_SIG_PREDICT`.
- **information_enricher**: Consumes `STREAM_INFO_RAW`, enriches,
  publishes to `STREAM_INFO_ENRICHED`.
- **news_outcome_labeler**: Consumes `STREAM_FEATURES_ONLINE` +
  `STREAM_MD_TRADES`, labels news outcomes.
- **sentiment_features**: Consumes `STREAM_SIG_SENT`, bridges to
  `STREAM_FEATURES_ONLINE`.
- **news_impact_agent**: Consumes `STREAM_INFO_ENRICHED`, produces
  `NewsImpactSignal` to `STREAM_SIG_NEWS_IMPACT`.

### Test Coverage

20 test files covering:
- `test_gbm_infer.py`, `test_gbm_train.py`, `test_gbm_features.py`,
  `test_gbm_feature_health.py`, `test_gbm_hot_reload.py`,
  `test_gbm_shadow.py` — GBM predictor (training, inference,
  features, hot-reload, shadow mode).
- `test_regime_rules.py` — regime classifier.
- `test_sentiment_llm.py`, `test_sentiment_features.py`,
  `test_sentiment_information_stream.py` — sentiment agent.
- `test_news_alpha_predictor.py`, `test_news_alpha_train.py`,
  `test_news_alpha_evaluate.py` — news-alpha predictor.
- `test_information_enricher.py` — information enricher.
- `test_news_outcome_labeler.py` — outcome labeler.
- `test_news_impact_agent.py` — news impact agent.
- `test_logreg_baseline.py` — logistic regression baseline.
- `test_walk_forward_cv.py` — walk-forward CV.
- `test_base.py` — Agent ABC.
- `test_llm_router.py` — LLM router.

### Configuration

- `UNIVERSE` (via `fincept_core.Settings`) — symbols to predict on.
- `FRED_API_KEY` — required for regime_agent (skips startup if unset).
- `NEWSAPI_API_KEY`, `ANTHROPIC_API_KEY` — required for
  sentiment_agent (skips startup if unset).
- `GBM_MODEL_DIR` / `models/active/gbm_predictor.v1.json` — model
  directory for GBM predictor.
- `GBM_RELOAD_POLL_S` (default 30) — hot-reload poll interval.
- `MODELS_DIR` (default `models`), `ACTIVE_MODELS_DIR` (default
  `models/active`) — model artifact paths.

### Integrations

- **Redis Streams**: Publishes to `STREAM_SIG_PREDICT`,
  `STREAM_SIG_REGIME`, `STREAM_SIG_SENT`, `STREAM_INFO_ENRICHED`,
  `STREAM_FEATURES_ONLINE`, `STREAM_SIG_NEWS_IMPACT`. Consumes
  `STREAM_FEATURES_ONLINE`, `STREAM_INFO_RAW`, `STREAM_MD_TRADES`.
- **Features service**: `OnlineStore` for live feature reads.
- **Orchestrator**: Consumes `STREAM_SIG_PREDICT` for consensus +
  decision generation.
- **FRED API**: Macro data for regime agent.
- **NewsAPI**: News articles for sentiment agent.
- **Anthropic API**: LLM sentiment scoring.
- **Alpaca**: News sync via `oms.alpaca.news_sync`.
- **Prediction log**: `fincept_core.prediction_log.PredictionLog`
  for durable prediction records.

### Key Design Invariants

1. **Agent ABC**: All agents implement `setup() → run() → teardown()`
   lifecycle (`base.py`). `agent_id` is class-level for event
   attribution.
2. **Optional agents skip gracefully**: `regime_agent` and
   `sentiment_agent` exit cleanly at startup if their API keys are
   missing — the rest of the stack runs without them.
3. **Hot-reload**: GBM predictor re-resolves the `active.json`
   pointer every 30s and atomically swaps the model. A failed load
   leaves the previous booster running (`main.py:20-27`).
4. **Feature compatibility layer**: `features.py:40-47` maps old
  feature names to new ones (`ret_1m` → `ret_simple_1`, etc.) so
  old model artifacts can still run online.
5. **Shadow predictions**: GBM predictor writes to
   `PredictionLog` (durable JSONL) for settlement + tournament
   evaluation.

### Issues

1. **`sys.path` manipulation in `news_impact_agent`**:
   `news_impact_agent/main.py:37-38` inserts
   `experiments/news-impact-model/src` into `sys.path` at import
   time. This is fragile, not thread-safe, and couples the agent to
   a specific filesystem layout.

2. **Empty `__init__.py` files**: `news_alpha_predictor`,
   `information_enricher`, `news_outcome_labeler`,
   `sentiment_features` all have empty (0-byte) `__init__.py` files
   — no public surface documented. This makes it hard to import
   their components from outside the agent's own `main.py`.

3. **GBM predictor `main.py` is 735 lines**: The entrypoint handles
   model resolution, hot-reload, feature health, prediction log
   writing, feature snapshot export, and the publish loop. Could
   benefit from extracting sub-components.

4. **`PredictionLog` import of private API**: `gbm_predictor/main.py:61`
   imports `_validate_agent_id` (a private function) from
   `fincept_core.prediction_log`. This couples to an internal
   implementation detail.

5. **No test for `regime_agent/main.py`**: The regime agent's
   entrypoint loop is not directly tested (only `rules.py` has
   tests via `test_regime_rules.py`).

---

## Cross-Service Observations

### Dependency Graph

```
agents ──→ features
agents ──→ fincept-bus, fincept-core

backtester ──→ risk, fincept-db, fincept-sdk, fincept-core
strategy_host ──→ backtester, fincept-bus, fincept-sdk, fincept-core

oms ──→ risk, portfolio, fincept-bus, fincept-db, fincept-core
risk ──→ portfolio, fincept-bus, fincept-core
portfolio ──→ fincept-bus, fincept-core

settlements ──→ fincept-core (only)
quant_foundry ──→ fincept-core (only)
```

The dependency graph is acyclic and clean. `quant_foundry` and
`settlements` have the minimal possible dependencies. The
`risk → portfolio` edge is the only cross-service library dependency
in the trading path; `oms` pulls in both.

### Shared Kernels

1. **`fincept_core.portfolio.apply_fill_to_position`**: Used by both
   `backtester/engine.py` and `portfolio/state.py` — guarantees
   bit-identical position math between offline backtests and live
   paper trading.

2. **`risk.checks.check_intent`**: Used by both `oms/main.py` and
   `backtester/engine.py` — guarantees the same risk gate in
   backtest and live.

3. **`fincept_core.schemas`**: All services use the canonical
   `BarEvent`, `Fill`, `Order`, `OrderIntent`, `Position`,
   `Prediction` schemas.

### Event Spine

All inter-service communication is via Redis Streams:
- `md.trades` — market data trades (ingestor → OMS, agents)
- `md.bars.1m` — 1-minute bars (ingestor → strategy_host)
- `ord.orders` — order intents + state transitions (strategy_host → OMS)
- `ord.fills` — fills (OMS → portfolio, strategy_host)
- `ord.positions` — position updates (portfolio → strategy_host, API)
- `events.alerts` — alerts (API → OMS for kill switch)
- `sig.predict` — predictions (agents → orchestrator)
- `sig.regime` — regime signals (regime_agent → orchestrator)
- `sig.sent` — sentiment signals (sentiment_agent → sentiment_features)
- `features.online` — online feature frames (features → agents)
- `info.raw` / `info.enriched` — information events

### Common Patterns

1. **Frozen Pydantic + extra="forbid"**: Used pervasively in
   `quant_foundry` for audit integrity. Less strict in other
   services (e.g., `backtester` uses `frozen=True` but not always
   `extra="forbid"`).

2. **Dependency injection**: All services inject Redis clients,
   price callables, and bar readers — making modules testable
   without I/O.

3. **Append-only JSONL**: `quant_foundry` uses JSONL extensively
   (outbox, inbox, settlement ledger, shadow ledger, callback
   metrics, budget tracking). `fincept_core.prediction_log` uses
   the same pattern.

4. **Heartbeat**: All long-running services (`portfolio`, `oms`,
   `strategy_host`, agents) use `beat_periodically(redis, name)`
   for health monitoring.

5. **Audit trail**: OMS appends every intent/state/fill to
   `fincept_db.audit`. Portfolio reads from audit for strategy
   attribution.
