# Phase X · Cutting Edge — Agent Prompts

**Tasks:** TASK-060 (vector memory), TASK-061 (LLM sentiment), TASK-062 (event miner), TASK-063 (TS foundation model), TASK-064 (LLM orchestrator loop), TASK-065 (RL execution), TASK-066 (research agent — HPO + alpha discovery)
**Checkpoint:** Shadow ensemble (gbm + ts_foundation + llm_sentiment) for 4 weeks must beat baseline by Sharpe ≥ +0.5 after costs, p<0.05.

This is where the firm's edge gets built. Most retail competitors stop at Phase A. The differentiation is here.

---

## Phase kickoff

```text
You are now implementing the cutting-edge agents — LLM-based sentiment, time-series foundation models, RL execution, and the orchestrator's tool-use loop. These are the components that give the system a real, durable edge over commoditized retail stacks. They are also the most expensive to run, the most likely to drift, and the most dangerous when they hallucinate.

PHASE-SPECIFIC RULES:

1. SHADOW BEFORE LIVE. No Phase X agent's output influences orders for at least 4 weeks of paper-trading shadow deployment. Period. The orchestrator (TASK-064) routes their signals only when explicitly enabled per-agent. New agents start in shadow with weight=0.

2. CALIBRATION OVER PRECISION. An LLM-based agent that's 60% accurate but well-calibrated beats one that's 70% accurate but overconfident. Confidence has to mean something downstream — orchestrator weights by confidence, sizing scales with confidence. Garbage-in-confidence = garbage-out-position.

3. COST CONTROL IS PRODUCT WORK. Every LLM call is real money. Implement aggressive caching (semantic dedup via vector memory), prompt minimization, and model tiering (cheap model for filter, expensive for detailed extraction). A useful agent that costs $50/day is OK. One that costs $5,000/day for the same signal is broken.

4. STRUCTURED OUTPUTS ONLY. LLMs always return JSON matching a schema. Use OpenAI's `response_format={"type":"json_object"}` or Anthropic's tool-use API. Free-text responses are forbidden in production paths.

5. TOOL-USE OVER PROMPT-STUFFING. The LLM orchestrator (TASK-064) calls typed tools from libs/fincept-tools (TASK-005) rather than reading raw streams. Tools enforce contracts. Stuffing JSON into a context window invites hallucinated edits.

6. NO RECURSIVE TOOL CALLS WITHOUT BUDGETS. The agent loop calls a tool, gets a result, decides next action. Cap at 8 iterations + 30s wallclock per decision cycle. Otherwise an agent that hallucinates a perpetual problem will burn credits forever.

7. EVALS ARE PRODUCTION CODE. Every LLM agent has a regression suite of (input, expected_output) examples. Re-run on every model upgrade. A new model that fails the eval suite does not deploy regardless of marketing-claimed quality.

CONTEXT TO LOAD:
- spec/CONTRACTS.md §3 (signal types), §8 (Tool protocol).
- libs/fincept-tools (TASK-005) — your tool registry.
- TASK-040 orchestrator (consumes your output).
- TASK-031 GBM predictor (the baseline you must beat).
- The agent's specific spec (TASK-061 etc.) when implementing it.

WHEN STUCK:
- LLM hallucinating tickers? Resolve via fincept_sdk.universe.load + entity.resolve. If model invents a symbol not in universe, drop the signal.
- RL agent diverging? Reduce learning rate, increase batch size, check reward function for unintended optima (e.g., it learned to do nothing because that's positive-sum vs random trading after costs).
- Foundation model giving identical predictions across all symbols? Likely you fed it concatenated multivariate data and it averaged. Run per-symbol and verify.
- LLM costs spiraling? Profile token usage: input tokens × calls × price. The fix is almost always: dedup (memory), filter (cheap model first), or shorten (truncate aggressively).

Acknowledge by listing the 7 rules. Wait for the first task.
```

---

## TASK-060 prompt — Vector memory (chromadb)

