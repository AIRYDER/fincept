# Sisyphus Quant/ML Deep Dive — Fincept Terminal

**Date:** 2026-06-22
**Author:** Sisyphus (autonomous audit pass, no runtime started)
**Scope:** Every quant & ML model in the system — what's actually built, what it's used for, what's planned, and the highest-leverage changes to make next.
**Method:** Read-only audit of source, tests, contracts, dashboards, docs, and `experiments/`. Code citations inline.

> **Companion to:** [`Sisyphus_Ultra_Report.md`](./Sisyphus_Ultra_Report.md) (whole-system audit). This report goes deep on the model layer; the Ultra Report covers the rest.

---

## 1. One-paragraph summary

Fincept Terminal runs **seven implemented agents and one experiment** on top of an event-sourced bus and an event-driven backtester. The only agent that has reached "production" status is `gbm_predictor` — a binary-classification LightGBM that predicts 15-minute up/down on price + vol + book features and emits signed `Prediction` events to `sig.predict`. Around it: a sentiment → features bridge (`sentiment_features`), a sentiment-trained news classifier (`news_alpha_predictor`) that runs off an explicit candidate-gate policy, an outcome labeler that turns future returns into training labels in Redis, a regime agent that reads FRED, and an out-of-tree `experiments/news-impact-model` that does historical-analog retrieval but is **not wired into the orchestrator**. The orchestrator's consensus is **naive confidence-weighted mean** and the allocator is **linear with a confidence threshold** — no Kelly, no portfolio-level vol targeting, no cross-sectional ranking, none of the X+ features the roadmap calls for. ML is therefore *real but narrow*: it works, it has a shadow + hot-reload + promotion pipeline that is genuinely careful, and yet it produces one number per (agent, symbol) per minute, which the orchestrator then scales linearly into a single notional. That is the binding constraint on the system's edge today, and it is what this report quantifies.

---

## 2. Inventory: every model and how it gets used

### 2.1 What is implemented in `services/agents`

Source: `services/agents/src/agents/` (8 packages, 17 source files, 13 test files).

| Package | Status | Trainable? | How it gets to a `Prediction` | Where the artifact lives |
|---|---|---|---|---|
| `gbm_predictor` | **Production** | Yes — full trainer + walk-forward CV | Subscribes to `features.online` via `OnlineStore`, predicts per (symbol, cycle) at default 60s cadence | `models/gbm_predictor/` + `models/active/gbm_predictor.v1.json` (hot-reload pointer) |
| `news_alpha_predictor` | **Production** (with promotion gate) | Yes — `train`, `export`, `evaluate` subcommands | Subscribes to `STREAM_FEATURES_ONLINE` with `freq == "sentiment"`, predicts per FeatureFrame | `models/news_alpha_predictor/` + `models/active/news_alpha_predictor.v1.json` |
| `news_outcome_labeler` | Service | No (label generator) | Consumes `STREAM_FEATURES_ONLINE` + `STREAM_MD_TRADES`; writes labeled examples to Redis hash `news_alpha:example:*` + ZSET `news_alpha:pending_labels` | Redis only |
| `news_impact_agent` | Production (shadow only) | No (consumes existing outputs) | Subscribes to news-impact events, surfaces shadow outputs only — explicitly no order path | n/a (in-memory) |
| `regime_agent` | **Production** (optional, FRED-gated) | Rule heuristics | Polls FRED, classifies regime on change, publishes `RegimeSignal` to `sig.regime` | n/a (rule-based) |
| `sentiment_agent` | **Production** (optional, key-gated) | No (LLM scoring) | NewsAPI → LLM scoring → `SentimentSignal` to `sig.sentiment` | n/a |
| `sentiment_features` | **Production** | No (feature bridge) | Subscribes to `sig.sentiment`, rolls sentiment features, publishes to `features.online` (freq=sentiment) | n/a |
| `information_enricher` | **Production** | No (entity resolution) | `STREAM_INFO_RAW` → `STREAM_INFO_ENRICHED` | n/a |

Two more files that are critical to the quant story:

- `agents/gbm_predictor/main.py` — long-running entrypoint with **hot-reload** (Phase D1) and **shadow deployment** (Phase E2).
- `agents/gbm_predictor/features.py` — the canonical feature list with an `FEATURE_ALIASES` compat layer (see §5.1 below).

### 2.2 What is *not* in `services/agents`

- `pairs` (cointegration strategy agent) — listed as a stub in `SYSTEM_OVERVIEW.md §7`, still empty in `services/agents/src/agents/pairs/`.
- `ts_foundation` — TimeFM/Lag-Llama/Moirai wrapper. **Not implemented.** The `spec/BUILD_ORDER.md` Task 063 row is unchecked.
- `execution_rl` — PPO over child-order slicing. **Not implemented.** Task 065 unchecked.
- `research` — nightly Optuna HPO + GP alpha discovery. **Not implemented.** Task 066 unchecked.
- `event_miner` — Task 062 unchecked.
- `agent memory` (chromadb) — Task 060 unchecked.

### 2.3 The out-of-tree experiment

`experiments/news-impact-model/` is **not in the uv workspace** and **not wired into the orchestrator**. It implements:

- `schema.py` — vendor-neutral `HistoricalOutcome` (event + abnormal returns by horizon)
- `data.py` — loaders for JSONL/CSV
- `analogs.py` — historical analog retrieval (text similarity, regime, source quality, recency)
- `training.py` — `optimize_analog_weights()` with leave-one-out and **walk-forward** evaluation
- `evaluation.py` — MAE + directional accuracy per horizon
- `pipeline.py` — facade
- `model.py` — deterministic baseline predictor
- A standalone workbench (`workbench/index.html` + `serve_workbench.py`) on port `8765`

The README is explicit: "This experiment does not size trades, submit orders, or change the live Fincept runtime." It is a research scaffold. The `news_impact_agent` in the main tree is a *consumer* surface; the actual model lives outside.

### 2.4 Backtester (the research tool, not a model)

`services/backtester/src/backtester/` is 11 modules. The GBM strategy lives here, but as a *separate* implementation from `gbm_predictor`:

