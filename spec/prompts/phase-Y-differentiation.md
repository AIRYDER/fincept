# Phase Y — Differentiation Layer

> **Strategic intent:** generic retail trading platforms can be replicated by anyone reading a textbook. Phase Y is where Fincept Terminal develops capabilities that competitors cannot trivially clone in a weekend. See `spec/EDGE_ROADMAP.md §4` for the durability thesis.

**Goal:** Capabilities that are durable, hard to replicate, and address known weaknesses of the Phase X+ stack — primarily *non-equity* alpha (on-chain, macro), *tail-risk* protection, *concept drift* defense, and *microstructure* extraction.

**Checkpoint:** 12-week shadow + paper. The Phase X+ ensemble plus Phase Y additions outperforms benchmark across **≥3 distinct macro regimes** within the period. Capacity stress: simulated 10× current AUM does not degrade Sharpe by more than 20%. Single-strategy max-DD contribution is bounded by `tail_hedge_budget` configured in TASK-092.

## Phase Y — Kickoff

```text
ENTERING PHASE Y — DIFFERENTIATION.

Session opener norms apply. Phases F, D, B, A, O, U, X, H, X+ complete or in maintenance. Phase-specific rules:

1. EVERY ADDITION MUST PASS THE "WHO ELSE?" TEST. Before writing a single line, document who else publishes this signal/feature/strategy. If the answer is "every retail platform with a Discord", DO NOT BUILD IT. Phase Y is about edges that survive when everyone else copies the obvious moves.
2. NON-EQUITY DIVERSIFICATION. ≥40% of new alpha by intent must come from non-equity sources (on-chain, macro, alt-data). The Phase X+ ensemble is heavily equity/crypto-equity-correlated; Phase Y must reduce that.
3. TAIL RISK IS NON-NEGOTIABLE. After TASK-092 ships, NO new strategy may go live without explicit tail-hedge budget allocation reflected in its sizing.
4. CONCEPT DRIFT IS A FIRST-CLASS PROBLEM. Static models silently decay. TASK-095 (online learning + drift detection) is required for any non-trivial supervised agent that goes to non-zero weight.
5. CAPACITY STRESS BEFORE WEIGHTS. Every Phase Y agent must publish its capacity curve (TASK-085 infrastructure) before non-zero allocation.
6. ALT-DATA: ONE VENDOR FIRST. Buying 6 alt-data feeds without ROI evidence is the canonical retail-platform money pit. TASK-093 enforces ROI-positive single vendor before second.
7. SHADOW PERIODS ARE LONGER. Phase Y agents shadow ≥ 6 weeks before non-zero weight (vs 4 weeks in Phase X+). Macro signals especially are slow.
8. CONTRACTS ARE STILL IMMUTABLE. Microstructure features extend `FeatureFrame.tags`; on-chain emits `SentimentSignal` event_type="onchain_*"; macro emits `RegimeSignal` regime_type="macro_*". No new event classes.

CONTEXT: spec/EDGE_ROADMAP.md §4 (mandatory), spec/CONTRACTS.md §3 (signals), §6 (streams), spec/prompts/phase-Xplus.md (immediate predecessor).

Tasks: TASK-090..096. Recommended order: 091 (macro_regime) → 094 (bandit_allocator) → 095 (online_learning) → 096 (microstructure) → 092 (tail_hedge) → 090 (onchain) → 093 (altdata).

Acknowledge by listing the 8 rules. State which task you are starting and confirm its causal hypothesis before any code. Wait.
```

---

## TASK-090 — On-chain analytics agent