```text
Implement TASK-060 — vector memory for LLM agents.

Files:
- services/agents/src/agents/memory.py — VectorMemory class.

VectorMemory contract:
- __init__(redis: Redis, namespace: str)  # namespace per agent so memories don't cross-pollinate
- async def setup() -> None  # initialize chromadb client, persistent collection at data/chroma/{namespace}/
- async def remember(key: str, text: str, ttl_s: int | None = None, metadata: dict | None = None) -> None
- async def seen(key: str, text: str, similarity_threshold: float = 0.92) -> bool  # has a SIMILAR text been seen?
- async def query(text: str, top_k: int = 5) -> list[dict]  # for retrieval-augmented prompts later

Specific landmines:
- Embeddings: use sentence-transformers all-MiniLM-L6-v2 by default (fast, runs on CPU). Document how to swap to OpenAI text-embedding-3-small if better accuracy needed.
- chromadb.PersistentClient(path=...) — DO NOT use in-memory; restart wipes deduplication.
- TTL: chromadb has no native TTL. Implement via a periodic sweep (jobs/) or by marking metadata with expiry timestamp and filtering on query.
- similarity_threshold: tune per agent. Sentiment: ~0.92 catches paraphrases. Code-events: ~0.97 stricter.
- Performance: batch upserts (100 at a time) when bulk-loading historical data.

Author spec/tasks/TASK-060-vector-memory.md, implement.

Verification:
  uv run pytest services/agents/tests/test_memory.py
  # Round-trip: remember "Apple beats earnings" then seen("Apple Inc. beat earnings expectations") returns True at threshold 0.92.
  # seen("BTC drops 10%") returns False (different topic).
```

---

## TASK-061 prompt — LLM sentiment agent

```text
Implement TASK-061 from spec/tasks/TASK-061-llm-sentiment.md.

Specific landmines (in addition to those in the task spec):
- News firehoses repeat. The vector memory (TASK-060) is essential — without it you'll re-process the same article 10x.
- Article body truncation: cap user prompt at 4000 tokens of article body. Most signal is in title + lead paragraph.
- Sentiment ≠ price direction. "Stock crashed on earnings miss" → score=-0.8, but if the price already crashed before the article, the signal is stale. Always include `published_at` in the SentimentSignal so orchestrator can decay older signals.
- Symbol resolution: hallucination risk is HIGH. The model loves to attribute generic news to specific tickers. Use entity.resolve strictly; reject signals for symbols not in universe.
- Eval suite: build at minimum 50 (article, expected_signal) pairs hand-labeled across event types. Run before any model swap.
- Cost guard: track tokens per call in OTel. Alert if daily cost > $20 in MVP. Production budget is per-deployment.

Append spec/tasks/TASK-061-llm-sentiment.md and implement.

Acceptance:
- Eval suite: ≥80% precision on labeled set (precision matters more than recall — false positives are noisy alpha).
- Daily cost on default settings: < $5/day in MVP universe of 5 symbols.
- Published signals → orchestrator → impact tested in shadow only (TASK-064 wires the routing).

Verification:
  uv run pytest services/agents/tests/test_llm_sentiment_eval.py
  uv run python -m agents.llm_sentiment.main &
  sleep 3600   # 1 hour observation
  redis-cli XLEN sig.sentiment
  # Should be > 0 if news flowed; sanity-check entries with `redis-cli XRANGE sig.sentiment - + COUNT 5`.
```

---

## TASK-062 prompt — Event miner (real-time event detection)

