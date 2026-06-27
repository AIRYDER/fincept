# Deep Dive: Data, Training, and the Path to Cutting-Edge

A deep architectural analysis of the Quant Foundry's data → training →
evaluation pipeline, followed by concrete, grounded speculation on
cutting-edge improvements.

This document assumes you've read `RUNPOD_TRAINING_ARCHITECTURE.md` and
`DATASETS_AND_DATA_STRUCTURE.md`. It goes deeper into the *why* behind
each design choice, the full evidence loop, and where the frontier is.

---

## Part I — The Deep Architecture

### 1. The Full Evidence Loop (not just training)

The system is not a training pipeline. It's a **closed evidence loop** —
a tournament where models compete, get scored on settled out-of-sample
performance, and are promoted or retired based on statistical evidence.
Training is just one phase of the loop.

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │                         THE EVIDENCE LOOP                                │
 │                                                                          │
 │   ┌─────────┐    ┌──────────┐    ┌───────────┐    ┌──────────────┐      │
 │   │ Feature │───▶│ Training │───▶│ Shadow    │───▶│ Settlement   │      │
 │   │ Lake    │    │ (RunPod) │    │ Inference │    │ Ledger       │      │
 │   │ (PIT)   │    │          │    │ (RunPod)  │    │ (gross→net)  │      │
 │   └─────────┘    └──────────┘    └───────────┘    └──────┬───────┘      │
 │        │              │               │                  │              │
 │        │         ModelDossier    ShadowPrediction   SettlementRecord    │
 │        │         (artifact)      (per-symbol)       (realized_return)   │
 │        │              │               │                  │              │
 │        │              ▼               ▼                  ▼              │
 │        │         ┌────────────────────────────────────────┐             │
 │        │         │          Leakage & Overfit Sentinel     │             │
 │        │         │  (shuffled labels, time-reverse, PBO,   │             │
 │        │         │   train/live gap, feature stability)    │             │
 │        │         └──────────────────┬─────────────────────┘             │
 │        │                            │                                   │
 │        │                            ▼                                   │
 │        │         ┌────────────────────────────────────────┐             │
 │        │         │           Tournament Scorer             │             │
 │        │         │  (DSR + bootstrap p-value + weighted    │             │
 │        │         │   components → total_score)             │             │
 │        │         └──────────────────┬─────────────────────┘             │
 │        │                            │                                   │
 │        │                            ▼                                   │
 │        │         ┌────────────────────────────────────────┐             │
 │        │         │          Leaderboard (ranked)           │             │
 │        │         └──────────────────┬─────────────────────┘             │
 │        │                            │                                   │
 │        │                            ▼                                   │
 │        │         ┌────────────────────────────────────────┐             │
 │        │         │       Promotion Gate (human-gated)      │             │
 │        │         │  SHADOW_ONLY → paper_approved → live    │             │
 │        │         └──────────────────┬─────────────────────┘             │
 │        │                            │                                   │
 │        │                            ▼                                   │
 │        │         ┌────────────────────────────────────────┐             │
 │        │         │       Drift Sentinel (adversarial)      │             │
 │        │         │  (feature drift, calibration drift,     │             │
 │        │         │   provider freshness, edge decay)        │             │
 │        │         └──────────────────┬─────────────────────┘             │
 │        │                            │                                   │
 │        │              ┌─────────────┴──────────────┐                    │
 │        │              ▼                            ▼                    │
 │        │         RETIRE / RETRAIN           LOWER_TRUST / SHADOW        │
 │        │              │                            │                    │
 │        └──────────────┘                            │                    │
 │   (retrain feeds back into the Feature Lake)       │                    │
 └────────────────────────────────────────────────────┘───────────────────┘
```

Every arrow is a **signed, hash-verifiable, frozen record**. Nothing in
this loop is mutable after creation — the audit trail is append-only.

### 2. The Seven Defense Layers (in order of execution)

The system defends against leakage, overfitting, and drift at seven
distinct layers. Each layer is independent and fail-closed.

```
  LAYER                          WHAT IT CATCHES                    WHEN
  ─────────────────────────────────────────────────────────────────────────
  1. PIT Proof (feature_lake)    look-ahead features                export time
  2. As-of Universe              survivorship bias / forward joins  construction
  3. Purged-k-fold + Embargo     label overlap across folds         manifest build
  4. Leakage Sentinel            shuffled-label edge, time-reverse  post-training
  5. PBO (CSCV)                  combinatorial overfit              post-training
  6. DSR + Bootstrap p-value     multiple-comparisons + luck        tournament
  7. Drift Sentinel              regime change / edge decay         live monitoring
```

**Layer 1-3** prevent leakage at data construction time.
**Layer 4-5** detect overfitting after training but before promotion.
**Layer 6** filters luck from the tournament ranking.
**Layer 7** catches decay after promotion.

A model must pass **all seven** to reach live trading. A failure at any
layer produces a `blocking_issue` on the dossier that only a recorded
human waiver can override.

### 3. The Statistical Engine (deep dive)

#### 3.1 Deflated Sharpe Ratio (DSR)

The tournament doesn't rank on raw Sharpe — it ranks on **Deflated**
Sharpe, which discounts for two things:

```
  DSR = raw_sharpe − multiple_trials_penalty − non_normality_penalty

  multiple_trials_penalty = sqrt(2 * ln(trial_count)) * SE_per_period
  non_normality_penalty   = skew/kurtosis adjustment (Bailey & López de Prado 2014)