```text
TASK-090 — On-chain analytics agent (whale wallets, exchange flows, DeFi TVL, miner reserves).

Hypothesis: Crypto markets have unique advantage: settlement is on a public ledger. Large wallet movements (whales depositing to exchange = potential sell pressure; large withdrawals = HODL accumulation), exchange reserve trends, stablecoin issuance, and miner reserves are LEADING indicators of price moves over 6h–7d horizons. Mechanism: information asymmetry between on-chain readers and price-only traders.

Files: services/agents/onchain/{main,client,whale_detector,exchange_flow,miner_reserve,defi_tvl}.py + services/ingestor/onchain/{etherscan,glassnode_free,blockchair}.py.

Implementation:
- Free tier first: blockchair.com, etherscan.io, blockchain.com APIs. Glassnode "Studio" free metrics. NO paid feeds in v1.
- Whale detector: track BTC + ETH wallets > 1000 BTC / 10000 ETH. Emit AlertEvent on movement >5% of wallet balance.
- Exchange flow: net deposits − withdrawals on top-10 CEX hot wallets, rolling 24h. Z-score → SentimentSignal score.
- Miner reserve: BTC miner-to-exchange flow, weekly net.
- DeFi TVL: Uniswap, Aave, Curve. Sudden TVL drop = de-risking event.

LANDMINES:
- ATTRIBUTION: wallet labels are crowdsourced and noisy. Use multiple sources; flag low-confidence labels.
- API RATE LIMITS: free tiers are 5–20 req/min. Cache aggressively; batch wallet queries.
- REORGS: Bitcoin reorgs are rare but happen. Confirm 6 blocks before acting on any whale event.
- CHAIN HALTS: Ethereum has had multi-hour client splits. Detect and pause signals during halts.
- MEME COIN NOISE: stick to BTC, ETH, top-10 stablecoins in v1. Adding alt-L1s = noise-to-signal collapse.

DONE WHEN:
- pytest green.
- 3-month replay: whale-movement alerts precede ≥30% of >5% same-day price moves on BTC/ETH.
- Cost: $0 (free APIs only).
- Emits: AlertEvent (whale moves), SentimentSignal event_type="onchain_exchange_flow"|"onchain_miner_reserve"|"onchain_defi_tvl" with confidence ∈ [0.3, 0.6] (inherently noisy).
- spec/tasks/TASK-090-onchain.md authored with documented hypothesis.

VERIFY: uv run pytest services/agents/tests/test_onchain.py -v
REPORT. CONTRACTS: §3 (SentimentSignal, AlertEvent).
```

---

## TASK-091 — Cross-asset macro regime model

```text
TASK-091 — Cross-asset macro regime classifier (inflation × growth × liquidity).

Hypothesis: Asset returns conditional on macro regime are >2× more predictable than unconditional. The 3-axis decomposition (inflation rising/falling × growth rising/falling × liquidity expanding/contracting) yields 8 regimes with distinct expected returns across equity, crypto, USD, gold, bonds. Mechanism: monetary-policy + fiscal-cycle effects on risk premia.

Files: services/agents/macro_regime/{main,classifier,features,asset_response}.py.

Features (FRED + Treasury + ISM, daily):
- Inflation: 5y5y forward, sticky-CPI YoY, ISM prices-paid.
- Growth: ISM PMI, NFP YoY change, real retail sales YoY.
- Liquidity: Fed balance sheet, RRP, M2 YoY, financial-conditions index.

Classifier: hidden Markov model with 8 states OR shallow tree. Soft regime probabilities (not hard labels). Smoothing: regime change requires ≥10 consecutive days posterior > 0.5.

Asset-response table (HAND-CURATED, not learned):
- Inflation↑Growth↑Liquidity↑: equity ++, crypto ++, gold +, USD −, bonds −.
- Inflation↑Growth↑Liquidity↓: equity ±, crypto −, gold +, USD +, bonds −.
- Inflation↑Growth↓Liquidity↓ (stagflation): equity −−, crypto −, gold ++, USD +, bonds −.
- Inflation↓Growth↑Liquidity↑ (Goldilocks): equity ++, crypto +, gold −, USD −, bonds +.
- ... etc. 8 cells total.

LANDMINES:
- POINT-IN-TIME: macro releases are revised. Use FRED ALFRED vintage data.
- LABEL LAG: NBER recession dates are announced 6+ months late. Apply matching delay in backtest.
- REGIME TRANSITIONS are the highest-information events. Smooth carefully so as not to over-fit to single noisy data point.
- THE TABLE IS HAND-CURATED. DO NOT learn it from data — too few transitions in 70 years to fit 8 cells.
- DO NOT extrapolate from this regime to instrument-level alpha. Use it as a TILT layer in TASK-094 (bandit allocator) and as a gate in TASK-088 (correlation breakdown).

DONE WHEN:
- pytest green.
- 1990–present walk-forward: regime-conditional buy-and-hold (asset-response table) outperforms equal-weight by ≥1.0% annualized after transaction costs.
- Emits RegimeSignal regime_type="macro_3axis", tags include p_inflation_up, p_growth_up, p_liquidity_up.
- spec/tasks/TASK-091-macro-regime.md authored with documented hypothesis.

VERIFY: uv run pytest services/agents/tests/test_macro_regime.py -v
REPORT. CONTRACTS: §3 (RegimeSignal).
```