```text
Implement TASK-062 — real-time event detection on top of price + on-chain + macro feeds.

Files:
- services/agents/src/agents/event_miner/main.py — entrypoint.
- services/agents/src/agents/event_miner/patterns.py — pattern detectors.

Patterns to detect (each fires a SentimentSignal with event_type set):
- earnings_release_imminent: price IV jumps + scheduled earnings within 24h.
- macro_print_imminent: known calendar events (FOMC, CPI, NFP) within 4h.
- liquidation_cascade: cross-exchange volume spike + futures funding flip + open interest drop. Crypto-specific.
- equity_circuit_breaker: SPX/NDX -5% in <30 min from open.
- crypto_exchange_hack: news+social spike on hack-related keywords + outflow from named exchange addresses.
- protocol_event: known protocol upgrades (e.g., BTC halving, ETH upgrade) within 7 days.

Sources combined:
- Market data (price moves, volume, OI from md.* streams).
- News + social via TASK-061's pipeline (subscribe to sig.sentiment).
- On-chain (deferred until on-chain ingestor exists; for MVP, stub with public APIs).
- Economic calendar (free APIs: investpy or scrape from a stable source).

Specific landmines:
- Calibration: an "earnings imminent" event isn't predictive itself; it's a regime tag. Don't conflate with directional signal.
- Many-to-one: a single market event (e.g., FOMC) generates one event signal across many symbols. Fan out in the orchestrator, not here.
- Stale calendar: economic calendars change. Refresh nightly. If calendar API down, fall back to last cached version with reduced confidence.

Author spec/tasks/TASK-062-event-miner.md, implement.

Verification:
  # Replay historical FOMC days; verify event detected within 4h of announcement.
  uv run pytest services/agents/tests/test_event_miner_replay.py
```

---

## TASK-063 prompt — Time-series foundation model

```text
Implement TASK-063 — wrap a pretrained time-series foundation model (TimesFM, Lag-Llama, or Moirai) for zero-shot forecasting.

Files:
- services/agents/src/agents/ts_foundation/main.py — entrypoint.
- services/agents/src/agents/ts_foundation/model.py — wrapper around the chosen model.
- services/agents/src/agents/ts_foundation/zero_shot.py — inference pipeline.

Model selection:
- DEFAULT: TimesFM-2.0 (Google, open weights, decent zero-shot on financial). Smaller variant runs on CPU; bigger needs GPU.
- ALTERNATIVE: Lag-Llama (probabilistic, gives quantile forecasts — useful for confidence intervals).
- ALTERNATIVE: Moirai (Salesforce, supports multivariate naturally).
- Pick ONE for v1; document the choice in an ADR. Compare via eval suite before swapping.

Inference pipeline:
1. Load latest 512 bars per symbol from feature store.
2. Per symbol: pass to model.forecast(horizon=15 bars).
3. Convert forecast to a Prediction: direction = sign(forecast_mean - last_close), confidence = 1 - (forecast_std / |forecast_mean|), bounded.
4. Publish to sig.predict.

Specific landmines:
- These models were trained on a mix of time series, not specifically financial. Validate they actually beat naive baselines (random walk, ARIMA) on YOUR data before trusting.
- Multi-horizon forecasts: foundation models give the full distribution. Don't waste it — emit multiple Prediction events at different horizons (1m, 15m, 1h). Set horizon_ns accordingly.
- Resource: TimesFM-2.0 base needs ~2GB GPU or ~20s CPU per inference. Batch across symbols to amortize.
- Don't fine-tune on your data without proper hold-out — easy to overfit and lose the zero-shot generalization that's the whole point.

Author spec/tasks/TASK-063-ts-foundation.md, implement.

Acceptance:
- Walk-forward OOS directional accuracy ≥ baseline (TASK-031 GBM) on the same labels.
- Calibrated confidence: predictions in 90%+ confidence bucket are correct ≥ 80% of the time.
- Inference cost: < $0.01 per symbol per 15-min cycle (CPU OK; document GPU upgrade cost).
```

---

## TASK-064 prompt — LLM orchestrator loop with tool use

