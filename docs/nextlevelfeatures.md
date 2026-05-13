# Next-Level Features

This backlog turns the current "more features" request into implementation slices that can be proved. The common theme across `README.md`, `docs/SYSTEM_OVERVIEW.md`, `docs/ROADMAP.md`, `docs/datasources.md`, and `docs/uirecommendations.md` is that Fincept already has many package-level and UI surfaces; the next work should generate operator proof before adding more autonomy.

## Selection Rules

- Prefer features that produce receipts, tests, or audit trails.
- Keep execution paper-first until the replay, risk, and route-smoke gates are green.
- Do not add a dashboard panel unless the API response contract and stale/error states are explicit.
- Treat provider/data health as a dependency for strategy enablement, model promotion, and operator recommendations.

## Priority 0: Proof Before Expansion

### 1. Paper-Spine Replay Receipt

**Why now:** The current top proof gap is a deterministic evidence trail for data -> feature -> prediction -> decision -> risk -> order -> fill -> portfolio. Without this, the implemented services are individually plausible but not proven as a trading system.

**Build next:**

- Add a replay command that starts from synthetic or checked-in fixture bars and drives the existing services/contracts through the paper path.
- Emit a dated JSON receipt under `reports/paper-spine/` with input fixture IDs, service versions, stream IDs, decision IDs, risk result, order ID, fill ID, final position, and pass/fail status.
- Add a minimal dashboard/API reference path so operators can locate the latest replay receipt from the system overview or health surface.

**Acceptance criteria:**

- One command runs locally without live broker credentials and exits nonzero on missing events, rejected schema shapes, or mismatched final position.
- The receipt proves at least one accepted order and one risk-rejected order.
- The replay asserts shadow models never publish order-driving signals.
- CI or local preflight can archive the receipt without leaking secrets.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Replay runner | `scripts/`, `services/backtester/`, `services/orchestrator/`, `services/risk/`, `services/oms/`, `services/portfolio/` |
| Shared schemas | `libs/fincept-core/src/fincept_core/` |
| Bus assertions | `libs/fincept-bus/src/fincept_bus/` |
| Receipts | `reports/paper-spine/`, docs link from `docs/SYSTEM_OVERVIEW.md` |

**Dependencies:** Redis/Postgres local stack, existing paper OMS, risk checks, portfolio rollup, and strategy-host enable/disable fixtures.

### 2. Port-8010 Operator Contract Smoke

**Why now:** The dashboard has grown across orders, strategies, markets, research, news, models, predictions, reconciliation, and portfolio builder. A route smoke catches API/dashboard contract drift before demos or manual testing.

**Build next:**

- Add a smoke script that authenticates once against `http://127.0.0.1:8010`.
- Probe `/health`, `/data/sources`, `/data/coverage`, `/data/symbols/search`, `/research/openbb/health`, `/strategies/configs`, `/orders`, `/models`, `/models/promote/active`, `/predictions` or model prediction routes, and `/regime`.
- Store a JSON receipt with status, latency, skipped reason, and response-shape assertions per route.

**Acceptance criteria:**

- A missing optional provider is reported as skipped or degraded, not as an opaque failure.
- Auth, timeout, and response-shape failures are separated in the receipt.
- The command can run after `scripts/start.ps1` and is referenced from dashboard/API verification docs.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Smoke command | `scripts/route-smoke.ps1`, optional Python helper under `scripts/` |
| API shape checks | `services/api/src/api/routes/`, `services/api/tests/` |
| Dashboard type assumptions | `apps/dashboard/src/lib/types.ts`, `apps/dashboard/README.md` |
| Receipts | `reports/route-smoke/` |

**Dependencies:** API auth token helper, local API default port `8010`, stable test data or graceful empty-state assertions.

### 3. Datasource Contract and Coverage Safety

**Why now:** Existing docs call out `venue` / `venue_default` drift, raw coverage errors, sequential coverage reads, and unclear omitted-venue semantics. This should be fixed before more market panels or provider-dependent strategies are added.

**Build next:**

