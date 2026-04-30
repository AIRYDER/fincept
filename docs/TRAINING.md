# Training Workflow

End-to-end guide for getting `gbm_predictor` from "no model" to "trained on real data, beating baseline."

## Status of the system right now

- Bootstrap model is trained on synthetic data (`models/gbm_predictor/model.txt`, AUC ≈ 0.51).
- Synthetic AUC ≈ 0.50 is **expected and correct** — random walks have no edge. The model is deliberately non-predictive; it exists so the agent goes UP and Predictions flow through the orchestrator → OMS → portfolio chain.
- To get real edge, retrain on actual market data captured by the live stack.

## The three-step path

### 1. Bootstrap (already done)

```pwsh
uv run python scripts/build_synth_parquet.py --bars 43200 --out data/synth_bars.parquet
uv run python -m agents.gbm_predictor.train --input data/synth_bars.parquet
```

Produces `models/gbm_predictor/{model.txt,meta.json}`.

### 2. Capture real data (start running today)

In a new pwsh window, alongside the live stack:

```pwsh
uv run python scripts/capture_to_parquet.py
```

This tails `md.bars.1m` and `features.online` from Redis, joins each bar with its FeatureFrame on `(symbol, ts_event)`, and writes batches to `data/captures/<run_id>_b<N>.parquet`.

**Leave it running** — the longer it runs, the more training data you accumulate. Status updates print every ~30 sec showing matched rows + pending rows + orphan counts.

Rough volume estimates with the default universe (`BTC-USD,ETH-USD,SOL-USD`):

| Runtime | Approx joined rows |
|---------|--------------------|
| 1 hour  | ~180 |
| 1 day   | ~4,300 |
| 1 week  | ~30,000 |
| 1 month | ~130,000 |

LightGBM trains fine on 30k rows but more is better, especially across volatility regimes.

### 3. Retrain on real data

After enough capture (recommend **at least 1 week**, ideally a month spanning a non-trivial price move):

```pwsh
# stop the running gbm_predictor first (Ctrl-C its window or restart the stack)
.\stop.bat

# train on all captured batches
uv run python -m agents.gbm_predictor.train --input "data/captures/*.parquet" --horizon-bars 15

# restart with the new model
.\start.bat -WithGbm
```

The trainer overwrites `models/gbm_predictor/{model.txt,meta.json}`. The agent picks up the new model on next start.

A real-data AUC of ~0.55-0.60 on holdout is a healthy crypto microstructure baseline. Above 0.65 deserves suspicion of look-ahead leakage; below 0.52 means the features aren't separating direction.

## Roadmap to "best performing"

The current setup is an honest baseline, not a production-grade research stack. To meaningfully push performance:

### Near-term (high ROI, days of effort)

| # | Task | Why it matters |
|---|------|----------------|
| 1 | **Walk-forward / purged CV** in train.py (TASK-023) | The current 80/20 holdout overstates real-time performance because adjacent rows leak signal. Walk-forward + purging is table stakes for serious time-series ML. |
| 2 | **Capture longer**, retrain on 1+ month of real data | Random-walk bootstrap has no edge by construction. Real data beats the bootstrap easily once you have it. |
| 3 | **Tune `confidence_threshold` and `position_scale`** in the orchestrator | A great model with bad sizing loses money. After retraining, rebalance these against measured AUC. |

### Medium-term (deeper edge, weeks)

| # | Task | Why |
|---|------|-----|
| 4 | **News + sentiment agent** using NewsAPI + Anthropic | Microstructure features have a half-life of seconds; news shocks have a half-life of hours. Different alpha decay = orthogonal signal. Your `FINCEPT_ANTHROPIC_API_KEY`/`FINCEPT_OPENAI_API_KEY`/`FINCEPT_NEWSAPI_API_KEY` slots are ready in `.env`. |
| 5 | **Macro regime agent** using FRED data | Risk-on vs risk-off conditioning. FRED key is parked. |
| 6 | **Cross-asset features** (BTC dominance, ETH/BTC ratio, crypto-equity correlation) | Many crypto returns are explained by BTC; conditioning on BTC features helps disambiguate alt-coin moves. |
| 7 | **Add more agents to consensus** in the orchestrator router | Currently single-agent (`gbm_predictor.v1`). Multi-agent consensus with different time horizons is the real reason the orchestrator exists. |

### Long-term (research-grade)

| # | Task | Why |
|---|------|-----|
| 8 | **Backtester** with realistic fees, slippage, latency | Required before flipping to live. Also enables hyperparameter search without burning paper capital. |
| 9 | **Hyperparameter sweep** with Optuna | LightGBM has 20+ parameters. Tuning matters more than model choice at this scale. |
| 10 | **Stacking / ensemble** GBM + linear baseline + LLM agent | Linear models often outperform tree ensembles in low-noise regimes; the right meta-learner picks the regime. |
| 11 | **Online learning / drift detection** | Crypto regime shifts are real. A model trained 6 months ago may be obsolete. Concept-drift detection signals when to retrain. |
| 12 | **Smart execution** (TWAP/VWAP/POV) in OMS | Even a great prediction loses to slippage if the execution dumps a market order at the open. |

## Key files for reference

| Concern | Path |
|---------|------|
| Feature spec (training contract) | `services/agents/src/agents/gbm_predictor/features.py` |
| Trainer | `services/agents/src/agents/gbm_predictor/train.py` |
| Inference loop | `services/agents/src/agents/gbm_predictor/main.py` |
| Synth bootstrap | `scripts/build_synth_parquet.py` |
| Live capture | `scripts/capture_to_parquet.py` |
| Smoke-test predictor | `scripts/inject_test_prediction.py` |
| Heartbeat / health | `libs/fincept-core/src/fincept_core/heartbeat.py` + `services/api/src/api/routes/services.py` |

## Operational notes

- Adding a new feature to `FEATURES` invalidates every existing model. The trainer writes the feature list into `meta.json`; the agent reads it back at inference. Bumping the list = full retrain.
- `models/gbm_predictor/` is the *only* on-disk artifact the agent reads. To keep multiple model versions around, copy to `models/gbm_predictor.v2/` and switch `GBM_MODEL_DIR` env var.
- The `/services` endpoint marks `gbm_predictor` as expected only when `models/gbm_predictor/model.txt` exists. So the dashboard panel reads 7/7 in dev (no model) and 8/8 once trained.