```

**Why this matters:** If you train 1000 models and pick the best by raw
Sharpe, you're almost certainly picking luck. The multiple-trials
penalty grows with `sqrt(2 * ln(N))` — the expected maximum of N
independent standard normals. A model that looks great after 1 trial
might be noise after 1000 trials. DSR corrects for this.

The non-normality penalty handles the fact that financial returns have
fat tails and negative skew. A Sharpe of 2.0 driven by one lucky month
is not the same as a Sharpe of 2.0 from 60 months of steady returns.

#### 3.2 Stationary Bootstrap p-value

The significance test doesn't assume IID returns — it uses the
**stationary bootstrap** (Politis & Romano, 1994):

```
  1. Compute the model's OOS net edge: mean(oos_returns_net)
  2. Generate a zero-skill baseline (returns = 0)
  3. Resample blocks of random length (geometrically distributed
     with expected length 1/p) from the model's returns
  4. For each bootstrap resample, compute the edge
  5. p-value = fraction of resamples where baseline ≥ model
  6. If p < 0.05, the edge is statistically significant
```

**Why blocks, not IID?** A 5-day prediction made on day t and day t+1
share 4 days of return. IID resampling would treat them as independent,
understating variance and overstating significance. Block resampling
preserves the autocorrelation structure.

#### 3.3 Probability of Backtest Overfitting (PBO)

PBO uses Combinatorially Symmetric Cross-Validation (CSCV):

```
  1. Split IS + OOS returns for N candidates into S partitions (default 16)
  2. For each combinatorial split (first half = IS, second half = OOS):
     a. Rank candidates by IS Sharpe
     b. Find the IS-optimal candidate
     c. Check if its OOS rank is below the median
  3. PBO = fraction of splits where IS-optimal underperforms median OOS
  4. logit(PBO) = ln(PBO / (1 − PBO))  → positive = overfit
```

**Interpretation:** PBO = 0.5 means the IS ranking is useless — the
best-looking IS strategy is no better than median OOS. PBO > 0.5 means
the family is likely overfit. The current trainer also computes a
simpler fold-level PBO (`count(val_acc < train_acc) / n_folds`).

#### 3.4 The Tournament Score (the "points")

```
  total_score = Σ(positive components) − Σ(penalty components)

  POSITIVE (weights sum to 1.0):
    net_edge          × 0.40    mean(net OOS returns) — net of costs!
    deflated_sharpe   × 0.35    squashed DSR (tanh-like)
    calibration       × 0.25    1 − Brier score

  PENALTY (subtracted):
    drawdown_penalty          × 0.10
    turnover_penalty          × 0.05
    feature_availability      × 0.05
    latency_penalty           × 0.05
    capacity_decay_penalty    × 0.05
```

**Key design choice:** `net_edge` is weighted highest (0.40) and is
**net of costs**, not gross. A model that makes 10 bps gross but pays
8 bps in fees + slippage has a net edge of 2 bps — and the tournament
sees that 2 bps, not the 10. This is enforced by the `CostModel`:

```python
@dataclass(frozen=True)
class CostModel:
    version: str              # cost models are versioned!
    fee_bps: float            # round-trip exchange/broker fee
    spread_bps: float         # modeled bid-ask spread
    slippage_bps: float       # market impact
    borrow_bps_per_day: float # financing for shorts
```

The cost model is **versioned** and recorded on every settled record, so
a later cost change doesn't silently rewrite history.

### 4. The Settlement Lifecycle (how predictions become evidence)

A shadow prediction goes through three states before it becomes
tournament evidence:

```
  ShadowPrediction emitted
        │
        ▼
  ┌─────────────┐     now < t + horizon     ┌──────────────┐
  │ PENDING_TIME│ ────────────────────────▶ │  not yet due │
  └─────────────┘                           └──────────────┘
        │
        │ now ≥ t + horizon
        ▼
  ┌─────────────┐     market data missing   ┌──────────────┐
  │ PENDING_DATA│ ────────────────────────▶ │ stuck provider│
  └─────────────┘                           └──────────────┘
        │
        │ market data available
        ▼
  ┌─────────────┐
  │   SETTLED   │ → realized_return_net → tournament
  └─────────────┘
```

**Why two pending states?** A stuck data provider (`PENDING_DATA`) is
not the same as a prediction that simply hasn't had enough time to
resolve (`PENDING_TIME`). Confusing them would either settle early
(look-ahead) or wait forever on a dead provider.

### 5. The Alpha Genome (automated recipe search)

The `AlphaGenomeLab` is a bounded-mutation engine that automates the
search for candidate recipes. It's not random search — it's a
constrained evolutionary search with cost budgets and early stopping.

```
  Parent Recipe
       │
       ▼
  RecipeMutation (typed, allowlisted)
  ├── add feature (from allowlist)
  ├── remove feature
  ├── transform feature (zscore, rank, log_return, diff, rolling_mean, rolling_std)
  ├── set hyperparameter (within bounds)
  ├── narrow window
  └── widen window
       │
       ▼
  N mutated recipes → dispatch training (budget-guarded)
       │
       ▼
  EarlyStopper monitors intermediate TournamentScore
  ├── kills underperforming recipes → KILLED_EARLY
  └── lets promising recipes complete
       │
       ▼
  Survivors → PromotionGate.evaluate() (same path as any model)
  ├── APPROVED → register
  └── REJECTED → DiscardReceipt
