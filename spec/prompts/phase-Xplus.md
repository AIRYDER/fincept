# Phase X+ · Profitability Layer — Agent Prompts

**Tasks:** TASK-080 (options flow), TASK-081 (earnings transcripts), TASK-082 (insider + short interest), TASK-083 (cross-sectional ranking), TASK-084 (portfolio vol targeting), TASK-085 (strategy decay + capacity), TASK-086 (multi-agent LLM debate), TASK-087 (sector rotation), TASK-088 (correlation breakdown alerts), TASK-089 (liquidity stress test)

**Checkpoint:** 8-week shadow. Sharpe ≥ baseline + 0.7, max DD ≤ benchmark, realized vol within ±20% of target, p < 0.05 via block bootstrap. LLM cost per dollar of attributed alpha ≤ 30%.

Strategic context: `spec/EDGE_ROADMAP.md`. Read before any task in this phase.

---

## Phase kickoff

```text
You are now implementing the Profitability Layer — the additions whose absence is the single biggest reason most retail/small-firm systematic platforms fail to outperform passive benchmarks.

PHASE-SPECIFIC RULES:

1. CAUSAL HYPOTHESIS REQUIRED. Every alpha addition has a documented economic / behavioral mechanism BEFORE you build it. "It backtests well" is not a hypothesis. If you cannot articulate why this should work in 2 sentences, STOP and report.

2. CALIBRATION OVER ACCURACY. The system already has many predictors. The bottleneck is correctly weighting them under uncertainty. A 55%-accurate signal with calibrated confidence beats a 60%-accurate signal that's overconfident.

3. COST DISCIPLINE IS PRODUCT WORK. Track every LLM token, every alt-data feed, every backtest run. Phase X+ checkpoint requires LLM cost ≤ 30% of attributed alpha.

4. ORTHOGONALITY IS A FEATURE. The 5th momentum-flavored signal adds capacity-bound noise. The 1st cross-sectional signal adds Sharpe. Demonstrate orthogonality (correlation matrix of signal P&Ls) before deploying.

5. SHADOW BEFORE LIVE. Phase X+ agents enter with weight=0 and shadow ≥ 4 weeks before non-zero weight. The decay monitor (TASK-085) watches them.

6. DECAY IS THE NORM. Every alpha decays. TASK-085 (decay monitor) and TASK-088 (correlation-breakdown) are not optional polish — they are why this phase works.

7. CAPACITY-AWARE FROM DAY ONE. Each strategy has a capacity curve (P&L per $ deployed). Capture it. Do not over-allocate to capacity-bound alphas.

8. CONTRACTS ARE STILL IMMUTABLE. Phase X+ adds NO new event types. Conform to existing types in spec/CONTRACTS.md.

CONTEXT TO LOAD:
- spec/EDGE_ROADMAP.md, spec/CONTRACTS.md, spec/BUILD_ORDER.md (Phase X+ table).
- libs/fincept-tools (TASK-005), services/orchestrator (TASK-040), services/risk (TASK-041).
- TASK-085 must land EARLY in the phase; later tasks rely on its decay infrastructure.

WHEN STUCK:
- Backtest too good? Multiple-comparison correction missing, lookahead leak, or survivorship bias. Check in that order.
- Alpha works in backtest, fails in shadow? Costs/slippage modeled wrong, or decay was already underway.
- LLM cost spiraling? Dedup via vector memory, cheaper-model first-pass filter, aggressive truncation.
- Multi-agent debate gives same answer as single-shot? You built parallel agents with the same prompt. Re-read the spec.

Acknowledge by listing the 8 rules. State the causal hypothesis for the first task. Wait for assignment.
```

---

## TASK-080 — Options flow agent