- `engine.py` — strict PIT event loop (orders from bar T-1 fill against bar T, **never** against the same bar; the `submitted_this_bar` set is the mechanism)
- `datasource.py` — `BarsDataSource` with `heapq.merge` of per-symbol streams
- `broker.py` — `SimBroker` with proper MARKET/LIMIT/STOP/STOP_LIMIT logic, partial fills via `max_participation_pct`, IOC/FOK semantics
- `costs.py` — `CostModel` with **square-root impact** (`impact_coef_sqrt * sqrt(participation_pct)` — the Almgren-Chriss form), maker/taker fees, per-symbol overrides, borrow cost for shorts (`accrue_borrow`)
- `walk_forward.py` — expanding-window walk-forward with `purge_bars` and `embargo_bars`; trains a **fresh LightGBM per fold** (correct academic behavior)
- `strategies.py` — `GBMStrategy` that consumes the trained model + features

**Critical observation:** The walk-forward trainer (`backtester/walk_forward.py::_train_fold_model`) and the production trainer (`services/agents/gbm_predictor/train.py::train_booster`) are **two different functions**, with different LightGBM hyperparameters (e.g., walk-forward uses `metric="binary_logloss"`, `num_leaves=7`; production uses `metric="auc"`, `num_leaves=63`) and different feature handling. See §5.2 for why this matters.

### 2.5 Where models surface to the operator

The dashboard reads the artifact world and exposes it through:

- `/models` and `/models/[name]` — registry list + per-model detail (active/shadow badges, CV/holdout AUC table, feature-importance chart, live predictions card, promote/shadow buttons)
- `/predictions` — per-symbol live prediction stream (filtered by threshold)
- `/news-impact-lab` — explicit shadow-only panel for the experiment
- `/strategies` — strategy config CRUD with model-binding dropdown
- API routes (per `services/api/src/api/routes/models.py`): `GET /models`, `GET /models/{name}`, `POST /models/train` (background spawner), `GET /models/runs`, `POST /models/{name}/promote`, `POST /models/promote/rollback`, `POST /models/{name}/shadow`, `POST /models/promote/shadow/clear`, `GET /models/{name}/feature-importance`, `GET /models/{name}/predictions`, `GET /models/{name}/prediction-stats`

Promotion is **filesystem-backed**: the API writes `models/active/<agent_id>.json` containing `{"model_name": "..."}`, and the agent process polls that file every 30s and atomically swaps its booster. **Shadow** is a separate pointer file (`<agent_id>.shadow.json`) and a separate inference loop with **no producer** (defence-in-depth — see §3.3 below).

---

## 3. How a `Prediction` actually becomes a paper trade

### 3.1 End-to-end data flow for `gbm_predictor`

```text
ingestor (md.bars.1m)
   → features (md.bars.1m → FeatureComputer → features.online,  Redis hash)
   → gbm_predictor (60s cadence, OnlineStore.get_latest(symbol, freq="1m"))
   → GBMPredictor._predict(features) = {direction ∈ [-1,+1], confidence ∈ [0,1], horizon_ns}
   → _publish_loop → producer.publish(STREAM_SIG_PREDICT, Event(type="prediction", payload))
   → orchestrator router (consumes sig.predict, builds ConsensusBuilder per symbol)
   → ConsensusBuilder.consensus(symbol, now_ns) = AgentConsensus(direction, confidence, ...)
   → allocator.target_notional(direction, confidence, cap, threshold)
   → decisions.build_decision_and_intent(symbol, delta_notional)
   → STREAM ord.decisions + ord.orders
   → risk gate (check_intent)
   → OMS paper fill
   → portfolio position update
   → ord.positions → API WebSocket → dashboard
```

**Every prediction also lands at** `data/predictions/<agent_id>.jsonl` via `PredictionLog.append(...)`. The `_publish_loop` and `_shadow_loop` both write to the same log, keyed by `model_name`, so the dashboard can render active-vs-shadow per model without joining against promotion history.

### 3.2 Orchestrator consensus (the binding constraint)

`services/orchestrator/src/orchestrator/consensus.py` is a 123-line file. Its aggregation rule is:

```python
weighted_direction = sum(c.direction * c.confidence for _, c in fresh) / total_conf
avg_confidence     = total_conf / len(fresh)   # MEAN, not sum — adding agents doesn't inflate beyond 1.0
horizon_ns         = mean of all contributors
ts_event           = max of all contributors
```

Staleness drops any prediction whose `ts_event + horizon_ns < now_ns`, with a 5-minute fallback for horizon-less predictions. There is no per-agent weighting, no regime conditioning (despite `regime_agent` existing), no calibration adjustment, no cross-asset hedging. **What consensus receives is what consensus returns**, scaled.

`allocator.py` is 63 lines:

```python
signal = direction * confidence                # in [-1, +1]
if abs(signal) < confidence_threshold: return 0
magnitude = cap_per_symbol * abs(signal)        # linear
return magnitude if signal > 0 else -magnitude
```

**No Kelly, no vol targeting, no correlation-aware sizing.** Task 042 (Kelly), Task 084 (portfolio vol targeting), and Task 083 (cross-sectional ranking) are all still open. `SYSTEM_OVERVIEW §9` already calls out position-aware vs target-aware as a deferred question. The current system is "target-portfolio-aware, not position-aware" — fine for paper, weak for any alpha that depends on diversification or rebalancing.

### 3.3 Shadow deployment: how it actually works (and why it's good)

The shadow slot in `services/agents/gbm_predictor/main.py` is the most carefully engineered part of the agent layer. Four transitions are handled explicitly:

```python
# None  -> None   (most common; no-op)
# None  -> Path   (operator just set a shadow)
# Path  -> None   (operator cleared shadow)
# Path  -> Path'  (operator switched shadow candidate)
```

Three invariants:

1. **The shadow `_shadow_loop` has no `producer` parameter at all.** There is no path — not even an exception path — by which a shadow prediction can land in `STREAM_SIG_PREDICT`. The orchestrator literally cannot see shadow signals.
2. **Build new shadow first.** If `setup()` raises on the new shadow model, the previous shadow keeps serving; the operator's bad promote click is a logged warning, not an outage.
3. **Independent reload.** Active and shadow slots move on different timelines; toggling shadow never disturbs active and vice-versa.

This is genuinely rigorous. Most "shadow" deployments I've seen at this scale are "write to a different log file and call it done." This one is a parallel inference loop with the publication path physically severed. The same `data/predictions/<agent_id>.jsonl` is used for both, so the dashboard can compare them via the `model_name` field without joining the promotion history.