```

**Critical invariant:** Every genome-generated candidate goes through
the **same** `PromotionGate.evaluate()` path as any manually created
model. No shortcut, no bypass. The genome cannot promote its own
children.

### 6. The Conformal Gate (uncertainty quantification)

The conformal gate produces **prediction intervals** (q10/q50/q90) and
**abstains** when the model can't make a reliable prediction:

```
  point_estimate from model
        │
        ▼
  ConformalCalibrator (fitted on residuals = predictions − outcomes)
        │
        ├── interval = [point + q10(residuals), point + q90(residuals)]
        │   q50 = point + median(residuals)
        │
        ▼
  Abstain checks:
  1. Insufficient calibration data (< min samples)? → ABSTAIN
  2. Confidence < min_confidence?                   → ABSTAIN
  3. Interval width > max_interval_width?           → ABSTAIN
  4. All pass → emit ConformalPrediction(interval)
```

**Why this matters for the tournament:** The interval width feeds into
position sizing (wide interval → smaller notional) and into the
tournament's `calibration` component. A model that is well-calibrated
(intervals cover the truth at the right rate) scores higher than one
that is just accurate on average.

### 7. The MoE Router (which model to trust by regime)

The Mixture-of-Experts router learns **which model to trust by market
regime, symbol, liquidity, volatility, and news type**:

```
  RoutingContext:
    regime, symbol, symbol_cluster, horizon,
    feature_availability, liquidity, volatility, news_type
        │
        ▼
  Rule-based routing (from tournament evidence)
  → per-regime, per-horizon, per-cluster scores
        │
        ▼
  If enough settled evidence (≥ min_settled_count):
    Learned router gate → weighted expert selection
  Else:
    ABSTAIN (INSUFFICIENT_EVIDENCE)
        │
        ▼
  Expert weights → prediction
```

**Abstain conditions:** no experts available, low feature availability,
stale model, poor calibration, insufficient evidence. The router would
rather abstain than route to a bad expert.

### 8. The Causal Graph (structural knowledge)

The system maintains a `CausalGraph` — a typed graph of
symbol/sector/event/regime/outcome nodes and leads/lags/correlates/
causes/influences edges:

```python
CausalNode(kind=SYMBOL,    label="AAPL")
CausalNode(kind=SECTOR,    label="TECH")
CausalNode(kind=EVENT,     label="Fed-rate-hike-2026-03")
CausalNode(kind=REGIME,    label="high-vol")
CausalNode(kind=OUTCOME,   label="AAPL-1d-return")

CausalEdge(source="Fed-rate-hike", target="AAPL",
           kind=CAUSES, strength=0.7, lag_ns=3_600_000_000_000)
CausalEdge(source="TECH", target="AAPL",
           kind=INFLUENCES, strength=0.8)
```

This is currently a structural scaffold — the graph is built but not
yet wired into feature engineering. It's the foundation for
causal-aware features (§Part II.3 below).

---

## Part II — Cutting-Edge Improvements (grounded speculation)

Each proposal is grounded in the existing architecture and the team's
own research knowledge base (`research/`). I'll note the existing
component it extends, the research entry it draws from, the effort, and
the risk.

### 1. Path Signature Features (rough paths)

**Extends:** `feature_lake.py` → `FeatureValue` set; `alpha_genome.py`
→ `ALLOWED_TRANSFORMS`

**Research:** `research/papers/2025/lyons-path-signatures.md`

**The idea:** The path signature — a sequence of iterated integrals of
the time series — is a **universal feature**: a linear function of
signature terms can approximate any continuous function of the path
(Stone-Weierstrass for paths). Empirically, signature features improve
GBM accuracy by 2-5% on financial time series.

**How it fits the existing architecture:**

```
  Current feature pipeline:
    raw bars → ret_1d, vol_20d, momentum, ... → FeatureValue(name, value, observed_at)

  Proposed:
    raw bars → ret_1d, vol_20d, ... + signature_terms(depth=3)
             → FeatureValue("sig_1", ..., observed_at=t)  ← PIT-safe (computed from past only)
             → FeatureValue("sig_2", ..., observed_at=t)
             → ...