```text
Implement TASK-064 — the LLM-driven decision loop for the orchestrator.

This wraps and AUGMENTS the existing TASK-040 orchestrator; it does not replace it. Numerical fusion (TASK-040's consensus) is fast; LLM reflection adds explainability and edge-case judgment.

Files:
- services/orchestrator/src/orchestrator/llm_loop.py — the LLM agent loop.
- services/orchestrator/src/orchestrator/explainer.py — generates human-readable rationale for each Decision.

Loop:
1. Numerical orchestrator (TASK-040) computes a candidate Decision.
2. If the Decision is non-trivial (notional > $X, or unusual signal mix), invoke LLM loop.
3. LLM gets: candidate Decision + last N signals + portfolio state (via tools from libs/fincept-tools).
4. LLM can: approve, reject (with reason), or modify (reduced size, different urgency).
5. LLM call: structured output, model = claude-sonnet-4-5 OR gpt-4o (pick one; document tradeoff).
6. Final Decision (with rationale string) emitted to ord.decisions.

Tool budget per Decision:
- Max 8 tool calls.
- Max 30s wallclock.
- Max 16k tokens of context.

Specific landmines:
- LLM should NEVER invent a tool call argument; always pull from existing signals/state via tools.
- Confidence floor: if LLM's confidence < 0.5, reject (do not modify into a low-conf trade).
- Logging: every LLM call (prompt, response, tool calls, tokens, latency, cost) → audit_log + OpenTelemetry trace.
- A/B harness: 50% of Decisions use LLM loop, 50% use numerical-only. Compare P&L attribution after 4 weeks.
- Cache: same (signal-hash, portfolio-hash) within 60s → cached LLM response. Saves cost on flapping signals.

Author spec/tasks/TASK-064-llm-orchestrator.md, implement.

Acceptance:
- Latency p99 < 2s per Decision (LLM dominates; that's OK for non-HFT).
- Daily cost: < $30 in MVP. (Adjust budget when scaling.)
- Decisions with rationale strings auditable in audit_log.
- A/B harness shows LLM-augmented Decisions don't degrade Sharpe vs numerical-only after 4 weeks.
```

---

## TASK-065 prompt — RL execution agent

```text
Implement TASK-065 — reinforcement learning agent for child-order slicing.

Files:
- services/agents/src/agents/execution_rl/main.py — entrypoint.
- services/agents/src/agents/execution_rl/env.py — gym-compatible environment over historical replay + paper OMS.
- services/agents/src/agents/execution_rl/policy.py — PPO policy (stable-baselines3).
- services/agents/src/agents/execution_rl/train.py — training script.
- services/agents/src/agents/execution_rl/serve.py — inference (sliced child orders given a parent order).

Problem framing:
- State: parent order details (qty, side, urgency, deadline) + market features (current spread, book depth, recent volatility).
- Action: place a child order with (price_offset_bps, size_pct_of_remaining, type=limit|market, expiry_seconds).
- Reward: -(execution shortfall vs benchmark VWAP) - (timing risk if not filled by deadline).
- Episode: a single parent order from receipt to either fill or deadline.

Stack:
- stable-baselines3 PPO. Document why PPO over DDPG/SAC (discrete-ish action space; PPO is stable and fast).
- Train on historical data via simulator (TASK-022 broker reused).
- Validate on held-out historical period; shadow on live paper before flipping the orchestrator to use it.

Specific landmines:
- Reward shaping is everything. Test that the policy doesn't learn to do nothing (positive reward vs random because random has costs).
- Action space: 3D continuous. Discretize on inference if continuous output gets weird at extremes.
- Policy can hallucinate impossible actions (e.g., offset_bps=10000). Clamp at every layer.
- Live deployment: agent only emits child orders for parent orders with `tags.use_rl_execution=true`. Default off; opt-in per Decision.

Author spec/tasks/TASK-065-rl-execution.md, implement.

Acceptance:
- Backtest shortfall ≥ 5bps better than naive TWAP on the same orders.
- Production deployment: shadow only; compare actual fills with vs without RL for 4 weeks.
- Training reproducibility: same seed + data → same policy weights.
```

---

## TASK-066 prompt — Research agent (HPO + alpha discovery)