---

## TASK-092 — Tail-risk hedging budget

```text
TASK-092 — Systematic tail-risk hedge (OTM SPX puts + crypto OTM puts).

Hypothesis: A small, persistent allocation to OTM index puts is negative-EV in normal times but bounds drawdowns during left-tail events. Without it, a single Black Monday or Mar-2020 event wipes years of compounded alpha. Cost: ~1–3% annualized; benefit: max-DD bounded.

Files: services/risk/tail_hedge.py + services/oms/options_paper.py + services/oms/venue/options_sim.py.

Implementation:
- Configurable `tail_hedge_budget_bps` (default 200bps annualized = 0.5%/quarter).
- Roll OTM SPX puts ~10% OTM, ~90 DTE, quarterly. Match crypto book with BTC OTM puts (Deribit) at proportional notional.
- Fund hedge from a dedicated budget line; do NOT cannibalize alpha-strategy capital.
- On VIX spike (>40), pause new hedge purchases (hedge prices already reflect the move).
- Paper-trade the entire hedge book in v1 (real options trading is Phase H gated; this task delivers shadow-mode infrastructure).

LANDMINES:
- BUDGET DISCIPLINE: tail-hedge feels like wasted money for 95% of the time. Without a hard config-budget cap, operators cut it during good years and miss the next Mar-2020. Make it INVISIBLE in normal P&L view; visible only in stress reports.
- ROLL TIMING: never roll the day before earnings/FOMC; volatility is mispriced. Skip 3 days around major events.
- BASIS RISK: SPX puts hedge equity book; BTC puts hedge crypto. NOT cross-fungible.
- LIQUIDITY: deep OTM puts have wide spreads. Use mid + 50% to reasonable max half-spread.

DONE WHEN:
- pytest green.
- Backtest 2008, 2020 events: portfolio with 200bps annual hedge budget shows max-DD ≤ 60% of unhedged portfolio max-DD.
- Hedge cost over 2010–2019 (calm decade) ≤ 3% annualized drag.
- Emits Decision events tagged strategy="tail_hedge", with separate audit trail.
- spec/tasks/TASK-092-tail-hedge.md authored.

VERIFY: uv run pytest services/risk/tests/test_tail_hedge.py -v
REPORT. CONTRACTS: §4 (Decision), tagged "strategy=tail_hedge".
```

---

## TASK-093 — Selective alt-data integration