```

The signature is computed from a **trailing window** ending at
`decision_time`, so `observed_at = decision_time` — PIT-safe by
construction. It slots into the existing `FeatureValue` schema with no
schema change. The Alpha Genome's `ALLOWED_TRANSFORMS` would gain a
`path_signature` transform.

**Why it's cutting-edge:** Signatures capture non-linear temporal
interactions that hand-engineered features miss. A depth-3 signature
of (price, volume) captures the joint dynamics of price moves and
volume — something that `ret_1d` and `vol_20d` separately cannot.

**Effort:** 1-2 weeks (use `iisignature` or `signatory` library).
**Risk:** Medium. The signature is high-dimensional (depth 3 ≈ O(n³)
terms); feature selection is needed. The Alpha Genome's mutation engine
already has the infrastructure for feature selection.

**Concrete next step:** Add `path_signature(returns, volume, depth=3)`
as a transform in `feature_lake.py`, emit the top-k signature terms by
variance, and let the Alpha Genome search over which terms to include.

---

### 2. Adaptive Conformal Prediction (online recalibration)

**Extends:** `conformal_gate.py` → `ConformalCalibrator`

**Research:** `research/papers/2025/vovk-conformal-trading.md`

**The idea:** The current conformal calibrator is **batch-fitted** on a
fixed residual set. Adaptive Conformal Inference (ACI) updates the
calibration online as new outcomes settle, so the intervals track
regime changes.

**How it fits:**

```
  Current:
    ConformalCalibrator.fit(residuals)  ← batch, static
    ConformalGate.predict(point_estimate)

  Proposed:
    AdaptiveConformalCalibrator
    ├── update(prediction, outcome)  ← called on every settlement
    ├── sliding window of recent residuals (drops old ones)
    ├── ACI correction: adjusts α based on recent coverage
    └── predict_interval(point_estimate) → interval that tracks drift
```

The `SettlementLedger` already produces `(prediction, outcome)` pairs
on every settlement. The adaptive calibrator hooks into the settlement
sweep — each settled record updates the calibrator. The `DriftSentinel`'s
`CALIBRATION_DRIFT` indicator becomes the trigger for ACI's correction
term.

**Why it's cutting-edge:** Static conformal intervals degrade under
regime change. ACI maintains coverage guarantees under weak stationarity
by adapting the quantile online. This is the difference between "the
interval was correct on average over 2024" and "the interval is correct
right now."

**Effort:** 1-2 weeks. The conformal infrastructure already exists;
this is an extension of `ConformalCalibrator` with an `update()` method
and a sliding window.
**Risk:** Low. The coverage guarantee is marginal (long-run), not
per-prediction — but that's already the case for the batch version.

**Concrete next step:** Add `AdaptiveConformalCalibrator` with a
`window_size` parameter and an `update(prediction, outcome)` method.
Wire it into the `SettlementSweep` so every settled record updates the
calibrator. Expose the recent coverage rate as a `shadow_health` metric.

---

### 3. Causal Feature Engineering (do-calculus for features)

**Extends:** `causal_graph.py` → `CausalGraph`; `feature_lake.py`

**Research:** `research/papers/2024/hartford-causal-inference.md`

**The idea:** The `CausalGraph` already exists as a structural scaffold.
Wire it into feature engineering to produce **causal features** —
features that represent the effect of an intervention, not just a
correlation.

**How it fits:**

```
  CausalGraph (existing):
    Fed-rate-hike ──CAUSES──▶ AAPL (strength=0.7, lag=1h)
    TECH sector    ──INFLUENCES──▶ AAPL (strength=0.8)

  Proposed causal features:
    FeatureValue("causal_fed_effect_on_AAPL",
                 value = graph.intervention_effect(
                     source="Fed-rate-hike",
                     target="AAPL",
                     observed_at=t),
                 observed_at=t)
```

The causal feature is the **predicted effect of a detected event on the
target**, computed from the graph's edge strengths and lags. This is
PIT-safe if the event's `available_at_ns <= decision_time`.

**Why it's cutting-edge:** Standard ML features are correlational. A
model that learns "AAPL goes up when the Fed cuts rates" is learning a
correlation that may reverse. A causal feature encodes the *mechanism*,
which is more stable across regimes. The CausalGraph's `lag_ns` field
already supports lag-aware feature construction.

**Effort:** 3-4 weeks. Requires a causal inference layer on top of the
graph (do-calculus or a structural equation model). The graph structure
exists; the inference engine does not.
**Risk:** Medium-high. Causal inference in finance is hard — the graph
is incomplete, confounders are everywhere, and the effect estimates are
noisy. Start with the graph as a **feature selector** (only include
features for symbols that have a causal path from a detected event)
before attempting full do-calculus.

**Concrete next step:** Use the CausalGraph as a **feature filter** —
only emit features for (symbol, feature) pairs where there's a causal
path from a recently-observed event. This is a cheap first step that
doesn't require a full causal inference engine.

---

### 4. PatchTST as a Shadow Model Family (time-series transformers)

**Extends:** `alpha_genome.py` → `ALLOWED_MODEL_FAMILIES`; `real_trainer.py`
→ new `PatchTSTTrainer` satisfying `TrainerProtocol`

**Research:** `research/papers/2025/nie-patchtst.md`,
`research/models/timesfm-google.md`, `research/models/chronos-amazon.md`

**The idea:** The current trainer only supports LightGBM (tabular).
PatchTST — a patch-based transformer that treats sub-sequences as
tokens — achieves ~20% improvement over prior SOTA on long-term
forecasting benchmarks. It's a **single-series** model (not a foundation
model), so it can be fine-tuned on per-symbol data.

**How it fits:**

```
  Current:
    TrainerProtocol.train(req, deadline_ns) → (ArtifactManifest, ModelDossier)
    └── RealLightGBMTrainer (tabular, binary classification)

  Proposed:
    TrainerProtocol.train(req, deadline_ns) → (ArtifactManifest, ModelDossier)
    ├── RealLightGBMTrainer     (tabular baseline)
    └── PatchTSTTrainer         (time-series, sequence input)
        ├── loads raw price/volume sequences (not tabular features)
        ├── patches the sequence (patch_len=16, stride=8)
        ├── transformer encoder over patches
        └── outputs direction probability → same binary label
