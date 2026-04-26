# Phase A · Agents v1 (baseline, non-LLM) — Agent Prompts

**Tasks:** TASK-030 (Agent base), TASK-031 (LightGBM predictor), TASK-032 (regime detector), TASK-033 (cointegration pairs)
**Checkpoint A1:** `gbm_predictor` ≥52% directional accuracy on held-out 3-month test set with p<0.05; regime labels align with manual inspection on ≥3 historical regime transitions; pairs strategy positive Sharpe on 2 yr OOS.

---

## Phase kickoff

```text
You are now implementing the first agents — predictive, regime, and pairs. These are NON-LLM baseline agents. They establish the floor against which every future agent (including LLM-based ones in Phase X) is benchmarked. If a Phase X agent does not beat these, we don't deploy it.

PHASE-SPECIFIC RULES:

1. NO LEAKAGE. Features come from the feature store via PIT joins (TASK-017). If you compute features inline inside the agent, you WILL leak future information. Always go through the store.

2. CALIBRATED, NOT JUST ACCURATE. A model that predicts UP at 80% confidence and is right 80% of the time is calibrated. One that predicts UP at 99% confidence and is right 55% of the time is overconfident — and will blow up position sizing. Track Brier score, calibration curves. Recalibrate via Platt scaling or isotonic regression before production.

3. WALK-FORWARD VALIDATION ALWAYS. Single train/test split is research-grade. Production uses walk-forward (TASK-023). If a model only beats baseline on a single split, it doesn't ship.

4. OUTPUT IS A PREDICTION OBJECT, NOT A TRADE. Agents emit `Prediction` (or `RegimeSignal` etc.) per spec/CONTRACTS.md §3. The orchestrator (Phase O) decides whether to trade. Agents that bypass the orchestrator and submit orders directly are out-of-scope and forbidden.

5. ONE PROCESS PER AGENT. Each agent runs as its own Python process (services/agents/<name>/main.py). Failures isolate. Restarts don't cascade. Do not run multiple agent types in one process.

6. RECEIVE THROUGH BUS. Agents subscribe to streams via fincept_bus.Consumer with a unique consumer-group name (e.g., "agent.gbm_predictor.v1"). They publish via Producer to the appropriate sig.* stream. Direct DB queries for live inference are allowed (read-only).

7. CONFIDENCE GROUNDED IN UNCERTAINTY. Don't fake-quantify. If your model is point-prediction-only, set confidence = a function of historical residuals, not 0.5 hardcoded. If you don't know how confident you are, your confidence is low.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §3 (Prediction, SentimentSignal, RegimeSignal), §7 (Agent base interface).
- spec/ARCHITECTURE.md (cutting-edge components table — note where these baseline agents fit).
- TASK-017 (feature store) — your input pipeline.
- TASK-023 (walk-forward) — your validation pipeline.

WHEN STUCK:
- Model accuracy too low? Check feature drift between train and inference. Are the same features available at inference time as at training time? PIT join required.
- Model accuracy suspiciously high? Lookahead bias is the prime suspect. Audit every feature for "could this have been computed at ts_event?".
- Inference too slow? LightGBM with predict_disable_shape_check=True is faster. Cache the booster object across calls.

Acknowledge by listing the 7 rules. Wait for the first task.
```

---

## TASK-030 prompt — Agent base + lifecycle

```text
Implement TASK-030 — the Agent abstract base class and lifecycle.

This is small but referenced by every agent. Get it right once.

Files:
- services/agents/src/agents/base.py — Agent ABC (already declared in spec/CONTRACTS.md §7; copy verbatim).
- services/agents/src/agents/runner.py — AgentRunner: glue that wires an Agent instance to Producer + lifecycle hooks + signal handlers.

AgentRunner contract:
- async def run(agent: Agent, output_stream: str) -> None
- Calls agent.setup(), then loops: async for sig in agent.run(): producer.publish(output_stream, sig).
- On SIGTERM: cancel the run loop, call agent.teardown(), exit clean.
- On unhandled exception: log with full traceback, call teardown(), exit non-zero.
- OpenTelemetry span around each yield to track per-signal latency.

Files:
- services/agents/src/agents/__main__.py — entrypoint dispatcher: `python -m agents <agent_name>`. Looks up class from a registry (agents.registry).
- services/agents/src/agents/registry.py — global agent registry; agents register themselves via decorator.

Author spec/tasks/TASK-030-agent-base.md, implement.

Verification:
  uv run pytest services/agents/tests/test_runner.py
  # Test: a fake agent yields 3 predictions; runner publishes 3 to a test stream; teardown called once.
```