```text
Implement TASK-080.

Hypothesis: Sophisticated traders sometimes express conviction via options before equity due to leverage + asymmetric payoff. Outsized OPENING flow that is OTM, short-dated, and large vs OI is noisy but persistent over 1–10 trading days.

Files:
- services/agents/options_flow/{main,screener,datasource,contract_validator}.py

Landmines:
- HALLUCINATED CONTRACTS. Cross-check every strike+expiry+underlying against the real chain. Drop unvalidated.
- HEDGING FLOW false positives. Filter: volume > 3× ADV, OTM 2–10%, DTE < 45d, net premium > $100k.
- OPENING vs CLOSING. Estimate from prior-day OI delta vs trade volume. Closing flow inverts the read.
- MULTI-LEG SPREADS parsed as legs hallucinate signal. v1: skip multi-leg detection; flag tags.has_multileg if uncertain.
- CONFIDENCE CAP at 0.7. Inherently noisy.

Contract: emits Prediction to sig.predict, horizon 1–10 trading days, direction long/short by call/put sweep, confidence ∈ [0.4, 0.7], tags include strike/expiry/premium_usd/iv_at_trade.

Author spec/tasks/TASK-080-options-flow.md, implement.

Verification:
  uv run pytest services/agents/tests/test_options_flow.py
  # Backtest IC ≥ 0.05 vs forward 5d returns over 6mo replay.
```

---

## TASK-081 — Earnings call transcript LLM agent

```text
Implement TASK-081.

Hypothesis: Management tone, hedging language, and forward-guidance changes during earnings calls contain information not fully priced into the post-print move. Loughran-McDonald and modern LLMs both extract forward-return-predictive signals over 3–60 days. Mechanism: slow information diffusion via analyst notes + investor digestion.

Files:
- services/agents/earnings_calls/{main,fetcher,extractor}.py
- services/agents/earnings_calls/eval/cases.jsonl (≥50 hand-labeled cases)

Landmines:
- DELAY. Transcripts post 1–4 hours after call. First move is gone. Signal is for the SECOND wave; horizon 3–60 days.
- TRANSCRIPT QUALITY varies; speaker attribution errors. Robust to "[crosstalk]" / "[inaudible]".
- STRUCTURED OUTPUT REQUIRED:
  {
    "tone_score": float in [-1, 1],
    "guidance_change": "raised" | "maintained" | "lowered" | "withdrawn" | "introduced",
    "analyst_pushback_severity": "none" | "mild" | "significant",
    "headwinds_mentioned": [str, ...],
    "tailwinds_mentioned": [str, ...],
    "confidence": float in [0, 1]
  }
- ENTITY RESOLUTION ONCE per transcript. Drop transcripts where vendor ticker disagrees with universe.
- EVAL SUITE FIRST. 50 hand-labeled across (beat-raised, beat-lowered, missed-bullish, missed-bearish, mixed). Re-eval before any model swap.
- COST. Truncate Q&A to first N exchanges if budget pressure; prepared remarks retain most signal.

Contract: SentimentSignal to sig.sentiment, event_type="earnings_call", confidence capped 0.8, horizon 60 trading days.

Author spec/tasks/TASK-081-earnings-transcripts.md, implement.

Verification:
  uv run pytest services/agents/tests/test_earnings_extractor.py
  uv run python -m agents.earnings_calls.eval --cases ./eval/cases.jsonl
  # Macro precision ≥ 0.75, recall ≥ 0.6. Daily LLM cost < $3 in MVP universe.
```

---

## TASK-082 — Insider Form 4 + short interest agents

```text
Implement TASK-082.

Hypotheses:
- Insider open-market PURCHASES (not sales, not 10b5-1, not exercise+sell) by directors/officers, especially CLUSTERED, predict 3–12mo positive returns. Mechanism: information asymmetry.
- HIGH SHORT INTEREST + positive catalyst creates squeeze potential. Mechanism: forced covering against limited float.

Files:
- services/agents/insider_short/{main,edgar_form4,finra_si,insider_analyzer,short_squeeze}.py

Landmines:
- 80% INSIDER NOISE. Filter: drop sales, drop 10b5-1, drop exercise+sell. Keep open-market purchases by named officers + directors. Cluster = ≥3 insiders within 30d.
- FORM 4 DELAY ≤ 2 business days. Real-time monitor SEC EDGAR feed; emit ≤ 1 hour of filing.
- SHORT INTEREST IS LAGGED 2 WEEKS. Combine with current price action (technical breakout, earnings beat) within 5 days.
- HIGH SI ALONE IS NOT BUY. Many high-SI names deserve to be shorted. Require co-occurring positive catalyst.
- UNIVERSE FILTER. Restrict to in-universe symbols. Form 4 firehose is huge.

Contracts:
- Insider cluster: SentimentSignal, event_type="insider_cluster_buy", score=+0.7, confidence ~ cluster size.
- Squeeze: SentimentSignal, event_type="short_squeeze_candidate", score=+0.4 (long-only), horizon 20 trading days.

Author spec/tasks/TASK-082-insider-short.md, implement.

Verification:
  uv run pytest services/agents/tests/test_form4_filter.py
  # Replay 12mo data; IC ≥ 0.04 vs forward 60d returns.
```