```

The key insight: PatchTST needs **sequence data**, not tabular features.
This requires a new dataset format — a third option alongside CSV and
Parquet: **sequence arrays** (`.npy` or `.npz`). The
`dataset_manifest_ref` URI scheme already supports `file://` and `s3://`;
adding `.npy` support to `_load_dataset()` is a small extension.

**Why it's cutting-edge:** GBMs are the right first family (fast,
interpretable, cheap to retrain — the system's own docs say this). But
GBMs can't model **temporal dynamics** — they see each row as
independent. PatchTST captures the sequential structure of price paths.
The Alpha Genome can search over both families and let the tournament
decide which wins per regime.

**Effort:** 2-3 weeks (model implementation + sequence dataset format +
RunPod container with PyTorch).
**Risk:** Low. PatchTST is channel-independent (each series processed
separately), which maps cleanly to the per-symbol training model. The
tournament already supports multiple model families — the `model_family`
field is an allowlist.

**Concrete next step:** Add `patchtst` to `ALLOWED_MODEL_FAMILIES`.
Implement `PatchTSTTrainer` with the same `TrainerProtocol`. Add
`.npy` sequence loading to `_load_dataset()`. Run it in shadow against
the GBM baseline and let the tournament compare DSR.

---

### 5. Distributionally Robust Optimization (DRO) for the Tournament

**Extends:** `tournament.py` → `Tournament.score()`; `outcomes.py` →
`CostModel`

**Research:** `research/papers/2025/namkoong-distributionally-robust.md`

**The idea:** The tournament currently ranks on the **empirical** mean
net edge. DRO replaces the empirical expectation with a **worst-case**
expectation over all distributions within ε-KL divergence of the
empirical:

```
  Current:  net_edge = mean(oos_returns_net)
  DRO:      net_edge = min_{P : KL(P || P_emp) ≤ ε} E_P[returns]
```

**How it fits:**

```
  Tournament.score(ScoringInput)
    ├── Component 1: net_edge (currently empirical mean)
    │
    └── Proposed: net_edge_robust = DRO_edge(oos_returns, ε=0.1)
                   = worst-case mean over ε-KL neighborhood
```

The DRO edge is always ≤ the empirical edge, so it's a **conservative**
replacement. A model that looks good only because the empirical
distribution happened to be favorable will have a much lower DRO edge.
The ε parameter controls conservatism — the Alpha Genome can search
over it.

**Why it's cutting-edge:** DRO gives explicit control over
robustness to regime change. The worst-case distribution may be a
2008-style or 2020-style crisis that's within ε-KL of the empirical
distribution. A model that survives the worst case is more trustworthy
than one that survives the average.

**Effort:** 2-3 weeks (implement the KL-DRO saddle-point solver; it's a
convex problem with a closed-form solution for the mean).
**Risk:** Medium. DRO is more conservative by design — it will reject
some models that would have been profitable. But that's the point: the
tournament is already designed to be conservative (DSR, PBO, gates).

**Concrete next step:** Add `dro_edge(returns, epsilon)` as an
alternative `net_edge` computation. Run it in parallel with the
empirical edge and compare tournament rankings. If the DRO ranking is
more stable out-of-sample, switch the `net_edge` component to DRO.

---

### 6. Thompson Sampling for Strategy Allocation (bandit meta-router)

**Extends:** `moe_router.py` → `MoERouter`; sits above the tournament

**Research:** `research/papers/2026/thompson-sampling-bandit.md`

**The idea:** The MoE router currently uses rule-based routing from
tournament evidence. Thompson sampling replaces the rules with a
**Bayesian bandit** that maintains a posterior over each expert's
expected return and samples from it at each rebalance:

```
  For each expert e:
    posterior_e = Beta(alpha_e, beta_e)  or  Normal(μ_e, σ²_e)

  At each rebalance:
    1. Sample θ_e ~ posterior_e for each expert
    2. Route to the expert with the highest θ_e
    3. Observe realized return r
    4. Update posterior: alpha_e += r, beta_e += (1 − r)  (Beta)
                        or  μ_e, σ²_e update (Normal)
```

**How it fits:**

```
  Current MoE:
    RoutingContext → rule-based scores → expert weights

  Proposed:
    RoutingContext → Thompson posteriors (per regime) → sampled weights
    ├── per-regime posteriors (a model may be good in "low-vol" but bad in "high-vol")
    ├── posterior updates from SettlementRecord outcomes
    └── exploration/exploitation balance (Thompson sampling is naturally Bayesian)
```

The `SettlementLedger` already produces per-model realized returns.
The Thompson posteriors update from the rolling 30-day settled returns
per expert, per regime. The `DriftSentinel`'s `LIVE_EDGE_DECAY`
indicator triggers a posterior reset (the bandit re-explores when edge
decays).

**Why it's cutting-edge:** Thompson sampling handles regime change
better than UCB (the paper shows ~30% lower regret) because the
posterior "tracks" the best strategy more quickly. It's also naturally
Bayesian — the uncertainty in the posterior encodes "how much do we
still not know about this expert," which maps to the conformal gate's
abstain logic.