---

## TASK-031 prompt — LightGBM directional predictor

```text
Implement TASK-031 from spec/tasks/TASK-031-gbm-predictor.md.

Specific landmines:
- Label leakage via feature engineering: if your label is `forward_return_15m` and a feature is `current_close - close_15m_ago`, that's fine. But if a feature is `close - close_in_1m`, you've leaked one bar into the future. Audit every feature.
- Class imbalance: in trending markets, up labels can be 60% of training data. Use `class_weight='balanced'` or oversample, otherwise the model is just predicting the prior.
- Walk-forward training is mandatory. The "simple 80/20" mention in TASK-031 is for the smoke test only — production training pipeline uses TASK-023's WalkForwardRunner.
- Calibration: after training, fit a CalibratedClassifierCV (sklearn) on a held-out validation slice. The booster's predict() returns probabilities, but they're rarely calibrated for tabular. Test Brier score before/after; reject if calibration didn't help.
- Inference latency: load Booster once at agent.setup(). Re-loading per inference adds ~50ms.
- Confidence: confidence = abs(2*prob - 1) (i.e., 0.5 → 0, 1.0 or 0.0 → 1). Maps probability away from 50/50 to a usable [0,1] confidence scale.

Append spec/tasks/TASK-031-gbm-predictor.md and implement.

Acceptance:
- Walk-forward training on 2 years of 1-minute BTC data with 15-bar (=15m) horizon labels.
- OOS directional accuracy ≥ 52%, p < 0.05 vs binomial(0.5, n).
- Brier score < 0.25 on calibration set.
- Inference p99 latency < 100ms on laptop.
```

---

## TASK-032 prompt — Regime detector

```text
Implement TASK-032 — regime detection agent.

Files:
- services/agents/src/agents/regime/main.py — entrypoint.
- services/agents/src/agents/regime/detector.py — HMM-based detector + classifier wrapper.
- services/agents/src/agents/regime/labels.py — manual labels for 3 historical regime transitions used as validation.

Approach (in this order — don't skip):
1. Feature engineering: compute realized vol (5m, 30m, 4h), trend strength (ADX or Hurst exponent), volume profile, autocorrelation of returns. All from feature store.
2. Train a 4-state Gaussian HMM on 2 yr of these features. States get labeled post-hoc by inspecting their characteristics: trend_up, trend_down, mean_revert, high_vol.
3. (Optional) For each emitted regime, also output a confidence based on the posterior probability of the most likely state.
4. Real-time inference: every 5 minutes, fetch latest feature window from store, run HMM forward filter, emit RegimeSignal.

Specific landmines:
- HMM converges slowly; use BaumWelch with 100 iterations + multiple random restarts.
- Regime labels are not given by the model; you assign them by inspection. Document the assignment in the docstring.
- Stickiness: regimes shouldn't flip every 5 minutes. Add a hysteresis / smoothing layer (e.g., only emit a new regime if it persists for 3 consecutive inference cycles).
- Validation: pick 3 known regime transitions in history (e.g., 2022 BTC crash, 2023 banking crisis spillover, 2024 spot ETF approval). Verify your HMM labels them correctly.

Author spec/tasks/TASK-032-regime.md, implement.

Acceptance:
- 3 manually-labeled historical transitions classified correctly.
- Stickiness verified: regime flips < 5x per day on average.
- Latency: < 500ms per inference cycle.
```

---

## TASK-033 prompt — Cointegration pairs strategy