---

## TASK-083 — Cross-sectional ranking layer

```text
Implement TASK-083.

Hypothesis: Within a correlated universe, relative strength on composite-quality measures predicts cross-sectional outperformance. Long top decile + short bottom decile diversifies away market beta and isolates alpha. The most durable equity strategy for 30+ years (with episodic crashes).

Files:
- services/orchestrator/{cross_section,composite_score}.py

Implementation:
- Composite score per symbol: weighted sum of normalized signal scores. Weights from regime-adaptive orchestrator.
- Universe rank: percentile within universe.
- Decisions: long top X% (default 10%), short bottom X%, market-neutral.
- Rebalance: configurable (default weekly Mon).

Landmines:
- MOMENTUM CRASH RISK at regime transitions (Mar 2009, Nov 2020). Regime gate: high-vol or transition → reduce gross or skip rebalance.
- TURNOVER. Only rebalance positions whose rank-percentile shifted ≥ 10pp.
- SECTOR NEUTRALITY flag (default off): rank within sector if on.
- SURVIVORSHIP. Universe MUST include delisted/bankrupt names in backtest.
- CAPACITY at small AUM is killed by commissions. Document min-AUM gate.

Contract: Decisions on ord.decisions, batched per rebalance. tags include strategy, long/short counts, rebalance_id.

Author spec/tasks/TASK-083-cross-section.md, implement.

Verification:
  uv run pytest services/orchestrator/tests/test_cross_section.py
  # 5-yr survivorship-bias-free walk-forward, weekly rebalance: Sharpe ≥ 1.0 net of 5bps round-trip, max DD ≤ 20%.
```

---

## TASK-084 — Portfolio-level vol targeting

```text
Implement TASK-084.

Hypothesis: Per-signal Kelly ignores portfolio-level vol clustering. Constant-vol-targeted portfolios outperform constant-leverage because realized vol scales inversely with future Sharpe; reducing exposure when vol is high improves risk-adjusted returns.

Files:
- services/risk/{vol_target,realized_vol}.py

Implementation:
- EWMA realized vol (21–63 day half-life).
- Target: configurable annualized vol (default 10%).
- Scale total gross by (target / realized), capped [0.25×, 2.0×] of base.
- Apply at Risk gate, AFTER Kelly, BEFORE OMS.

Landmines:
- PROCYCLIC DELEVERAGING (selling at the bottom). Cap downscaling at 0.5× per day; longer-window vol during transitions.
- VOL-OF-VOL whipsaw. 5-day EMA on the scaler.
- COSTS from churn. Track turnover; back off responsiveness if over budget.
- EQUITY VS CRYPTO realized vols differ 3–5×. Document choice (per asset class vs portfolio-level dollar-weighted).
- LEVERAGE CAP enforced; the 2.0× cap must respect account limits.

Contract: layer, not a signal. Modifies Decision notional. Emits PortfolioVolMetric to sig.metrics each cycle.

Author spec/tasks/TASK-084-vol-target.md, implement.

Verification:
  uv run pytest services/risk/tests/test_vol_target.py
  # Backtest: 10%-targeted vs unconstrained, 3yr period. Targeted Sharpe ≥ unconstrained + 0.2.
```

---

## TASK-085 — Strategy decay monitor + capacity curves