- Choose the public universe shape: either expose both `venue` and `venue_default`, or standardize on `venue_default` and update dashboard types.
- Replace raw coverage exceptions with stable error codes, correlation IDs, and short operator messages.
- Add batch coverage reads with explicit all-venue vs default-venue semantics.
- Persist periodic coverage snapshots so freshness can be trended instead of only sampled.

**Acceptance criteria:**

- Backend response models, dashboard TypeScript types, and `docs/datasources.md` agree on the venue field names.
- `/data/coverage` exposes `availability_pct`, `fresh_pct`, latest timestamp, and stale reason without leaking raw DB/provider exception text.
- Coverage tests include missing DB, stale data, no venue filter, explicit venue filter, and partial-provider degradation.
- The route-smoke receipt includes coverage status and Redis availability.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| API | `services/api/src/api/routes/data.py`, `services/api/tests/` |
| Database reads | `libs/fincept-db/src/fincept_db/` |
| Dashboard markets/provider UI | `apps/dashboard/src/app/markets/`, `apps/dashboard/src/lib/types.ts` |
| Docs | `docs/datasources.md`, `docs/uirecommendations.md` |

**Dependencies:** Existing datasource registry, Timescale bars, Redis health checks, and dashboard Markets route.

## Priority 1: Operator-Useful Intelligence

### 4. Model Validation and Calibration Dossier

**Why now:** The ML lifecycle exists, including train, promote, hot-reload, shadow, predict, and log. The next feature should help an operator decide whether a model is fit to promote or keep in shadow.

**Build next:**

- Generate a model dossier for each candidate with walk-forward summary, holdout metrics, fold dispersion, feature list, calibration bucket table, latest prediction age, active/shadow state, and known data window.
- Add rolling accuracy and Brier-score views by `agent_id`, `model_name`, symbol, horizon, and regime where labels exist.
- Require a dossier link in promotion history before a model can be promoted from shadow to active.

**Acceptance criteria:**

- Promotion UI and API can show whether a model has a current dossier, stale dossier, or missing labels.
- Shadow predictions are scored without publishing to Redis order streams.
- The dossier records data cutoffs and feature names so backtest/live drift is inspectable.
- Tests cover missing metrics, stale prediction logs, and a failed model artifact read.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Agent metrics | `services/agents/`, `libs/fincept-core/src/fincept_core/prediction_log.py` |
| Model API | `services/api/src/api/routes/models.py`, `services/api/src/api/feature_importance.py` |
| Dashboard | `apps/dashboard/src/app/models/`, `apps/dashboard/src/app/predictions/` |
| Reports | `reports/model-dossiers/`, `models/runs/` |

**Dependencies:** Existing prediction log, model registry/promotions, labels from outcome labelers or replay fixtures.

### 5. Strategy Readiness Gate

**Why now:** Strategy configs and lifecycle controls now exist, but the operator needs a clear "can this strategy safely start?" answer based on data, model, risk, and broker readiness.

**Build next:**

- Add a readiness check before strategy start that evaluates required symbols, feature freshness, model binding, risk limits, kill-switch state, paper broker connectivity, and recent route-smoke status.
- Return blocking failures, warnings, override eligibility, and audit fields.
- Surface the check in the Strategies page before enabling a config.

**Acceptance criteria:**

- A strategy cannot start when required market data is stale, kill switch is active, or the model binding is missing.
- Operator overrides are explicit, logged, and limited to warning-level failures.
- The check has deterministic tests for enabled, disabled, stale-data, missing-model, risk-blocked, and broker-unavailable cases.
- Strategy history records the readiness result used at start time.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Strategy config/runtime | `libs/fincept-core/src/fincept_core/strategy_config.py`, `services/strategy_host/` |
| API | `services/api/src/api/routes/strategies.py`, `services/api/src/api/routes/control.py` |
| Data/model/risk inputs | `services/api/src/api/routes/data.py`, `services/api/src/api/routes/models.py`, `services/risk/` |
| Dashboard | `apps/dashboard/src/app/strategies/` |