**Effort:** 1-2 weeks. The MoE router infrastructure exists; this is
replacing the rule-based gate with a posterior-based gate.
**Risk:** Low. Thompson sampling is well-understood and the
infrastructure (settlement records, per-model returns) already exists.

**Concrete next step:** Add `ThompsonMoERouter` as an alternative to
the rule-based `MoERouter`. Initialize priors from the tournament
evidence (the rule-based scores become the prior means). Update
posteriors from the settlement sweep. Compare the two routers in shadow.

---

### 7. Diffusion-Generated Adversarial Scenarios (stress testing)

**Extends:** `sentinel.py` → `LeakageSentinel`; new `stress_test.py`

**Research:** `research/papers/2025/diffusion-financial-scenarios.md`

**The idea:** Train a score-based diffusion model on historical
returns, then generate **adversarial scenarios** — synthetic time
series that preserve the statistical properties of the historical
distribution (return moments, volatility clustering, fat tails) but
represent worst-case regimes.

**How it fits:**

```
  Historical returns → Diffusion model (score-based)
        │
        ▼
  Generate N synthetic 1-year scenarios
        │
        ├── Scenario 1: "2008-style crisis" (conditioned on regime)
        ├── Scenario 2: "2020-style flash crash"
        ├── Scenario 3: "2022-style bear market"
        └── ...
        │
        ▼
  Run each model on each scenario (shadow inference)
        │
        ▼
  Worst-case Sharpe, worst-case drawdown, worst-case Kelly
        │
        ▼
  Feed into tournament as a new penalty component:
    adversarial_robustness_penalty = f(worst-case drawdown)
```

**Why it's cutting-edge:** Current stress testing (if any) uses
historical scenarios — but history doesn't cover all possible futures.
Diffusion models generate **plausible but novel** scenarios that the
model has never seen. A model that survives a diffusion-generated
"2008-but-worse" scenario is more robust than one that survives the
actual 2008 (which it may have been trained on).

**Effort:** XL (4-6 weeks). Training a diffusion model is non-trivial,
and validating that the generated scenarios are realistic requires
statistical tests (moment matching, autocorrelation structure).
**Risk:** High. The generated scenarios may not be realistic enough to
be useful. Start with a simpler GAN or even a block-bootstrap
augmentation before attempting full diffusion.

**Concrete next step:** Before diffusion, implement a **block-bootstrap
stress test** — resample blocks of historical returns to create
adversarial scenarios with different regime mixtures. This is much
cheaper and uses the same stationary bootstrap infrastructure as the
significance test. Add a `worst_case_drawdown` component to the
tournament score. Diffusion is the eventual upgrade.

---

### 8. Online Learning with Concept-Drift Detection

**Extends:** `drift_sentinel.py` → `DriftSentinel`; `real_trainer.py`

**Research:** `research/papers/2025/concept-drift-survey-gama.md`

**The idea:** The current system is **batch-train then shadow-evaluate**.
Online learning would continuously update the model as new data arrives,
with concept-drift detection triggering retraining.

**How it fits:**

```
  Current:
    Train (batch) → Shadow inference → Settle → Tournament → Promote
    (model is frozen between training runs)

  Proposed:
    Train (batch) → Shadow inference → Settle → Online update
    ├── if drift detected (DriftSentinel):
    │   ├── mild → online update (incremental LightGBM)
    │   ├── moderate → shadow-only + queue for retrain
    │   └── severe → retire + retrain from scratch
    └── else: continue with frozen model
```

LightGBM supports incremental training (`model.update()`) — the model
can be updated with new data without a full retrain. The
`DriftSentinel`'s `RETRAIN` recommendation becomes the trigger for an
incremental update rather than a full retrain.

**Why it's cutting-edge:** Financial markets drift. A model trained on
2024 data may be stale by mid-2025. Online learning keeps the model
current without the full RunPod dispatch cycle. The key is detecting
*when* to update (concept drift) vs. when the model is still good.

**Effort:** 2-3 weeks (incremental LightGBM is built-in; the work is
the drift-triggered update loop and the versioning of incrementally-
updated artifacts).
**Risk:** Medium. Online learning can chase noise — the drift detector
must be conservative. The `DriftSentinel` already has severity levels;
the update trigger should only fire on `HIGH` or `CRITICAL`.

**Concrete next step:** Add `incremental_update(new_data)` to
`RealLightGBMTrainer`. Wire the `DriftSentinel`'s `RETRAIN`
recommendation to trigger an incremental update (not a full retrain)
when severity is `HIGH`. Keep the full retrain path for `CRITICAL`.
Version the incrementally-updated artifact with a new
`incremental_version` field on the `ArtifactManifest`.

---

### 9. Multi-Modal LLM Features (text → signal)

**Extends:** `feature_lake.py`; `alpha_genome.py` → `ALLOWED_TRANSFORMS`

**Research:** `research/papers/2025/li-multimodal-llm-trading.md`,
`research/papers/2026/llm-transcript-earning.md`,
`experiments/news-impact-model/`