```text
Implement TASK-085 — the meta-discipline that makes Phase X+ work. Land this EARLY in the phase.

Hypothesis: Every alpha decays — by arbitrage, by mechanism shift, or by self-competition at scale. Without monitoring, capital flows to dead strategies. Without capacity curves, the system over-allocates to capacity-bound strategies whose alpha vanishes at scale.

Files:
- services/jobs/strategy_decay.py
- services/risk/capacity.py
- libs/fincept-db/migrations/00X_strategy_metrics.sql

Decay monitor:
- Daily per strategy: rolling 21d/90d Sharpe, hit rate, turnover, IC vs forward return.
- Alert: 90d Sharpe < 0.3 for 30 consecutive days, OR Sharpe drop > 1.0 vs prior 90d.
- On alert: recommend weight × 0.5 for orchestrator.

Capacity curve:
- Fit Sharpe(N) = a · N^(-b) on (allocation, realized_pnl) history. Refit weekly.
- Recommended max: N where marginal Sharpe = 0.3.
- Allocator (TASK-094, Phase Y) reads recommended caps; for now, expose via metrics.

Landmines:
- SAMPLE-SIZE NOISE. Block-bootstrap the alert threshold. No single-week alerts.
- ATTRIBUTION QUALITY. Garbage attribution = garbage decay decisions. Verify TASK-045 portfolio attribution before trusting.
- COLD-START CAPACITY CURVE. No cap until ≥3 distinct allocation levels each ≥30 days observed.
- MANUAL OVERRIDE TABLE with required justification field.

Contracts: db.strategy_metrics_daily; AlertEvent on events.alerts; /api/strategies/{id}/capacity endpoint.

Author spec/tasks/TASK-085-decay-capacity.md, implement.

Verification:
  uv run pytest services/jobs/tests/test_strategy_decay.py
  uv run pytest services/risk/tests/test_capacity.py
  # Synthetic: strategy Sharpe 1.5 → -0.2 over 60d. Alert within 30d of cliff.
```

---

## TASK-086 — Multi-agent LLM debate

```text
Implement TASK-086 — replace single-shot LLM in TASK-064 with bull / bear / judge.

Hypothesis: Single-shot LLM exhibits motivated reasoning. Adversarial multi-agent debate (bull, bear, judge) consistently improves calibration and catches edge cases across domains. Cost: 3× tokens; alpha: better-calibrated decisions.

Files:
- services/orchestrator/llm_debate.py
- services/orchestrator/prompts/{bull,bear,judge}.md

Pattern:
1. Numerical orchestrator computes candidate Decision.
2. Bull and bear agents in PARALLEL (asyncio.gather), each blind to the other.
3. Judge: receives Decision + bull rationale + bear rationale + portfolio state. Outputs approve/modify/reject + confidence + rationale.

Landmines:
- TRUE ADVERSARIAL FRAMING. Bull and bear MUST NOT see each other's outputs. Parallel calls.
- JUDGE BIAS toward longer rationale. Judge prompt MUST require engaging with the strongest counterpoint from the opposite side before deciding.
- COST. Total cap 8k tokens across the three calls. Judge longer; bull/bear tighter.
- LATENCY. p99 < 4s (3× single-shot 1.5s budget). Measure and enforce.
- CACHE on (signal-hash, portfolio-hash) within 60s.
- FALLBACK on any timeout/malformed JSON: numerical-only Decision + alert. No improvising.

Contract: same Decision schema. tags include llm_pattern="debate", bull/bear/judge confidences, tokens_used, cost_usd. audit_log includes all three rationales.

Author spec/tasks/TASK-086-llm-debate.md, implement.

Verification:
  uv run pytest services/orchestrator/tests/test_llm_debate.py
  # 4-week A/B (50% single-shot vs 50% debate): debate ≥ 5% Brier-score improvement, no Sharpe degradation.
```

---

## TASK-087 — Sector rotation overlay