### 3.4 News-alpha: the candidate-gate policy

`news_alpha_predictor/evaluate.py` has a `CandidateGatePolicy` that the production model does not:

```python
DEFAULT_MIN_AUC = 0.52
DEFAULT_MIN_ROWS = 200
DEFAULT_MIN_VAL_ROWS = 40
DEFAULT_MIN_AUC_DELTA = 0.0    # candidate must beat active (or match if 0)
DEFAULT_MAX_AGE_HOURS = 168.0   # one week
```

The evaluator:

- Loads `meta.json` for the candidate and the active model
- Checks: `model.txt` present, `meta.json` present, `rows >= min_rows`, `val_rows >= min_val_rows`, `best_auc >= min_auc`, `trained_at` within `max_age_hours`, candidate AUC must exceed active AUC by `min_auc_delta`
- Emits a `CandidateGateReport` with `approved: bool`, `reasons: list[str]`, and a `promotion_hint` showing the exact POST endpoints

This is the dossier pattern from the Ultra Report's V1.4 — and it exists *only* for news_alpha. `gbm_predictor` has no equivalent gate. See §5.3 for why this is asymmetric.

The evaluation CLI:

```pwsh
python -m agents.news_alpha_predictor.train evaluate \
  --candidate-dir models/news_alpha_predictor_candidate \
  --min-auc 0.52 --min-rows 200 --min-val-rows 40 \
  --max-age-hours 168 \
  --report reports/news_alpha_candidate_report.json
```

The `promotion_hint` field is a small but excellent operator ergonomics detail: it gives the operator the literal `curl`-equivalent to apply promotion without having to read the API docs.

---

## 4. What is *actually planned* for quant/ML

Source: `spec/EDGE_ROADMAP.md`, `spec/BUILD_ORDER.md`, `docs/TRAINING.md`, `nextlevelfeatures.md`, `SYSTEM_IMPROVEMENT_REPORT.md`.

### 4.1 The strategic thesis (EDGE_ROADMAP)

The EDGE_ROADMAP is unusually honest. Section 1 states the thesis:

> **Goal:** Net-of-cost Sharpe ≥ 1.5 with max drawdown ≤ S&P 500 over rolling 3-year windows...
>
> **Brutal truth:** ~80% of professional active managers fail to beat the S&P over 10+ years, net of fees. The base rate for systematic firms reaching durable Sharpe > 1.5 net of cost is low.

It then enumerates **where edges exist at this scale** and **where they don't** — and the "don't" list is exactly what the EDGE_ROADMAP forbids the team from building (sub-ms latency, dark pools, Reddit firehose, mass image sentiment, pure RL portfolio allocation). This is the most important document in the repo for understanding the constraints.

### 4.2 Tiered roadmap

The roadmap is structured in three tiers (X+, Y, Z) beyond the Phase A–X baseline:

**Tier X+ (Phase X+ — Profitability Layer)** — Tasks 080–089 in `spec/BUILD_ORDER.md`:

- 080: Options-flow agent
- 081: Earnings-call transcript LLM agent
- 082: Insider Form 4 + short-interest agents
- 083: **Cross-sectional ranking layer in orchestrator** (the missing piece for any cross-asset edge)
- 084: **Portfolio-level vol targeting** (above Kelly)
- 085: Strategy decay monitor + capacity curves
- 086: Multi-agent LLM debate (bull/bear/judge)
- 087: Sector-rotation overlay
- 088: Correlation-breakdown alerts
- 089: Liquidity stress test

EDGE_ROADMAP calls out: "every alpha decays; without monitoring, allocation continues to dead strategies." The capacity curve is the line between "profitable on $10k paper" and "unprofitable on $10M live."

**Tier Y (Phase Y — Differentiation)** — Tasks 090–096:

- 090: On-chain analytics
- 091: Cross-asset macro regime model (inflation × growth × liquidity)
- 092: Tail-risk hedging budget
- 093: Selective alt-data integration (only after ROI-positive vendor validation)
- 094: Multi-arm bandit strategy allocator (above orchestrator)
- 095: **Online learning / concept drift** (river integration)
- 096: L2 microstructure features (book imbalance, hidden liquidity, flow toxicity)

**Tier Z (Phase Z — Research Frontier)** — Tasks 100–104:

- 100: Options strategies as alpha sources
- 101: Generative scenario simulation
- 102: Graph neural networks (supply chain)
- 103: Causal inference layer (DoWhy / EconML)
- 104: Federated learning

**End-state targets (per EDGE_ROADMAP §6):**

| Phase exit | Net Sharpe | Max DD | Capacity |
|---|---|---|---|
| O | ≥ 0.5 | ≤ 25% | $1k paper |
| X | ≥ 1.0 | ≤ 20% | $1k–$10k paper |
| **X+** | **≥ 1.5** | **≤ 15%** | **$10k–$100k paper / $1k live** |
| Y | ≥ 1.7 | ≤ 12% | $1M+ live |
| Z | ≥ 2.0 | ≤ 10% | $10M+ live |

The targets are not promises; the roadmap says "track honestly; revise when reality disagrees." Worth noting that **Phase X+ is where the model graduates from research tool to profitability bet.** Tasks 083, 084, 085, 088, 089 are not new models — they are governance + portfolio-construction features that turn a model into a portfolio.

### 4.3 The seven principles for new alpha proposals (EDGE_ROADMAP §5)

1. **Causal hypothesis.** "Correlation in backtest" is not an answer.
2. **Capacity estimate.** <$10M AUM = deprioritize.
3. **Marginal cost vs marginal Sharpe.** Compute both before building.
4. **Decay rate.** Older signals decay slower; "novel" alpha gets arbitraged fast.
5. **Orthogonality.** 5th momentum variant = noise; 1st cross-asset macro = Sharpe.
6. **Eval suite first.** No eval = don't understand the problem.
7. **Shadow before live.** 4+ weeks for non-LLM, 8+ for LLM-based.

These are *applied* inconsistently in the existing code: gbm_predictor was built without a causal hypothesis document; news_alpha_predictor got an evaluation suite first. The asymmetry between the two agents reflects principle 6 partly honored.

### 4.4 Calibration and "do-not-build" list

EDGE_ROADMAP §3 enumerates the traps the team is choosing not to fall into:

- Sub-millisecond latency / colocation (Citadel, Jane Street, Jump have won this)
- Twitter/Reddit firehose at scale (signal-to-noise too low; LLM cost too high)
- Sentiment from images/video (token cost vs alpha currently terrible)
- Pure RL for portfolio allocation (sample-inefficient, unstable)
- "1000 features" trap (capacity-bound, multiple-comparison noise)
- Mass-customized indicators with no causal hypothesis
- Mass-scrape every news source (pay for one good vendor or two)

This is a coherent strategy document. The system today is consistent with it; the gap is **the X+ layer** (cross-sectional ranking, vol targeting, capacity, decay monitoring) which is exactly where the binding Sharpe constraint lives.

### 4.5 What `docs/TRAINING.md` says about the current state

The `TRAINING.md` doc is a candid status read:

> Bootstrap model is trained on synthetic data (`models/gbm_predictor/model.txt`, AUC ≈ 0.51). Synthetic AUC ≈ 0.50 is expected and correct — random walks have no edge. The model is deliberately non-predictive; it exists so the agent goes UP and Predictions flow through the orchestrator → OMS → portfolio chain.
>
> To get real edge, retrain on actual market data captured by the live stack.

It gives a concrete path: `scripts/capture_to_parquet.py` tails `md.bars.1m` + `features.online`, joins on `(symbol, ts_event)`, writes batches to `data/captures/`. Rough volume estimates with the default `BTC-USD, ETH-USD, SOL-USD` universe:

| Runtime | Approx joined rows |
|---|---|
| 1 hour | ~180 |
| 1 day | ~4,300 |
| 1 week | ~30,000 |
| 1 month | ~130,000 |

> A real-data mean walk-forward AUC of ~0.55-0.60 is a healthy crypto microstructure baseline. Above 0.65 deserves suspicion of look-ahead leakage; below 0.52 means the features aren't separating direction.

This is calibrated, grounded guidance. The "above 0.65 deserves suspicion" line is the kind of practitioner note that prevents the most common ML-in-finance failure mode (overfit backtest that ships to production and bleeds money).

The `TRAINING.md` "Roadmap to best performing" section mirrors `EDGE_ROADMAP` almost exactly: near-term (capture longer, walk-forward CV, tune sizing), medium-term (sentiment, regime, cross-asset features, expand consensus carefully), long-term (realistic backtester, Optuna sweep, stacking/ensemble, online learning, smart execution). The trained team's view and the spec author's view are aligned. **No new items appear in one that contradict the other.**

---

## 5. Critical ML/quant gaps in the *implemented* code

These are gaps *within what already exists*, not aspirational. Each is grounded in source.

### 5.1 Feature-name drift between trainer and live service

This is the most concerning concrete bug class. The legacy trainer (`services/agents/gbm_predictor/train.py`, line 20) hard-codes:

```python
FEATURES: list[str] = [
    "ret_1m", "ret_5m", "ret_15m", "ret_60m",
    "rv_5m", "rv_30m",
    "mom_z_30m", "mom_z_240m",
    "book_imbalance_1", "spread_bps",
]
```

The live feature service (`services/features/src/features/computer.py` + `transforms/price.py` + `volatility.py` + `cross.py`) emits a **different** vocabulary:

```python
# PriceFeatures.feature_keys → ret_log_1, ret_simple_1, mom_5, mom_15, mom_20, mom_60
# VolatilityFeatures.feature_keys → vol_rs_w, vol_park_w, vol_gk_w for w ∈ {5, 20, 30, 60, 240}
# CrossFeatures.feature_keys → beta_BTC-USD_w, corr_BTC-USD_w for w ∈ {60, 240}
```

The agent knows the two vocabularies disagree. `features.py` has:

```python
FEATURE_ALIASES: dict[str, str] = {
    "ret_1m": "ret_simple_1",
    "ret_5m": "mom_5",
    "ret_15m": "mom_15",
    "ret_60m": "mom_60",
    "rv_5m": "vol_rs_5",
    "rv_30m": "vol_rs_30",
}
DEFAULTABLE_FEATURES: set[str] = {
    "ret_5m", "ret_15m", "ret_60m", "rv_5m", "rv_30m",
    "mom_z_30m", "mom_z_240m", "book_imbalance_1", "spread_bps",
}
```

And the inference path uses `allow_compat_defaults=True` so that missing features default to `0.0` for non-price features. The "long-window and book-derived features may be unavailable in the first minutes of a dev session" defaulting is **forgiving** by design — it lets the agent run before the book feed is warm.

**But:** `mom_z_30m` and `mom_z_240m` have no alias in the current FeatureComputer. The model trained against the legacy `FEATURES` list will receive `0.0` for those two features at inference time, *forever*. That's not a warmup issue — that's a *silent zero-fill on a feature the model considers important*.

Until a recent retrain uses the modern feature vocabulary (or the FEATURES list is reconciled to match what the live service emits), the live model is operating with two permanently-zeroed features. The fix is mechanical: update `FEATURES` in `services/agents/gbm_predictor/features.py` to use the current vocabulary (e.g., `ret_log_1`, `vol_park_5`, `vol_park_30`, `vol_gk_5`, `vol_gk_30`, `beta_BTC-USD_240`) and retrain.

The current code is *defensive* (it skips a symbol rather than emitting a Prediction with NaN values; see `load_live` return-None contract), but defensive against null ≠ defensive against zero. A trained model treats `0.0` for a momentum z-score at hour 23 of a major regime shift as a confident "no signal," not as "missing."

### 5.2 Two parallel training functions that disagree

`services/agents/gbm_predictor/train.py` (production trainer, lines 89–137):

```python
"objective": "binary",
"metric": "auc",
"learning_rate": 0.05,
"num_leaves": 63,
```

`services/backtester/walk_forward.py::_train_fold_model` (research trainer, line 256–264):

```python
"objective": "binary",
"metric": "binary_logloss",
"verbosity": -1,
"num_leaves": 7,
"learning_rate": 0.05,
```

Different metric, different `num_leaves`. The walk-forward backtest is the primary research tool for evaluating whether a model deserves promotion, but it trains a *different* model from what gets promoted. The promotion-evaluation chain is therefore testing one model and shipping another.