**The idea:** The news-impact experiment already has a
`HashingTextEmbedder` — a dependency-free text encoder. Replace it with
a real LLM embedding (FinBERT, a fine-tuned small LLM, or an API-based
embedding) and add the embedding as feature columns.

**How it fits:**

```
  Current (news-impact experiment):
    NewsEvent.headline → HashingTextEmbedder → 256-dim vector
    → analog retrieval → ImpactLabels

  Proposed:
    NewsEvent.headline + body → LLM embedder → 768-dim vector
    → FeatureValue("llm_embed_0", ..., observed_at=available_at_ns)
    → FeatureValue("llm_embed_1", ..., observed_at=available_at_ns)
    → ... (768 features)
    → FeatureRow with LLM features alongside price/volume features
    → LightGBM trains on combined tabular + LLM features
```

The `available_at_ns` on the `NewsEvent` becomes the `observed_at` on
the LLM feature values — PIT-safe by construction. The LLM embedding is
computed at feature-lake build time, not at inference time, so there's
no latency cost at inference.

**Why it's cutting-edge:** Text features capture information that price/
volume features cannot — earnings call sentiment, Fed statement tone,
news urgency. The news-impact experiment already proved the retrieval
pipeline; adding LLM embeddings to the feature lake is the natural
extension.

**Effort:** 2-3 weeks (LLM embedding pipeline + feature lake extension
+ dimensionality reduction for the 768-dim vectors).
**Risk:** Medium. LLM embeddings are high-dimensional and may overfit.
Use PCA or feature selection (the Alpha Genome can search over which
embedding dimensions to include). Start with FinBERT (domain-specific,
smaller) before a general LLM.

**Concrete next step:** Replace `HashingTextEmbedder` with a FinBERT
embedding in the news-impact experiment. Add the embedding dimensions
as `FeatureValue`s in the feature lake. Let the Alpha Genome search
over which dimensions to include. Compare the DSR of the LLM-enhanced
model to the baseline.

---

### 10. Meta-Learning Across Model Families (learn-to-learn)

**Extends:** `alpha_genome.py` → `AlphaGenomeLab`; `tournament.py`

**The idea:** The Alpha Genome searches over recipes by mutation.
Meta-learning would train a **meta-model** that learns which recipes
work best for which regimes — a model that predicts the tournament
score of a recipe before training it.

**How it fits:**

```
  Current Alpha Genome:
    Parent Recipe → mutate → train → evaluate → keep/discard

  Proposed:
    History of (Recipe, regime, tournament_score) pairs
        │
        ▼
    Meta-model (e.g., gradient-boosted meta-learner)
    "Given a recipe config + current regime, predict tournament_score"
        │
        ▼
    Alpha Genome uses meta-model to PRIORITIZE mutations:
    ├── high predicted score → train first (exploitation)
    └── high uncertainty → train to learn (exploration)
```

This is essentially a **Bayesian optimization** layer over the Alpha
 Genome. The meta-model is trained on the history of all recipes that
have been trained and evaluated — the tournament results are the
labels.

**Why it's cutting-edge:** The Alpha Genome currently mutates blindly
(within allowlists). Meta-learning makes it **directed** — it learns
which feature sets, hyperparameters, and windows work best for which
regimes. This is the difference between random search and informed
search.