```text
TASK-093 — Selective alt-data integration (ONE ROI-positive vendor first).

Hypothesis: SOME alt-data feeds (credit-card aggregates, satellite parking-lot counts, app downloads) contain real alpha. MOST do not. The economic question is which one, and at what price. Wrong answer = $5k/mo subscription with no measurable alpha lift; right answer = $5k/mo for +0.3 Sharpe attribution = wildly profitable.

Files: services/ingestor/altdata/{base,vendor_a}.py + services/agents/altdata/{main,evaluator}.py.

PROCESS (not just code):
1. **Pre-purchase eval phase (free trial/demo data)**: ≥3 months of historical sample; eval suite measures attribution to forward returns; null-hypothesis test that this signal is orthogonal to existing Phase X+ ensemble.
2. **Decision gate**: alt-data vendor approved IFF projected attribution > 1.5× annual cost AND orthogonality |corr| < 0.3 vs existing alpha pool.
3. **Implementation phase**: build adapter ONLY for the approved vendor.
4. **Monthly re-eval**: re-run attribution monthly. Renewal gate: same > 1.5× rule. Auto-cancel if < 1.0× for 2 consecutive months.

VENDORS to consider (ROI-evaluation-only in v1, not commitment):
- SimilarWeb / data.ai (app + web traffic) — high signal for consumer-tech earnings; ~$2k/mo.
- Earnest / Yodlee credit card aggregates — gated, $10k+/mo, only worth it if quant equity book is core.
- Glassnode paid tier — only if on-chain agent (TASK-090) shows alpha but free tier is bottlenecking it.
- DO NOT consider: generic "social sentiment" vendors (already public, already arbitraged).

LANDMINES:
- VENDOR LICENSING: most alt-data licenses are NON-TRANSFERABLE and FORBID RESELLING SIGNALS. Read the license. If your output is a multi-tenant SaaS, you cannot embed vendor data in customer-facing predictions without license upgrade.
- POINT-IN-TIME: vendors restate historical data. Confirm vendor provides true PIT or simulate the lag.
- SURVIVORSHIP: vendor sample data is curated to look good. Always demand random in-sample period, not vendor-selected.
- THE NULL HYPOTHESIS WINS by default. Reject vendors that don't clear it after 3 months.

DONE WHEN:
- pytest green.
- ≥1 vendor evaluated end-to-end via the pre-purchase eval phase (even if rejected).
- Adapter built for ≥1 vendor that PASSED the gate (or documented decision that none passed).
- spec/tasks/TASK-093-altdata.md authored with vendor evaluation log.

VERIFY: uv run pytest services/ingestor/altdata/tests/ -v
REPORT.
```

---

## TASK-094 — Multi-arm bandit strategy allocator

```text
TASK-094 — Multi-arm bandit strategy allocator (Thompson sampling above orchestrator).

Hypothesis: Fixed strategy weights are stale within weeks. Thompson sampling over per-strategy posterior Sharpe distributions converges to optimal allocation faster than equal-weight or fixed-Sharpe weighting and adapts gracefully when new strategies enter / decay. Mechanism: explicit exploration-exploitation balance in capital allocation.

Files: services/orchestrator/bandit_allocator.py + libs/fincept-db/migrations/00X_strategy_posteriors.sql.

Implementation:
- For each strategy, maintain a Beta(α,β) posterior on Sharpe quintile-rank within the active strategy pool.
- Daily: sample from each posterior; allocate capital proportional to sampled values, normalized.
- Decay: prior α,β multiply by 0.99 daily so old performance fades.
- Cold start: new strategy enters with prior matching the median strategy. Min 30 days observation before allocator gives full sampling weight.
- Sits ABOVE orchestrator (TASK-040) — orchestrator still produces per-strategy decisions; allocator scales each strategy's gross exposure.

LANDMINES:
- DEPENDENCY ON TASK-085 (decay monitor). Bandit allocator cannot meaningfully sample posteriors without the strategy_metrics_daily table.
- THOMPSON HAS HIGH VARIANCE in early days. Cap turnover: max ±20% allocation change per week per strategy, regardless of sample.
- REGIME SWITCH: posteriors lag regime changes by 2–4 weeks. Optionally condition posterior on TASK-091 macro regime (regime-conditional posteriors). Document if used.
- BUDGET INVARIANT: Σ(allocator weights × strategy gross) ≤ portfolio gross cap. Enforce at the allocator boundary.

DONE WHEN:
- pytest green.
- Backtest on synthetic 6-strategy pool with one decaying strategy: bandit reallocates away from decayer within 4 weeks of cliff.
- Live shadow: allocator outputs match (within 5%) a fixed-Sharpe weighting in stable regimes; diverges sensibly when one strategy decays.
- Emits AllocatorDecision to ord.allocator with audit trail.
- spec/tasks/TASK-094-bandit-allocator.md authored.

VERIFY: uv run pytest services/orchestrator/tests/test_bandit_allocator.py -v
REPORT.
```