**Dependencies:** Coverage safety, model dossier status, kill-switch state, and paper broker health.

### 6. Source-Aware Operator Recommendations

**Why now:** The dashboard already has research, news, markets, predictions, and portfolio-builder surfaces. The useful next AI feature is not open-ended chat; it is a constrained recommendation rail that cites source health and refuses to overstate weak inputs.

**Build next:**

- Add an operator recommendation payload that combines coverage freshness, provider health, latest predictions, news/research summaries, portfolio exposure, and risk state.
- Classify each recommendation as investigate, hold, reduce, paper-only test, or blocked.
- Include evidence IDs, source timestamps, confidence caveats, and next check links.

**Acceptance criteria:**

- Recommendations degrade to "insufficient evidence" when provider health, coverage, or model labels are stale.
- No recommendation can place an order directly.
- Each item links to source routes or local receipt IDs used to form the recommendation.
- Tests cover missing provider keys, stale predictions, active kill switch, and conflicting model/news evidence.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Recommendation API | `services/api/src/api/routes/`, possibly a new read-only route |
| Tool calls | `libs/fincept-tools/src/fincept_tools/` |
| Dashboard rail | `apps/dashboard/src/app/`, `apps/dashboard/src/components/` |
| Evidence docs | `docs/agent-ui-analysis/`, `docs/uirecommendations.md` |

**Dependencies:** Datasource health, model dossier status, portfolio/risk state, and structured research outputs.

## Priority 2: Deeper Research and Simulation

### 7. Backtester Fidelity Upgrade

**Why now:** The event backtester is a core research asset. Its next upgrade should make live-paper comparisons more credible, especially around fees, latency, partial fills, risk gates, and attribution.

**Build next:**

- Add configurable latency, partial-fill, spread/slippage, and fee scenarios.
- Simulate the same risk checks used in the paper path.
- Produce attribution by strategy, symbol, feature family, model, and risk rejection reason.

**Acceptance criteria:**

- A replay can compare simulated fills with internal paper fills and, when configured, Alpaca paper fills.
- Risk-gate simulation uses the same rule parameters as the live risk service.
- Reports show gross P&L, net P&L, fees, slippage, rejected notional, turnover, drawdown, and Sharpe.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Backtester | `services/backtester/` |
| Risk rules | `services/risk/`, shared config if introduced |
| Reports/API | `services/api/src/api/routes/backtest.py`, `reports/backtests/` |
| Dashboard | `apps/dashboard/src/app/backtest/` |

**Dependencies:** Paper-spine replay receipt and strategy readiness gate.

### 8. Cross-Asset and Regime Feature Pack

**Why now:** The repo has multiple agents and optional macro/news inputs. Cross-asset features should be added only after source coverage and calibration evidence can prove they improve decisions.

**Build next:**

- Add BTC dominance, ETH/BTC, equity index proxy, macro regime, and news-volume features with clear source dependencies.
- Track feature freshness and missingness by symbol and horizon.
- Compare model performance with and without each feature family.

**Acceptance criteria:**

- Each feature has point-in-time tests and a documented provider dependency.
- Training reports show ablation or feature-importance evidence before the feature influences active strategy sizing.
- Missing optional feature sources produce degraded confidence, not crashes.

**Likely future ownership:**

| Area | Files or modules |
|---|---|
| Feature computation | `services/features/`, `services/agents/*/features.py` |
| Training | `services/agents/gbm_predictor/train.py`, news-alpha training modules |
| Datasource registry | `services/api/src/api/routes/data.py`, `docs/datasources.md` |
| Dashboard | `apps/dashboard/src/app/predictions/`, `apps/dashboard/src/app/models/` |

**Dependencies:** Data heartbeat history, model dossier, and provider health states.

## Explicitly Not Next

- Live-capital execution or live-broker promotion gates.
- FPGA, kernel bypass, sub-100 microsecond latency work, or FIX certification.
- Public marketplace, plugin economy, or multi-tenant auth productization.
- Autonomous order placement from AI recommendations.
- Broad new dashboard pages that do not close a named proof gap.