```text
Implement TASK-033 — pairs trading agent.

Files:
- services/agents/src/agents/pairs/main.py — entrypoint.
- services/agents/src/agents/pairs/cointegration.py — pair selection + spread modeling.
- services/agents/src/agents/pairs/signals.py — z-score-based entry/exit signals.

Pair selection (offline, runs nightly via services/jobs):
1. From the universe, take all C(n, 2) pairs.
2. Filter by sector / category similarity (e.g., only pair within {large-cap-crypto}, {US-tech-equity}, etc. — define groupings in config).
3. For each surviving pair, run Engle-Granger cointegration test on 1 yr of daily closes. Keep pairs with p < 0.01.
4. For surviving pairs, fit OU process to the spread (mean-reversion speed κ, equilibrium μ, vol σ).
5. Persist top-N pairs (default 50) with their hedge ratios and OU params to a `pairs_universe` table.

Live signal generation (this agent):
1. For each active pair, compute current spread = price_A - hedge_ratio * price_B.
2. Compute z-score = (spread - μ) / σ (use rolling estimates so they adapt).
3. If |z| > 2: open mean-reversion trade (long the underperformer, short the outperformer, dollar-neutral).
4. If |z| < 0.5: close.
5. If |z| > 4: close (regime broken — don't double down).
6. Emit a Prediction with direction = -sign(z) (mean reverts toward 0), confidence = min(1, |z| / 3).

Specific landmines:
- Cointegration is unstable. Re-test pairs weekly; drop ones that fail.
- Hedge ratio drift: if rolling correlation drops below 0.5 over a 30-day window, mark the pair stale.
- Funding cost matters in crypto perp pairs and equity short borrow. Account for it in expected P&L; agents that ignore it look profitable on paper.
- Don't trade ALL active pairs simultaneously — concentration risk. Cap to top-K by current |z| score (default K=10).

Author spec/tasks/TASK-033-pairs.md, implement.

Acceptance:
- Walk-forward backtest on 2 yr crypto data, 50-pair universe → positive Sharpe net of costs.
- Hedge ratio drift detection unit-tested.
- Live agent emits Prediction events; pair P&L attributable per pair via tags.
```

---

## Phase A1 exit verification

```text
Run the Phase A1 checkpoint validation:

1. GBM predictor:
   uv run python -m agents.gbm_predictor.train --input data/btc_1m_features.parquet --horizon-bars 15
   uv run pytest services/agents/tests/test_gbm_walk_forward.py -v
   # Walk-forward OOS accuracy ≥ 52%, p < 0.05.
   # Brier < 0.25.

2. Regime detector:
   uv run python -m agents.regime.train --input data/btc_5m_features.parquet
   uv run pytest services/agents/tests/test_regime_historical.py -v
   # 3/3 manually-labeled transitions classified correctly.
   # Stickiness check passes.

3. Pairs:
   uv run python -m agents.pairs.select_pairs --universe crypto_top50 --start 2022-01-01 --end 2024-01-01
   uv run fincept walk-forward agents.pairs.signals:PairsAgent \
     --symbols-from pairs_universe --start 2022-01-01 --end 2024-12-31 \
     --train-months 12 --test-months 1 --step-months 1
   # OOS Sharpe > 0 net of costs over the full 2-yr OOS window.

4. End-to-end live test (paper data):
   make dev
   uv run python -m agents.gbm_predictor.main &
   uv run python -m agents.regime.main &
   uv run python -m agents.pairs.main &
   sleep 600  # 10 minutes
   redis-cli XLEN sig.predict   # > 0
   redis-cli XLEN sig.regime    # > 0
   # All three agents producing signals while ingestor is feeding live data.

5. Confirm calibrations:
   uv run python scripts/eval_calibration.py --agent gbm_predictor.v1 --window 7d
   # Reliability diagram: bins should track diagonal within ±5%.

If all five pass, declare Phase A1 COMPLETE. Mark tasks 030–033 as [x]. Add "Checkpoint A1: passed YYYY-MM-DD". Proceed to spec/prompts/phase-O-orchestrator-risk-oms.md.

If any fail, especially the calibration check, do NOT advance — orchestrator's consensus mechanism multiplies miscalibrated confidence into miscalibrated bets.
```