---

## TASK-095 — Online learning + concept drift

```text
TASK-095 — Online learning + concept drift detection (river integration for GBM + features).

Hypothesis: Static GBMs (TASK-031) trained on N months of data silently decay as market microstructure evolves (regulation changes, market-maker dropouts, fee changes, regime shifts). Online learning + explicit drift detection maintains model relevance with minimal retrain cost.

Files: services/agents/gbm_predictor/online.py + services/features/online_drift.py + libs/fincept-tools/drift_detector.py.

Implementation:
- river.tree.HoeffdingTreeClassifier or river.ensemble.AdaptiveRandomForestClassifier alongside the batch LightGBM model.
- Online model updates per-bar; batch model retrained nightly on full window.
- Drift detector: ADWIN or DDM on residuals (prediction − realized). Alert when drift detected.
- On drift alert: weight = max(weight × 0.5, min_weight) until next batch retrain confirms recovery.
- Feature drift: PSI (population stability index) on feature distributions, daily. PSI > 0.25 → feature warning; >0.5 → feature pause.

LANDMINES:
- ONLINE MODELS UNDER-PERFORM BATCH on stationary periods. Keep both. Use ensemble (online weighted ~30% in normal regime, ~70% post-drift).
- CONCEPT DRIFT VS SAMPLE NOISE: ADWIN with default delta=0.002 fires on noise; tune delta=0.0001 with backtested calibration.
- FEATURE PIPELINE PARALLELISM: online features must be computed identically to offline. Use the same code path (TASK-016 / 017).
- DRIFT ON LIVE > DRIFT ON PAPER. When transitioning paper→live, expect drift alerts in the first 2 weeks; do not auto-retrain on the live data immediately. Wait for steady state.

DONE WHEN:
- pytest green.
- Synthetic test: data distribution shifts at known timestamp; ADWIN alerts within 200 samples.
- Live shadow: online + batch ensemble achieves Sharpe ≥ batch-only Sharpe (no degradation), with bounded drift episodes documented.
- Emits AlertEvent severity="warning" on drift detection.
- spec/tasks/TASK-095-online-drift.md authored.

VERIFY: uv run pytest services/agents/tests/test_online_drift.py -v
REPORT. CONTRACTS: §3 (Prediction), §7 (AlertEvent).
```

---

## TASK-096 — L2 microstructure features