```text
Implement TASK-066 — automated overnight research.

Files:
- services/agents/src/agents/research/main.py — scheduler entrypoint (runs nightly via services/jobs/).
- services/agents/src/agents/research/hpo.py — Optuna-driven hyperparameter optimization.
- services/agents/src/agents/research/discovery.py — genetic programming for alpha factor synthesis.
- services/agents/src/agents/research/promote.py — promote winning configs to MLflow staging.

HPO pipeline:
- Per registered model (gbm_predictor, ts_foundation, etc.), run an Optuna study optimizing OOS Sharpe over the last 90 days.
- Budget: 200 trials per model per night.
- Walk-forward validation per trial; multiple-comparison correction over the trial count.
- If best OOS Sharpe > current production by ≥ +0.2 with p<0.05, promote to staging.

Alpha discovery (gp.py):
- Genetic programming over a DSL of:
  - operators: add, sub, mul, div, log, abs, max, min, ts_mean(N), ts_std(N), ts_rank(N), neutralize_by(group).
  - leaves: features from the feature store.
- Fitness: rolling OOS Sharpe with multiple-comparison correction.
- Population: 200 expressions, 50 generations, mutation rate 0.1.
- Best K winners (default 5) saved to db; researchers review weekly.

Specific landmines:
- Multiple-testing correction is non-negotiable. 200 random trials × 100 models = guaranteed false positives without correction. Use Benjamini-Hochberg or Šidák.
- Computational cost: HPO + GP is heavy. Schedule overnight; throttle to NCPU - 2 to keep dev environment usable.
- Promote != deploy. Promotion goes to MLflow "staging" tag. Deployment to production requires human review.
- The genetic-programming DSL must reject expressions that look forward (use only ts_* operators that respect causality).

Author spec/tasks/TASK-066-research.md, implement.

Verification:
  uv run python -m agents.research.main --once --dry-run
  # Runs one HPO + one GP cycle on small data; produces report.
  # Schedule via services/jobs/nightly_retrain.py.
```

---

## Phase X exit verification (the profitability gate)

```text
Run the Phase X checkpoint validation.

This is the most important checkpoint in the project. If you advance past it without meeting the bar, you ship a system whose AI components are decorative, not profitable.

1. Shadow ensemble (4 weeks minimum):
   - Enable agents: gbm_predictor.v1, ts_foundation.v1, llm_sentiment.v1.
   - Orchestrator's LLM loop: enabled, A/B 50/50 with numerical-only.
   - All running in shadow against live paper OMS.

2. Statistical evaluation after 4 weeks:
   - Compute realized Sharpe of (gbm + ts + llm) ensemble paper trades, net of simulated costs.
   - Compare to baseline (gbm only).
   - Required: ensemble Sharpe ≥ baseline + 0.5, with p < 0.05 via block bootstrap (block size 1 day).
   - If not met: do NOT advance. Iterate on individual agents, eval suites, calibration. Common cause: LLM sentiment latency or symbol resolution failure.

3. Cost discipline:
   - Total LLM spend over the 4 weeks reported. Compute cost per dollar of P&L attributed.
   - If LLM cost > 30% of marginal alpha, the agent doesn't pay for itself. Optimize prompts / cache before advancing.

4. Eval suites green:
   - Each LLM agent has its eval suite passing on current model versions.
   - Foundation model has its calibration eval passing.
   - RL execution has shortfall improvement validated on held-out parent orders.

5. Audit:
   - Pick a random Decision from the shadow period. Reconstruct: source signals → numerical consensus → LLM reflection → final Decision. Every link must trace cleanly.

6. Operational stability:
   - Total agent crashes over the 4 weeks: < 5.
   - LLM API outage handled gracefully (no cascading failures; orchestrator falls back to numerical-only).

If all six pass, declare Phase X COMPLETE. Mark tasks 060–066 as [x]. Add "Checkpoint X: passed YYYY-MM-DD". Proceed to spec/prompts/phase-H-hardening.md.

If shadow Sharpe < baseline + 0.5, the cutting-edge thesis is not validated. Decide: iterate Phase X further, or pivot scope (the system is still useful as a baseline trading platform without the cutting-edge claim). Do NOT proceed to live capital with un-validated agents — that's how firms blow up.
```