A second-order issue: the walk-forward uses a `feature_names` argument and validates it via `require_supported(feature_names)` against `required_window_bars(feature_names, bar_minutes=...)`. The production trainer reads `FEATURES` from `gbm_predictor/features.py` directly. So even if you wanted to keep them in sync, the *vocabulary* they're training against is different. (`gbm_features.py::compute_features` in the backtester is yet a third implementation of feature math.)

This is not a bug; it's DRY-violation debt. The fix is to extract a single `services/agents/gbm_predictor/_training_lib.py` with one `train_binary_lgbm(X, y, **kwargs)` function, and have both call sites consume it.

### 5.3 `gbm_predictor` has no candidate-gate policy; `news_alpha_predictor` does

The asymmetry is real:

- `news_alpha_predictor.evaluate.py::CandidateGatePolicy` enforces `min_auc=0.52`, `min_rows=200`, `min_val_rows=40`, `max_age_hours=168`, plus `min_auc_delta` over the active model.
- `gbm_predictor` has no equivalent. The promotion endpoint writes the active pointer; the next inference cycle picks it up. There is no `evaluate` subcommand, no gate report, no min-AUC check.

The Ultra Report's V1.4 calls this out — and the asymmetry is the *direct* evidence. `news_alpha_predictor` was built more recently and the team learned the gate pattern; `gbm_predictor` was the first production agent and predates that learning.

Fix: copy `CandidateGatePolicy` into a shared module (`libs/fincept-core/src/fincept_core/model_gate.py`) and apply it to `gbm_predictor`. Add `python -m agents.gbm_predictor.train evaluate --candidate-dir <path>` as the standard pre-promotion step. The promotion endpoint should refuse to update the active pointer if the gate report shows `approved=False`.

### 5.4 No calibration discipline

The probability output of a LightGBM classifier is **not** calibrated. LightGBM optimizes ranking (log-loss), not probability accuracy. For a trading system where downstream Kelly-like sizing will be added, uncalibrated probabilities are *worse* than useless — they mislead the sizing into over/under-confidence.

The codebase has the right hooks for fixing this (per-prediction `calibration_tag="gbm.v1"` on every Prediction, the prediction log keyed by model_name, the dossier evaluation pipeline), but no actual calibration step exists. Recommended additions (in order of leverage):

- **Platt scaling** as a post-hoc step on every promoted booster. ~50 lines, runs once per promotion. Stored as `calibration.json` next to `model.txt`.
- **Reliability buckets** (10 quantile buckets of predicted vs actual direction) in the dossier. Surfaces miscalibration visually before it bites.
- **Brier score** alongside AUC in `meta.json`.

### 5.5 Horizon mismatch between trainer defaults and feature vocabulary

Default horizon: 15 bars × 60 seconds = **15 minutes** (`horizon_bars=15`, `bar_seconds=60`).

But the most useful volatility windows in the live `VolatilityFeatures` are 5, 20, 30, 60, 240 — a 30-bar window covers 30 minutes, which is *the training horizon*. A 15-minute-horizon classifier with 5/30-minute vol features is asymmetric: the longer-window vol captures regime-level moves that the 15-minute label cannot reliably decompose.

Either shorten the horizon to 5m and use 5/20 vol windows, or extend to 60m and use 30/60/240 windows. The current 15/30 split is awkward and shows up as low weight on the long-window features in any model introspection.

### 5.6 No concept-drift detection

`TRAINING.md` calls this out as item 11 in the long-term roadmap: "Online learning / drift detection. Crypto regime shifts are real. A model trained 6 months ago may be obsolete."

The current system has no drift detection. The prediction log captures every prediction and could trivially support rolling-window Brier/AUC by `model_name × symbol × horizon × regime_bucket`, but no code reads it back for monitoring. The job exists (`services/jobs/`); the audit script doesn't.

This is the highest-leverage addition short of Kelly sizing. A 30-day rolling Brier score on the active GBM, plotted on the dashboard with a threshold line at `0.27` (the Brier of an uncalibrated 50/50 classifier on a 50/50 base rate), would catch the most common production ML failure before it costs money.

### 5.7 No on-the-fly class-imbalance handling

The label is `sign(forward return) > 0`. Crypto markets have a documented upward drift, but per-bar forward-return distributions are heavy-tailed and not symmetric. The model uses default LightGBM parameters with no `is_unbalance` or `scale_pos_weight`. Combined with the no-default-features-available warmup, the model is more likely to predict "up" with moderate confidence than "down" with moderate confidence — a directional bias that the consensus's mean-of-confidences calculation cannot detect.

### 5.8 No `feature_importance.json` saved by the production trainer

The backtester writes `feature_importance.json`; the production trainer does not. The API endpoint `GET /models/{name}/feature-importance` exists. The data it returns is *either* from the backtester *or* absent for production models.

Fix: in `services/agents/gbm_predictor/train.py::save_artifacts`, after `model.save_model(...)`, write `feature_importance.json`:

```python
gain = model.feature_importance(importance_type="gain")
split = model.feature_importance(importance_type="split")
(out_dir / "feature_importance.json").write_text(json.dumps({
    "gain": dict(zip(feature_names, gain.tolist())),
    "split": dict(zip(feature_names, split.tolist())),
}, indent=2))
```

### 5.9 The orchestrator's `confidence_threshold` is a single global constant

`allocator.py::target_notional` accepts `confidence_threshold: float = 0.1` as a default. There is no per-strategy, per-symbol, or per-regime override surface. A strategy with high in-sample AUC might warrant a 0.05 threshold (trade on weaker signals); a strategy with low confidence might warrant 0.30 (only trade on strong signals). Today's system has no place to express that.

When TASK-042 (Kelly) lands, it will need to consume the same per-strategy overrides — and if the override surface doesn't exist yet, it will need to be invented then. Better to invent it now, while there are no live strategies depending on the global default.

### 5.10 No replay comparison between shadow and active

The shadow model produces JSONL rows tagged with its `model_name`. The active model produces JSONL rows tagged with its `model_name`. Both land in the same `data/predictions/<agent_id>.jsonl`. There is no code that compares them.

A 30-line script that reads the JSONL, groups by `model_name`, joins on `(symbol, ts_event)`, and produces a per-model Brier + directional accuracy comparison would close the operator's main loop: "is my shadow candidate actually better than the active model?" Without it, the operator must click `Promote` to find out, which violates EDGE_ROADMAP principle 7 ("Shadow before live. No new alpha source touches order routing for 4+ weeks").