```text
TASK-096 — L2 microstructure features (order-book imbalance, hidden-liquidity, flow toxicity).

Hypothesis: Order-book microstructure contains short-horizon (seconds–minutes) directional information that is invisible to bar-aggregated features. Three primary signals: (a) book imbalance Δ predicts next-tick direction; (b) hidden-liquidity proxy (effective spread vs quoted) reveals informed traders; (c) flow toxicity (VPIN — volume-synchronized PIN) flags adverse-selection regimes for execution sizing.

Files: services/features/microstructure.py + services/ingestor/binance_l2.py (extend) + tests/.

Implementation:
- Book imbalance: (bid_size − ask_size) / (bid_size + ask_size) at top-N levels. N=5 default.
- Hidden liquidity: (effective_spread − quoted_spread) / quoted_spread. Effective spread from realized fills.
- VPIN: Easley/López de Prado formulation. Buckets of constant volume; PIN computed bucket-by-bucket. Updates per bucket close.
- All features published to FeatureFrame.tags with name prefix "micro_".

LANDMINES:
- L2 INGESTION COST: 100× more bandwidth than L1. Sample-rate decision: full L2 in dev, top-5 + 100ms snapshot in prod is usually enough.
- VENUE SPECIFICITY: book formats differ. Binance, Coinbase, Kraken all have subtly different update semantics. Build per-venue normalizers.
- REGULATORY SCOPE: equity L2 (TotalView, ARCA, etc.) is licensed market data with redistribution restrictions. Crypto L2 is unrestricted. v1 scope: crypto only.
- VPIN IS NOT A PREDICTION. It's a regime indicator. High VPIN → reduce execution size, NOT short the asset.
- LATENCY BUDGET: book-imbalance feature must be available within 50ms of book update for HFT-adjacent strategies. Use Redis Streams + in-process aggregator, not DB roundtrips.

DONE WHEN:
- pytest green.
- Live test: 1 hour of BTCUSDT L2 → micro features computed at >100Hz, published to feature store with <50ms p99 latency.
- Predictive test: book imbalance has IC ≥ 0.05 vs next-bar return on BTCUSDT 1m bars over 3-month replay.
- spec/tasks/TASK-096-microstructure.md authored.

VERIFY: uv run pytest services/features/tests/test_microstructure.py -v
REPORT. CONTRACTS: §3 (FeatureFrame).
```

---

## Phase Y — Exit verification

```text
PHASE Y EXIT — DIFFERENTIATION GATE.

CHECKLIST:
1. WHO-ELSE AUDIT: every spec/tasks/TASK-09x.md begins with documented "who else publishes this signal" and the Phase Y hypothesis test (the answer is NOT "every retail platform"). Reviewed against EDGE_ROADMAP §4.
2. NON-EQUITY DIVERSIFICATION: ≥40% of new alpha attribution over the 12-week window comes from non-equity sources (onchain, macro, microstructure-on-crypto). Reported in attribution dashboard.
3. 12-WEEK SHADOW + PAPER: Phase X+ ensemble + Phase Y additions outperforms benchmark across ≥3 distinct macro regimes (per TASK-091 classifier) within the 12 weeks.
4. CAPACITY STRESS: simulated 10× current AUM does not degrade Sharpe by more than 20%. Per-strategy capacity curves (TASK-085) reflect the stress test.
5. TAIL-HEDGE BUDGET ENFORCED: every live strategy has its allocated tail-hedge budget reflected in Risk gate; total tail-hedge spend during shadow ≤ configured cap.
6. CONCEPT DRIFT DETECTORS LIVE: TASK-095 detectors running on all supervised agents; drift events documented; weight reductions applied where flagged.
7. ALT-DATA ROI: TASK-093 vendor either passed gate (kept) or failed gate (cancelled) — no "indefinite trial" status allowed.
8. ORTHOGONALITY: correlation matrix of strategy-level P&Ls across (X+ + Y) — top eigenvalue ≤ 0.5 of total variance; ≥5 strategies with pairwise |corr| < 0.3.
9. mypy --strict clean across all Phase Y services.
10. TASK-090..096 specs exist; [x] in BUILD_ORDER.md.

If green: Phase Y COMPLETE. Add "Checkpoint Y: passed YYYY-MM-DD". Phase Z (research frontier) may begin.

If <3 macro regimes occurred in window: extend shadow to ≥18 weeks before evaluation. Macro is slow.

If non-equity attribution < 40%: Phase Y is partially differentiated. Decide:
- Iterate (most common cause: onchain agent under-leveraged or macro regime weights too small).
- Accept (the system is differentiated on the X+ axes alone; explicit choice).

REPORT.
```