**Effort:** 3-4 weeks (meta-model training + Bayesian optimization loop
+ integration with the Alpha Genome's `EarlyStopper`).
**Risk:** Medium. The meta-model needs enough history to be useful —
at least 100+ trained recipes. Start collecting the (recipe, score)
history now even if the meta-model isn't built yet.

**Concrete next step:** Log every `(Recipe, RoutingContext.regime,
TournamentResult.total_score)` tuple to a durable store. After 100+
entries, train a simple meta-learner (e.g., LightGBM on the recipe
config + regime features → predicted score). Use it to prioritize the
Alpha Genome's mutation order.

---

## Part III — Improvement Priority Matrix

| # | Improvement | Effort | Risk | Impact | Depends on |
|---|---|---|---|---|---|
| 2 | Adaptive Conformal (ACI) | 1-2 wk | Low | High | Existing conformal gate |
| 6 | Thompson Sampling MoE | 1-2 wk | Low | High | Existing MoE + settlement |
| 1 | Path Signatures | 1-2 wk | Med | Med | Existing feature lake |
| 4 | PatchTST model family | 2-3 wk | Low | High | Trainer protocol |
| 5 | DRO tournament edge | 2-3 wk | Med | High | Existing tournament |
| 8 | Online learning + drift | 2-3 wk | Med | Med | Existing drift sentinel |
| 9 | LLM text features | 2-3 wk | Med | Med | News-impact experiment |
| 3 | Causal features | 3-4 wk | Med-High | High | Existing causal graph |
| 10 | Meta-learning Alpha Genome | 3-4 wk | Med | High | 100+ recipe history |
| 7 | Diffusion scenarios | 4-6 wk | High | Med | Block-bootstrap first |

**Recommended order:**
1. **Adaptive Conformal (2)** — cheapest, lowest risk, immediate
   improvement to uncertainty quantification.
2. **Thompson Sampling MoE (6)** — cheap, low risk, replaces rules with
   Bayesian posteriors that track regime change.
3. **Path Signatures (1)** — cheap, novel feature class that slots into
   the existing schema.
4. **PatchTST (4)** — the first non-GBM model family; the tournament
   infrastructure already supports multiple families.
5. **DRO (5)** — makes the tournament robust to distributional shift.

Items 7-10 are longer-term and benefit from the infrastructure built by
items 1-6.

---

## Part IV — The Deep File Map

```
services/quant_foundry/src/quant_foundry/
│
├── DATA LAYER
├── feature_lake.py              # FeatureRow, FeatureValue, UniverseEntry (PIT)
├── feature_availability.py      # per-feature availability reporting
├── feature_snapshot_export.py   # compact snapshots for inference
├── dataset_manifest.py          # FeatureLakeManifest, PurgedFoldSpec, FoldBoundary
├── training_manifest.py         # operator-facing TrainingManifest
├── market_data_adapter.py       # BarDataAdapter, PricePoint (raw prices)
│
├── TRAINING LAYER
├── runpod_training.py           # RunPodTrainingHandler, LocalTrainer, TrainerProtocol
├── real_trainer.py              # RealLightGBMTrainer (walk-forward, metrics)
├── baseline_family.py           # workflow orchestration (train → validate → register)
├── alpha_genome.py              # automated recipe mutation + search
│
├── EVALUATION LAYER
├── significance.py              # DSR + stationary bootstrap p-value
├── pbo.py                       # PBO via CSCV (Bailey et al. 2017)
├── tournament.py                # weighted score → TournamentResult
├── leaderboard.py               # ranked leaderboard
├── leaderboard_expanded.py      # per-regime/per-cluster expanded leaderboard
├── outcomes.py                  # SettlementRecord, CostModel (versioned)
├── settlement.py                # SettlementLedger
├── settlement_sweep.py          # batch settlement
│
├── DEFENSE LAYER
├── sentinel.py                  # LeakageSentinel (shuffled labels, time-reverse, PBO)
├── drift_sentinel.py            # DriftSentinel (feature/calibration/provider/edge drift)
├── conformal_gate.py            # ConformalCalibrator + ConformalGate (uncertainty)
│
├── GOVERNANCE LAYER
├── promotion.py                 # PromotionGate, PromotionReviewQueue (human-gated)
├── retirement.py                # model retirement
├── paper_bridge.py              # shadow → paper (first dangerous connection)
├── dossier.py                   # DossierRecord, DossierBuilder, DossierStore
├── registry.py                  # DossierRegistry
│
├── ROUTING LAYER
├── moe_router.py                # Mixture-of-Experts router (regime-aware)
├── causal_graph.py              # CausalGraph (structural scaffold)
│
├── INFERENCE LAYER
├── shadow_inference.py          # ShadowInferenceEngine (stub)
├── real_inference.py            # RealInferenceEngine (ONNX/LightGBM loading)
│
├── TRANSPORT LAYER
├── schemas.py                   # all cross-boundary contracts (frozen, extra=forbid)
├── signatures.py                # HMAC-SHA256 sign/verify
├── runpod_client.py             # HttpRunPodClient, RunPodDispatcher, BudgetGuard
├── inbox.py                     # CallbackInbox (durable JSONL)
├── outbox.py                    # JobOutbox (durable JSONL)
├── callbacks.py                 # CallbackProcessor (verify → domain effect)
├── gateway.py                   # QuantFoundryGateway (facade)
├── ids.py                       # hash_payload, artifact_id generation
├── artifacts.py                 # ArtifactRecord, import_artifact
```

---

## Part V — TL;DR

**The architecture is a closed evidence loop, not a training pipeline.**
Models train, get scored on settled out-of-sample performance, and are
promoted or retired based on statistical evidence. Seven defense layers
( PIT proof, as-of universe, purged-k-fold, leakage sentinel, PBO, DSR,
drift sentinel) protect against leakage, overfitting, and drift.

**The ten cutting-edge improvements, in priority order:**

1. **Adaptive Conformal** — online interval recalibration that tracks
   regime change (1-2 wk, low risk).
2. **Thompson Sampling MoE** — Bayesian bandit replaces rule-based
   routing (1-2 wk, low risk).
3. **Path Signatures** — universal feature class from rough path theory
   (1-2 wk, medium risk).
4. **PatchTST** — time-series transformer as a second model family
   (2-3 wk, low risk).
5. **DRO Tournament** — worst-case edge replaces empirical edge
   (2-3 wk, medium risk).
6. **Online Learning** — drift-triggered incremental updates
   (2-3 wk, medium risk).
7. **LLM Text Features** — FinBERT embeddings as feature columns
   (2-3 wk, medium risk).
8. **Causal Features** — do-calculus features from the causal graph
   (3-4 wk, medium-high risk).
9. **Meta-Learning** — Bayesian optimization over the Alpha Genome
   (3-4 wk, medium risk).
10. **Diffusion Scenarios** — adversarial stress testing with synthetic
    data (4-6 wk, high risk).

Every improvement slots into the existing architecture without breaking
the security boundary, the frozen contracts, or the seven defense
layers. The system was designed for this: the `TrainerProtocol`, the
`ALLOWED_MODEL_FAMILIES` allowlist, the `ALLOWED_TRANSFORMS` set, the
tournament's weighted components, and the Alpha Genome's mutation engine
are all extension points waiting to be used.