### 5.11 The `gbm_predictor` trainer supports an 80/20 holdout *and* walk-forward CV — but the default is still holdout

`train.py` line 357–399:

```python
parser.add_argument("--cv-folds", type=int, default=0,
                    help="If > 0, run walk-forward CV... If 0 (default), use the legacy 80/20 holdout split.")
```

`TRAINING.md` says "Use walk-forward CV by default" — but the default CLI flag is still `0`. This is a small thing, but it's a *signal* of the gap between documentation and code. One-line fix: change `default=0` to `default=5`.

### 5.12 The `pairs` agent is a stub and remains so

Listed as a stub in `SYSTEM_OVERVIEW §6` ("`pairs` remains a stub") and again in the dashboard docs. The 7-implemented-agent count is technically correct, but anyone building a "pairs" agent will find an empty directory. The road to a working pairs agent is non-trivial (cointegration requires statistical infrastructure that doesn't exist yet — Johansen test, half-life estimation, hedge ratio estimation) and the EDGE_ROADMAP doesn't elevate pairs as a Tier X+ priority. **Mark it explicitly as a non-goal for now** and stop advertising it.

---

## 6. What would add the most value to the ML/quant layer

Ranked by impact-per-engineer-week. Each item unblocks the next. Cross-references to Ultra Report tiers are in `[brackets]`.

### Tier Q0 — Make the model layer honest (1–2 weeks)

These fix correctness before adding capability.

**Q0.1 Reconcile `gbm_predictor` features with the live feature vocabulary** *(1 day)*

Update `services/agents/gbm_predictor/features.py::FEATURES` to use what `FeatureComputer` actually emits (`ret_log_1`, `vol_park_5`, `vol_gk_30`, `beta_BTC-USD_240`, etc.). Add a tiny `models/gbm_predictor_fixtures.parquet` of synthetic-but-realistic features so the trainer can be smoke-tested. Retrain on the captured real data in `data/captures/`.

Why now: until this is fixed, the model trained on legacy features is silently zero-filling two of its inputs at inference time. The "above 0.65 deserves suspicion of look-ahead" guidance in `TRAINING.md` is meaningless if the inference pipeline receives `0.0` for momentum z-scores it was trained against.

**Q0.2 Extract a shared trainer module** *(2 days)*

Move LightGBM training into `libs/fincept-core/src/fincept_core/_lgbm.py` (or `services/agents/src/agents/_training.py` if you want to keep it agent-side). One `train_binary_lgbm(X, y, *, n_folds=0, purge_bars=0, num_leaves=63, lr=0.05, metric="auc")` function. The production trainer and the backtester's walk-forward trainer both call it.

Why now: the system literally trains two different models for the same task and then promotes the one that was never tested. The Phase X+ gate ("shadow deployment of ensemble beats baseline by Sharpe ≥ +0.5") is impossible to evaluate correctly until the production and research trainers produce comparable artifacts.

**Q0.3 Apply `CandidateGatePolicy` to `gbm_predictor`** *(1 day)*

Promote `CandidateGatePolicy` to `libs/fincept-core/src/fincept_core/model_gate.py`. Add `python -m agents.gbm_predictor.train evaluate --candidate-dir <path>` (mirror `news_alpha_predictor.train evaluate`). Refuse `POST /models/{name}/promote` when the gate report shows `approved=False`. Add `min_auc=0.52` for crypto microstructure (per `TRAINING.md`'s calibration guidance) and a per-agent override.

Why now: the asymmetry between news_alpha (gated) and gbm (ungated) is a recipe for promoting a model with AUC ≈ 0.50 over one with AUC ≈ 0.55 because nothing prevented it.

**Q0.4 Save `feature_importance.json` in the production trainer** *(half a day)*

Add the gain/split dump in `save_artifacts`. Wire `GET /models/{name}/feature-importance` to prefer the dossier's importance over recomputing it from the model.

Why now: the dashboard's per-model detail page already has a feature-importance chart slot; the data is missing for half the production models.

### Tier Q1 — Make the model layer observable (1–2 weeks)

These let operators *see* what the models are doing — a prerequisite for the Tier Q2 portfolio-construction work.

**Q1.1 Calibration dossier per promoted model** *(1 week)*

For each candidate accepted by `CandidateGatePolicy`, produce a `reports/model-dossiers/<name>/<timestamp>.md` containing:

- Walk-forward summary (per-fold AUC, mean/std/min/max)
- Holdout AUC and Brier score
- Calibration buckets (10 quantile buckets of predicted prob vs actual win rate)
- Per-symbol breakdown (per-symbol AUC, per-symbol Brier, per-symbol row count)
- Feature-importance dump (gain + split)
- Data window (training period, label distribution, missing-data fractions)
- Promotion rationale (which metric crossed which threshold)
- Calibration check (Brier + reliability plot reference)

The dashboard model-detail page links to the latest dossier. Promotion is refused if the dossier is missing or stale (>1 week old).

Why now: the `nextlevelfeatures.md` "Priority 1" item 4 calls for exactly this. The dashboard already has the slot; the data is missing.

**Q1.2 Shadow-vs-active comparison report** *(1 week)*

For each agent with a shadow model, run a daily job that joins active+shadow predictions from `data/predictions/<agent_id>.jsonl` on `(symbol, ts_event)`, computes per-symbol Brier and directional accuracy for each, and writes `reports/shadow-vs-active/<agent_id>/<date>.json`. The dashboard surfaces a "shadow is X% better than active" indicator on the model detail page.

Why now: the entire point of shadow deployment is to evaluate candidates without promotion risk. Without the comparison, shadow is just a write target. With it, the operator's promotion decision becomes evidence-based instead of vibes-based.

**Q1.3 Drift detection + capacity curves** *(1 week)*

Add `services/jobs/model_drift.py`:

- Rolling 30-day Brier score per active model, plotted on the dashboard
- Rolling 30-day directional accuracy per symbol, thresholded at the regime's historical mean + 1.5σ
- Alert (`events.alerts`) when Brier crosses 0.27 or per-symbol accuracy drops > 5% week-over-week

Add `services/risk/capacity.py` (per EDGE_ROADMAP X+ Task 085):

- For each model, simulate scaling order size from 0.1× to 10× current and record the resulting slippage
- Surface as a "this strategy survives $X AUM" badge on the model detail page

Why now: EDGE_ROADMAP §1: "every alpha decays." Without monitoring, allocation continues to dead strategies. The Phase X+ gate includes realized vol ≤ portfolio vol target ± 20% — that is a capacity check, and the code doesn't exist yet.

**Q1.4 Class-imbalance + horizon-mismatch fix** *(1 day)*

Add `is_unbalance=True` to the LightGBM params (or `scale_pos_weight=neg_count/pos_count`). Default horizon to 30 (matching the strongest vol window), or document the 15-minute horizon explicitly in the dossier. Either is fine; the asymmetry is the bug.

Why now: 30 minutes vs 15 minutes matters when the longest volatility window is 30 bars. Asymmetric horizons → asymmetric feature weights → asymmetric model behavior. One-day fix.

### Tier Q2 — Make the model layer a portfolio (2–3 weeks)

These are the EDGE_ROADMAP X+ features that turn a model into a portfolio. Without them, the system has a Sharpe ceiling around 1.0.

**Q2.1 Cross-sectional ranking layer (TASK-083)** *(2 weeks)*

Add `services/orchestrator/src/orchestrator/cross_section.py`. After consensus, every cycle: rank all symbols by `(direction * confidence)`, long the top decile, short the bottom decile. Returns become target notionals. The existing `allocator.target_notional` is per-symbol; the cross-sectional layer is per-cycle.

This is the most durable equity edge for 30+ years per EDGE_ROADMAP. Crypto edge is harder (BTC dominance drives correlation) but the same shape applies.

**Q2.2 Portfolio-level vol targeting (TASK-084)** *(1 week)*

Add `services/risk/vol_target.py`. After all per-symbol notionals are computed, scale them so the *portfolio* realized vol targets 10–15% annualized (configurable). When realized vol drops, scale up; when it rises, scale down. This single change smooths the equity curve and improves Sharpe with no new alpha source.

**Q2.3 Strategy decay monitor (TASK-085 partial)** *(1 week)*

Per-model rolling Sharpe with decay detection. When a model's 30-day Sharpe drops below `baseline_sharpe - 1.5σ`, emit an alert and reduce its allocation by 50%. After 7 days without recovery, retire it. The strategy_host already has config + lifecycle; this is the missing metric.

**Q2.4 Kelly-optimal sizing (TASK-042)** *(2 weeks)*

Replace `allocator.target_notional` with a Kelly variant that takes `(direction, confidence, gross_cap, covariance_estimate)` and outputs a per-symbol notional. The covariance estimate can come from a rolling 60-day window of returns. Per EDGE_ROADMAP, this is the Phase O prerequisite for Phase X+; the dependency has been on the critical path for a while.

**Q2.5 Per-strategy and per-symbol confidence thresholds** *(half a day)*

Extend `allocator.target_notional` to read `confidence_threshold` and `cap_per_symbol` from `StrategyConfig` rather than from the global default. Single-place change; closes a 6-month-old TODO.

### Tier Q3 — Cutting-edge quant features (only after Q0–Q2 are green)

The Tier X+ features in EDGE_ROADMAP, prioritized by leverage:

| Task | Effort | Notes |
|---|---|---|
| 086: Multi-agent LLM debate | 3 weeks | Replace single-shot LLM call in news_alpha or sentiment with bull/bear/judge |
| 081: Earnings-call LLM agent | 2 weeks | Reuses sentiment infra |
| 087: Sector-rotation overlay | 2 weeks | Macro-conditioned sector tilts |
| 088: Correlation-breakdown alerts | 1 week | When "uncorrelated alphas" suddenly correlate |
| 089: Liquidity stress test | 1 week | Daily: if I had to exit 50% of book in 1 day, what's the slippage? |
| 080: Options-flow agent | 4 weeks | CBOE LiveVol or OPRA scrape |
| 082: Insider Form 4 + short-interest agents | 4 weeks | Free SEC/FINRA data |
| 094: Multi-arm bandit allocator | 3 weeks | Thompson sampling across strategies (not signals) |
| 095: Online learning / concept drift | 4 weeks | `river` for incremental updates |

### Tier Q4 — Research frontier (only after Phase Y checkpoint passes)

EDG_ROADMAP Tier Z: 100 (options alpha), 101 (GAN/diffusion scenarios), 102 (GNN), 103 (causal), 104 (federated). High variance, durable payoff, whitepaper-required. **Explicitly not next.**

---

## 7. Quant/ML-specific risks (not covered in the Ultra Report)

### 7.1 Look-ahead leakage is the most common failure mode in ML-in-finance

The codebase has good defenses:

- `walk_forward.py` uses `purge_bars` and `embargo_bars` between train and val
- The label is `sign(forward return)` with proper shift
- The backtester engine prevents same-bar fills on strategy-submitted orders
- The price features use only past closes (rolling deques)
- The cross-asset features align by position, not by timestamp (acknowledged as a known approximation in the spec)

But there are subtle leakage vectors:

- **Volatility features** in `VolatilityFeatures` use the current bar's `o, h, l, c` to compute Parkinson and Garman-Klass for *the same bar*. For a label that uses `close[T+H]`, this is fine because we're computing features from bar T's data. But the label's `forward return` is computed at bar T+H, which uses bar T+H's OHLC — *not* bar T's. So there's no leak in the standard case. **Edge case:** if the trainer is given a parquet where `bar.ts_event` is bar-open-time and the close is bar-close-time, the current bar's close is "the future" relative to bar-open. Verify with `services/ingestor/src/ingestor/writer.py` how bar timestamps are assigned.
- **`ret_log_1`** uses `c0 / c_prev - 1`. If `c_prev` is null (warmup), this returns None. The trainer's `build_dataset` drops nulls. No leak.
- **Cross-asset features** align by *position* in the deque, not by `ts_event`. Two symbols on different venues might have drifted timestamps. EDGE_ROADMAP-spec landmine #5 acknowledges this. The leak risk is small (a minute-scale misalignment), but it should be measured.

### 7.2 Survivorship bias in feature work

When retraining on real captured data (`data/captures/*.parquet`), the only symbols present are the ones the system was running for. If the system was running for `BTC-USD, ETH-USD, SOL-USD` and one was delisted, the model never learns from it. **Always include `metadata.captured_symbols` in the dossier** so future-you knows which universe the training data represents.

### 7.3 Regime-conditional strategies are the system's main opportunity *and* its main trap

`regime_agent` exists and publishes to `sig.regime`. But the orchestrator's consensus does not read regime. If a model performs well in `risk_on` and poorly in `risk_off` (which is the common case), the current system treats every regime the same. **Either condition the consensus on regime** (good — Tier Q2 work) or document the regime blindness explicitly in the dossier (acceptable for now, but it should not be silent).

### 7.4 Latency budget for live inference

The agent's `cadence_s = 60.0` default. Per-cycle work: read `OnlineStore` for N symbols, run LightGBM prediction on a single-row matrix. LightGBM inference on a single row is fast (<1 ms for 10 features). Redis read is <10 ms. Total: well under 100 ms. **No latency issue at current scale.**

Where it could become one: if feature vocabulary grows to 100+ features per symbol with 50+ symbols and a 10-second cadence, the per-cycle budget becomes 5 ms × 50 = 250 ms. Still fine, but worth measuring before adding 50 symbols.

### 7.5 The `1.0` confidence saturation

When `prob_up ≈ 1.0` (the model is "very sure up"), `direction = 2*1.0 - 1 = 1.0` and `confidence = |1.0| = 1.0`. LightGBM probabilities never quite hit 1.0 in practice (they're calibrated to the training distribution, and the leaf values are bounded), but the absolute certainty in either direction is misleading: the model is "certain up" relative to a fixed training distribution that has itself drifted.

A fix: squash confidence through a temperature-scaled softmax before publishing. `confidence = sigmoid(logit / T)` for some temperature `T > 1`. This makes extreme confidence rarer and gives the consensus more honest uncertainty to work with.

### 7.6 LLM cost vs marginal alpha (EDGE_ROADMAP principle 3)

When the LLM-sentiment agent runs, every news article costs Anthropic or OpenAI tokens. The current code doesn't track this. Add `usage_count` and `cost_estimate` fields to the SentimentSignal, surface as an LLM-cost-per-prediction metric on the dashboard. If the cost-per-prediction exceeds a configurable ceiling (default $0.01), the agent should skip inference rather than run. **This is a budget feature, not a model feature.**

---

## 8. How to use this report together with `Sisyphus_Ultra_Report.md`

The two reports are designed to be read together:

- `Sisyphus_Ultra_Report.md` covers the whole system: architecture, safety, dashboard, docs, deployment, value-add prioritization across the entire stack.
- This report (`Sisyphus_Quant_ML_Deep_Dive.md`) covers the model layer in depth: what's implemented, what's not, what's planned, and the specific ML/quant gaps and value-adds.

**The Tier Q0 items in this report should land in parallel with the Tier 0 items in the Ultra Report**, because both are correctness-and-trust work that has to be done before anything else compounds on top of it. The Tier Q2 items (cross-sectional ranking, vol targeting, Kelly, decay monitor) *are* the Ultra Report Tier 2 V2.x items (backtester fidelity + calibration dossier + provider ledger), but framed for the ML layer specifically.

If you have 2 engineers and 4 weeks: have one engineer do Ultra Report Tier 0 (runtime safety matrix, route smoke, paper-spine replay, verification receipt) and have the other do Quant Report Tier Q0 (feature reconciliation, shared trainer, candidate-gate policy, feature importance). They are independent. After both land, the platform is ready for Tier 1 / Q1 work.

---

## 9. The 60-second quant/ML TL;DR for a new engineer

If you are new to Fincept and want to understand the model layer in 60 seconds:

1. **Seven agents run today.** Only `gbm_predictor` and `news_alpha_predictor` are first-class production agents that emit live predictions to the orchestrator. The others are feature-bridging, label-generating, or context-providing services.
2. **The training pipeline is the one in `services/agents/gbm_predictor/train.py`.** It supports a legacy 80/20 holdout (`--cv-folds 0`) and an expanding-window walk-forward with purge gap (`--cv-folds N`). **The default is the holdout; change it to 5.**
3. **Models are promoted via filesystem.** `POST /models/{name}/promote` writes `models/active/<agent_id>.json`. The agent polls that file every 30s and atomically swaps the in-memory booster. Shadow uses a separate pointer and a parallel inference loop with the publication path physically severed.
4. **Predictions land twice.** Once on `STREAM_SIG_PREDICT` (live) and once in `data/predictions/<agent_id>.jsonl` (auditable, per-model). The shadow loop writes only to JSONL.
5. **The orchestrator's consensus is a naive confidence-weighted mean.** No regime conditioning, no per-source weighting, no calibration adjustment. **This is the binding constraint on edge.** The allocator is linear with a confidence threshold. There is no Kelly sizing, no portfolio-level vol targeting, no cross-sectional ranking.
6. **The walk-forward backtester trains a *different* LightGBM than the production trainer** (different `num_leaves`, different `metric`, different feature vocabulary). The promotion chain is therefore testing one model and shipping another until Q0.2 lands.
7. **The `gbm_predictor` features in code do not match what the live feature service emits.** A retrained model against the current feature vocabulary is required before any meaningful live AUC can be reported.
8. **`news_alpha_predictor` has a `CandidateGatePolicy`** with `min_auc=0.52`, `min_rows=200`, `max_age_hours=168`. `gbm_predictor` has no equivalent. Asymmetry.
9. **The "above 0.65 deserves suspicion of look-ahead leakage" guidance** in `docs/TRAINING.md` is the most important calibration note in the codebase. Trust it.
10. **The strategic thesis lives in `spec/EDGE_ROADMAP.md`.** Read it before doing anything quantitative. It tells you what *not* to build (sub-ms latency, Twitter firehose, image sentiment, pure RL allocation) and what to build instead (cross-sectional ranking, vol targeting, capacity curves, decay monitoring, sector rotation, sentiment ensemble).

If you take only one thing away from this report: **the system is real but narrow.** The ML works. The shadow + hot-reload + promotion pipeline is genuinely careful. The orchestrator's consensus is the binding constraint on edge. Tier Q0 + Q1 fixes the foundation. Tier Q2 unlocks the Sharpe ceiling. Tier Q3 is the X+ layer. Tier Q4 is research and explicitly not next.