```text
Implement TASK-087.

Hypothesis: Macro regimes (early/mid/late/recession) systematically favor different sectors. Macro signals (yield curve, HY spreads, ISM, NFP) classify regime months ahead of equity confirmation. Mechanism: sector earnings-cycle sensitivity + regime-positioning lags.

Files:
- services/agents/sector_rotation/{main,macro,regime_classifier,sector_map}.py

Implementation:
- Macro features (FRED + Treasury + ISM, daily): 10y-2y slope, BAML HY OAS, ISM PMI (lagged 1mo), real Fed Funds, YoY NFP change.
- Classifier: 4-class (early/mid/late/recession). Logistic regression or shallow tree (interpretability matters). Trained on lagged-NBER + economist-classified expansions.
- Sector map (HAND-CURATED, not learned):
  - Early: cyclicals, financials, materials.
  - Mid: tech, comm services, discretionary.
  - Late: energy, staples, healthcare.
  - Recession: utilities, staples, healthcare, gold/USD.

Landmines:
- POINT-IN-TIME LABELS. NBER announces ~6mo late. Apply with that delay or the classifier looks prophetic in backtest.
- MACRO REVISIONS. Use vintage data (FRED ALFRED) for backtest, not current revisions.
- REGIME FREQUENCY 12–60mo. Smoothing/persistence floor: ≥30 days consistent classification before regime change.
- NEVER LEARN THE SECTOR MAP. Few transitions in history → guaranteed overfit. Hand-curated table.
- ETF VS BASKET. Liquid sector ETFs (XLF, XLE, ...) preferred; equally-weighted baskets if not.

Contract: RegimeSignal to sig.regime, regime_type="macro_cycle", regime ∈ {early, mid, late, recession}, tags.sector_tilts dict.

Author spec/tasks/TASK-087-sector-rotation.md, implement.

Verification:
  uv run pytest services/agents/tests/test_sector_rotation.py
  # 1990–present walk-forward: sector-tilted long-only outperforms equal-weight benchmark by ≥1.5% annualized after costs, lower max DD.
```

---

## TASK-088 — Correlation breakdown alerts

```text
Implement TASK-088.

Hypothesis: Multi-strategy systems are sized assuming approximate strategy independence. During stress, strategies sharing an underlying risk factor (most often: beta, vol-of-vol, liquidity) simultaneously lose. Realized vol explodes vs predicted; Kelly built on prior correlations becomes severely overlevered. Detecting the transition early allows preemptive deleveraging.

Files:
- services/risk/{corr_monitor,regime_alert}.py

Implementation:
- Per-strategy daily P&L for last 252d.
- Pairwise correlation on rolling 60d window.
- Top eigenvalue of correlation matrix.
- Baseline: rolling 90d median of top eigenvalue.
- Alert: top eigenvalue > baseline × 1.5 for 5 consecutive days.
- Risk gate response: gross × 0.5 until eigenvalue normalizes.

Landmines:
- MISSING-DATA FALSE POSITIVES. Strategy with zero P&L for 5d artificially correlates. Filter strategies with ≥80% non-zero P&L days.
- BASE-RATE NOISE. Block-bootstrap the threshold per strategy mix; do not flat 1.5×.
- LIVE VS PAPER. Paper correlations are simulated; tune threshold separately.
- POST-ALERT COOLDOWN. After alert + deleveraging, require ≥5 days below baseline before unwinding (eigenvalue normalizes during the deleveraging itself).

Contract: AlertEvent on events.alerts, severity="critical". Risk gate (TASK-041) reads and applies deleveraging.

Author spec/tasks/TASK-088-corr-monitor.md, implement.

Verification:
  uv run pytest services/risk/tests/test_corr_monitor.py
  # Replay March 2020 on multi-strategy portfolio: alert within 5 trading days of correlation spike, before worst of drawdown.
```

---

## TASK-089 — Liquidity stress test

```text
Implement TASK-089.

Hypothesis: Vol-based sizing ignores exit cost in adverse markets. Strategies that look profitable on paper can be uneconomical at scale because exit slippage exceeds expected alpha. Daily simulation of "exit 50% of book in 1 trading day" caps tail position size.

Files:
- services/risk/{liquidity_stress,market_impact}.py

Implementation:
- Per open position daily: % of ADV; estimated slippage to exit X% in 1 day via Almgren-style square-root model. Calibrate from TASK-022 broker history; defaults from literature.
- Aggregate: total estimated cost to exit 50% of book in 1 day, % of NAV.
- Cap: if 50%-exit cost > threshold (default 100bps NAV), prevent NEW positions that worsen the metric. Existing positions ride.
- Daily report per-symbol contribution.

Landmines:
- ADV FOR CRYPTO inflated by wash trading. Use top-3-venue volume; cap any single venue.
- ADV FOR EQUITIES varies 5×. Rolling 21d MEDIAN, not mean (mean dominated by FOMC days).
- IMPACT MODEL CALIBRATION per asset class. Almgren defaults are large-cap equity; crypto and small-cap have higher impact.
- MULTI-LEG POSITIONS don't decompose. Phase X+ scope: single-leg only; flag tags.exit_estimate_unreliable.
- SOFT GATE, not hard: prevents NEW additions, does NOT force-liquidate (would be self-fulfilling).

Contract: LiquidityStressMetric on sig.metrics. Risk gate rejects new positions exceeding threshold with reason="liquidity_stress_cap".

Author spec/tasks/TASK-089-liquidity-stress.md, implement.

Verification:
  uv run pytest services/risk/tests/test_liquidity_stress.py
  # Synthetic: 5% ADV in 10 illiquid names → flag and prevent further accumulation.
  # Calibration: estimated slippage on TASK-022 historical fills agrees within 30% of realized.
```

---

## Phase X+ exit verification (the profitability gate)

```text
Run the Phase X+ checkpoint validation. This is the gate that determines whether the Phase X investment paid off. Be rigorous.

1. Causal-hypothesis audit:
   - Every TASK-08x.md begins with a documented causal hypothesis.
   - Reviewed against EDGE_ROADMAP §5. Any hypothesis amounting to "it backtested well" is FAIL.

2. 8-week shadow results:
   - Phase X+ ensemble (10 new + Phase X agents) running shadow alongside Phase X baseline.
   - Realized Sharpe of full ensemble net of simulated costs.
   - Required: Sharpe ≥ baseline + 0.7. p < 0.05 via block bootstrap (block = 1d, B = 10000).
   - Required: max drawdown ≤ S&P 500 over the same window.
   - Required: realized portfolio vol within ±20% of TASK-084 target.

3. Cost discipline:
   - LLM spend over 8 weeks reported. Cost per dollar of attributed alpha computed.
   - Required: ≤ 30%. If higher, the agent doesn't pay for itself; optimize prompts/cache before advancing.

4. Decay infrastructure live:
   - TASK-085 decay monitor running ≥ 4 weeks in production.
   - At least one synthetic decay drill executed (artificially flipped a strategy negative; alert fired correctly).
   - Capacity curves populated for ≥ 5 strategies with ≥ 3 distinct allocation levels each.

5. Risk additions live:
   - TASK-088 correlation monitor: at least one alert fired during shadow (or synthetic test demonstrates it would have).
   - TASK-089 liquidity stress: caps applied at least once during shadow; no positions accumulated past 100bps exit threshold.

6. Orthogonality check:
   - Correlation matrix of strategy-level P&Ls computed.
   - Top eigenvalue ≤ 0.6 of total variance (no single factor dominates).
   - At least 4 strategies with pairwise |corr| < 0.3 to all others (genuine diversification).

7. Operational stability:
   - Total agent crashes ≤ 5 over the 8 weeks.
   - Multi-agent debate (TASK-086) graceful fallback to numerical-only on LLM outage tested and clean.

8. Audit: pick a random Decision from the shadow period. Reconstruct: source signals → composite score → cross-sectional rank → numerical consensus → debate rationale → vol-target scaling → liquidity gate → final Decision. Every link traces cleanly via audit_log.

If all eight pass: Phase X+ COMPLETE. Mark tasks 080–089 [x]. Add "Checkpoint X+: passed YYYY-MM-DD". The system is now a credible candidate for sustained S&P outperformance, validated in shadow. Risk-committee may now consider increasing live capital allocation per Phase H rollout schedule. Phase Y (differentiation) may begin in parallel.

If shadow Sharpe < baseline + 0.7: the profitability thesis is partially validated but not fully. Decide:
 - Iterate Phase X+ further (most common cause: weights, calibration, or one decayed agent dragging the ensemble).
 - Pivot scope (the system is still a strong baseline platform without Phase X+ checkpoint claim).
 - Do NOT scale live capital beyond Phase H Week-1 limits until passed.
```
