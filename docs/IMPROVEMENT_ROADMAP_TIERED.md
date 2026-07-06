# Fincept / Quant Foundry — Tiered Improvement Roadmap

Date: 2026-07-04
Author: post-A7 system review (written immediately after the full training
pipeline was proven live on the `6dbec436` image)
Scope: the whole platform — RunPod training fleet, quant_foundry, the
monorepo services (`ingestor`, `features`, `backtester`, `risk`, `oms`,
`portfolio`, `api`, `agents`, `orchestrator`), libs (`fincept-core`,
`fincept-bus`, `fincept-db`, `fincept-sdk`), the Next.js dashboard, CI, and
the agent-swarm operating process itself.

Related docs: `docs/SYSTEM_IMPROVEMENT_REPORT.md` (2026-06-21 repo audit —
safety-guard and path-boundary findings there are still valid),
`docs/runpod-fix-plan/RECEIPT_INDEX.md` (investigation evidence),
`docs/TRAINING_ANALYSIS.md`, `research/` (anti-curation methodology).

---

## Where the system actually is (grounded baseline)

What is **proven live** as of today:

- The RunPod training worker boots, picks up jobs, and stays healthy
  (6/6 canaries, A6 gpu_healthcheck, A7 train_model — all on the exact-SHA
  `6dbec436` production image).
- The full training pipeline works end-to-end in isolation: inline dataset
  → `RealLightGBMTrainer` walk-forward validation → final fit → pickle
  export with sha256 re-verification + HMAC write receipt + signed typed
  callback.
- **Training is bit-deterministic across environments** — the A7 live run
  and a local in-process run of the same payload produced the *identical*
  model sha256 (`ac0b69ba...`). This is rare and strategically valuable
  (see Tier 3/4).
- Security posture in the worker is genuinely good: fail-closed preflight,
  no broker/Redis creds, redacted receipts, HMAC-signed contracts.
- The operating process itself (receipt bundles, `RECEIPT_INDEX.md`,
  receipt-integrity pytest guard, swarm task queues v1–v10) is an unusual
  and valuable asset — most of Tier 4 builds on it.

What is **not yet real**:

- No product flow: nothing dispatches training jobs from the platform, and
  nothing ingests the signed callbacks. A7's artifact was written to the
  worker's `/tmp` — it died with the worker. There is no durable model
  registry.
- The GPU is paid for but idle during training: the trainer backend is
  CPU lightgbm (the PyPI wheel is CPU-only); xgboost/catboost GPU are
  installed in the image but unused.
- Anti-overfitting stats are placeholders: `pbo_method=fold_overfit_ratio`
  and `deflated_sharpe_method=sharpe_times_1_minus_fold_overfit_ratio` are
  crude stand-ins, and the Sharpe on the A7 canary (769) shows the metric
  pipeline needs sanity bounds.
- Known debt: leaked Stripe secret (Trivy CRITICAL), 1334 pre-existing Ruff
  errors on `ci`, `next` needs a security bump, ~6 GB image causes 155s+
  cold pulls.

---

## Tier 0 — Do now (proven, days, mostly no spend)

| #   | Item                                                                                                                                                                  | Why / grounding                                                                                                                                                                                                                                      |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0.1 | **Rotate the leaked Stripe key and purge it from git history**; bump `next` in `apps/dashboard`                                                                       | Trivy CRITICAL on `main`. Security debt compounds; everything else is secondary. (Tasks D1/D2 in the v10 queue.)                                                                                                                                     |
| 0.2 | **Durable artifact upload from the worker** — attach a RunPod network volume (or push to S3/R2 via presigned URL passed in the request) and set `output_prefix` to it | A7's model went to `/tmp` and is gone. `VolumeArtifactWriter` already exists and works; this is config + a URI allowlist entry, not new code. Without it, every training job's output is disposable.                                                 |
| 0.3 | **Set the RunPod endpoint job timeout ≥ 1860s in the template**                                                                                                       | The handler enforces `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800`; RunPod's 600s default would `TIMED_OUT` a real job before the handler's signed failure envelope fires (task C10). A7 only passed because it ran in 1.6s.                        |
| 0.4 | **Slim the training image** — the ~6 GB torch-cu124 wheel is in the image but lightgbm training doesn't use torch                                                     | Attempt #1 of A7 failed purely on a cold pull exceeding 180s. A lightgbm/xgboost-only image is <1.5 GB → cold starts drop from minutes to seconds, and spot capacity churn hurts less. Keep a separate `-torch` tag for future NN work.              |
| 0.5 | **CI lint debt burn-down on a separate branch off `main`**                                                                                                            | 1334 pre-existing Ruff errors mask real regressions (`ruff --fix` auto-fixes ~613). A red-always CI trains people to ignore CI. (Task C8.)                                                                                                           |
| 0.6 | **Metric sanity bounds in the callback**                                                                                                                              | Sharpe 769 should be flagged `implausible` by the worker itself (e.g., clamp/annotate                                                                                                                                                                |
| 0.7 | **Endpoint/template lifecycle in code**                                                                                                                               | The A7 run hit both a template-name collision and a transient `deleteEndpoint` failure. `run_train_model.py` now handles both — extract that into a shared `runpod_lifecycle.py` helper so every future tool inherits unique naming + retry cleanup. |

---

## Tier 1 — Standard, proven industry practice (weeks)

These have been done a thousand times; the only question is sequencing.

### 1.1 Callback ingestion service (closes the product loop)

A small FastAPI endpoint (or handler in `services/api`) that receives the
worker's signed callback, verifies the HMAC (`quant_foundry.signatures`),
and writes the dossier + artifact manifest into `fincept-db`. The worker
already supports `QUANT_FOUNDRY_CALLBACK_URL` with host validation — the
receiving side simply doesn't exist yet. This is the single highest-leverage
build item in the repo: it converts "training works in isolation" into
"training is a platform capability."

### 1.2 Model registry with promotion workflow

Tables: `models`, `model_versions`, `promotions`. States:
`shadow-only → candidate → production → retired`. The worker already emits
`promotion_eligible`, `authority: shadow-only`, dossiers, and quality-gate
results — the registry just persists and enforces them. Proven pattern
(MLflow Model Registry, SageMaker Registry); build it on `fincept-db` to
keep the receipt discipline.

### 1.3 Use the GPU you pay for

The image already ships GPU xgboost (`device="cuda"`) and catboost
(`task_type="GPU"`). `model_family` is already in the request schema.
Adding an xgboost-GPU backend to the real trainer gives 5–20x on wide
datasets for near-zero infra work. (LightGBM GPU requires a custom build —
skip it; xgboost is the proven path.)

### 1.4 Hyperparameter search inside the worker

`search_space` is already in `RunPodTrainingRequest`. Wire Optuna (proven,
lightweight) with a trial budget + the existing deadline enforcement, and
record **every trial** in the dossier metadata. The trial count feeds the
deflated Sharpe fix in Tier 2 — do not skip recording it.

### 1.5 Dataset registry (point-in-time correctness)

`dataset_manifest_hash`, FoldSpec manifests, and the quality-gate module
(`data_ingestion/quality_report.py`) already exist. Formalize: every
training dataset is an immutable parquet + manifest row (hash, time range,
feature schema hash, quality report) in `fincept-db`. `inline_dataset_csv`
stays as a test-only path. This is the standard defense against silent
lookahead/survivorship bugs.

### 1.6 Observability + cost accounting

Per-job structured metrics: queue delay, execution time, GPU utilization,
$ cost (RunPod bills per-second). Emit from the handler into the callback;
aggregate in the dashboard. Proven stack: Prometheus/Grafana or even a
simple Timescale table + dashboard page. The A7 receipts show you already
capture delayTime/executionTime — persist them.

### 1.7 Scheduled + drift-triggered retraining

A `jobs` service cron that dispatches training via the (new) dispatcher
when (a) N days elapsed, or (b) feature/label drift exceeds a threshold
(PSI/KS tests — standard). Depends on 1.1/1.2/1.5.

---

## Tier 2 — Advanced, proven at top quant firms (months)

### 2.1 Combinatorial Purged Cross-Validation (CPCV) + real PBO

Current walk-forward + purge (F2 fix) is correct but minimal, and
`pbo_method=fold_overfit_ratio` is a placeholder. Implement CPCV and the
actual Probability of Backtest Overfitting estimator (Bailey, Borwein,
López de Prado, Zhu). `fincept_core.datasets.cv.make_folds` is the right
canonical home. This is *the* thing separating serious quant validation
from Kaggle-style validation.

### 2.2 Honest Deflated Sharpe Ratio

DSR requires the **number of trials** and the variance across trials.
Today's `sharpe_times_1_minus_fold_overfit_ratio` is not DSR. Once 1.4
records every Optuna trial and the registry records every experiment, DSR
becomes computable honestly. Pairs with `research/_meta/ANTI_CURATION.md`,
which already commits the project philosophically to this.

### 2.3 Triple-barrier labeling + meta-labeling

The synthetic A7 label was a toy. For real forward-return data: triple
barrier (profit-take/stop/timeout) labels, then a meta-model that decides
*whether to act* on the primary model's signal. Proven (AFML), directly
compatible with the existing lightgbm/xgboost backends and the
`extra_constraints` mechanism (horizon_bars/purge_bars already resolve from
there).

### 2.4 Champion/challenger shadow deployment

`authority: shadow-only` and `prediction_log` already exist in the schema.
Wire challenger models to score live in shadow, log predictions
point-in-time, and auto-compare vs. the champion over a fixed window before
promotion. This is the standard, proven guardrail between "backtest looks
good" and "give it capital." Depends on 1.1/1.2.

### 2.5 Execution-aware backtesting

Integrate slippage/market-impact/fee models into `services/backtester` so
training metrics and backtest metrics share cost assumptions. The Sharpe-769
artifact demonstrates why frictionless metrics must never reach a promotion
decision.

### 2.6 Feature store with point-in-time joins

`services/features` exists. Formalize: versioned feature definitions,
materialized point-in-time tables, and a training-request field that pins a
feature-set version (extend the dataset manifest). Proven (Feast et al.),
but build thin on Timescale rather than adopting a heavy platform.

### 2.7 Checkpoint/resume + spot-fleet training

For longer jobs: periodic checkpoint upload to the volume, resume on
preemption, idempotency by `job_id` (the schema already carries one). Turns
spot-price GPUs into reliable capacity.

---

## Tier 3 — Cutting edge (few production examples anywhere)

### 3.1 Reproducibility attestation as a CI gate ("determinism proofs")

A7 proved bit-identical model hashes across two independent environments.
Almost nobody in ML can do this. Formalize it: a nightly/CI job trains the
same (dataset manifest, code SHA, seed) recipe on two independent workers
and **fails if the sha256s differ**. Any nondeterminism regression (library
bump, threading change, GPU nondeterminism) is caught the day it lands.
Prereqs: Tier 0.2 (durable artifacts). Cost: two tiny canary trainings/day.

### 3.2 SLSA-style provenance chain for models

Extend the existing receipt chain into a formal attestation graph
(in-toto/DSSE format): dataset manifest hash → feature schema hash → code
git SHA → container image digest → training receipt → model sha256 →
promotion decision → live order IDs. Every link already exists as a field
in the dossier/artifact manifest — the work is emitting standard, signed
attestation documents and a verifier CLI. Output: any model in production
can be audited end-to-end by a third party in minutes.

### 3.3 Machine-readable agentic ops mesh

The swarm process (task queues v1–v10, consolidation passes, receipt
integrity tests, BridgeMind MCP) is currently prose-driven. Make it
machine-native: task cards as JSON with acceptance predicates, agents claim
via MCP, receipts auto-verified by an expanded `test_receipt_integrity.py`,
and a "do-not-retry" ledger that agents must consult before dispatching
experiments (the RECEIPT_INDEX "What Failed" table, as data). The RunPod
investigation proved this loop works manually; automating it is genuinely
novel ops tooling.

### 3.4 LLM research-analyst loop with pre-registration

`services/agents` + `experiments/news-impact-model` + the anti-curation
docs point here already. The loop: LLM proposes a hypothesis → the
hypothesis is **hash-committed to a registry before evaluation** → the
experiment runs through the standard training pipeline → results are
recorded against the commitment whether good or bad → DSR uses the true
trial count. LLM-generated alpha research exists in labs; *pre-registered,
overfitting-accounted* LLM research does not, publicly.

### 3.5 Regime-aware model routing / online learning

A router that selects among registered models per detected regime
(volatility state, liquidity state), with online calibration of the routing
weights only (keeping the underlying models frozen and attested). Cutting
edge but tractable; depends on 1.2 and 2.4.

### 3.6 Confidential-compute training (TEE)

Run training inside SEV-SNP/TDX (or GPU TEE, H100 CC-mode) so the
determinism + provenance chain (3.1/3.2) is *hardware-attested*. Relevant
if models or data are ever shared with counterparties.

---

## Tier 4 — First-of-its-kind (the system is uniquely positioned)

### 4.1 The receipt-native trading platform

Combine 3.1 + 3.2 + the existing OMS/risk services into a single claim:
**every live order is cryptographically traceable to the exact dataset
bytes, code SHA, container digest, training run, validation stats, and
promotion decision that caused it — and any of it can be re-executed
bit-identically.** No retail or institutional platform ships this
end-to-end today. Regulator-grade, LP-due-diligence-grade, and it falls out
of infrastructure you have already half-built by accident of good
discipline.

### 4.2 Verifiable model recipes instead of model weights

Because training is bit-deterministic, a model can be distributed as a
*recipe* — (dataset manifest ref, code SHA, image digest, seed, params) —
whose output hash is publicly committed. Buyers/auditors reproduce the
model instead of trusting the weights. This inverts the normal ML trust
model and is only possible because of the determinism property proven in
A7. Nobody does this yet.

### 4.3 Public (or internal) pre-registered alpha ledger

4.2's commitment scheme applied to strategy research (3.4): an append-only
ledger of hypothesis commitments and outcomes, making the platform's
backtest statistics *provably* free of selection bias — the trial count in
the DSR is externally verifiable. First-of-its-kind honest-backtesting
infrastructure; also a differentiating marketing artifact.

### 4.4 Self-healing agent fleet with spend-gated autonomy

Full automation of what this investigation did manually: agents watch CI +
worker-health receipts, open task cards, implement fixes behind local
gates, and request operator approval only at spend/security boundaries
(the exact human-in-the-loop points used for A7). The RunPod fix history
(root-cause bisection → fix → live proof → consolidation) is the training
corpus for what "good" looks like.

### 4.5 zkML backtest integrity proofs (research horizon)

Zero-knowledge proofs that a backtest was executed faithfully over
committed data without revealing the strategy. Today this is research-grade
(proof costs are enormous for real workloads); track it, don't build it.
Listed for completeness because 4.1–4.3 put the platform closer to it than
almost anyone.

---

## Suggested sequencing

```
Tier 0 (all, in order 0.1 → 0.7)
   └→ 1.1 callback ingestion ──→ 1.2 model registry ──→ 2.4 shadow deploy
   └→ 1.5 dataset registry ───→ 2.1 CPCV/PBO ────────→ 2.2 honest DSR
   └→ 1.3 GPU backend          1.4 Optuna trials ─────┘
   └→ 0.2 durable artifacts ──→ 3.1 determinism gate ─→ 3.2 provenance chain ─→ 4.1 / 4.2
   └→ 3.3 ops mesh (parallel, no dependency on product flow)
```

Rule of thumb: nothing in Tier 2+ should start before 1.1/1.2 exist,
because without ingestion + a registry, every advanced validation stat is
computed and then thrown away — exactly what happened to the A7 artifact
in `/tmp`.

## What NOT to do (carried forward from the investigation)

- Do not switch training base images to `nvidia/cuda` or `runpod/base`
  (breaks RunPod job dispatch — disproven with receipts).
- Do not reintroduce a Docker `HEALTHCHECK` in the training image.
- Do not adopt a heavyweight MLOps platform wholesale (Kubeflow, SageMaker
  Pipelines) — the receipt/HMAC/manifest discipline here is *better* than
  what those give you, and porting onto them would destroy it. Adopt
  narrow, proven pieces (Optuna, object storage, Prometheus) instead.
- Do not compute promotion decisions from frictionless metrics (Sharpe 769
  is the standing reminder).
- Do not let `inline_dataset_csv` leak past test tooling into product flow.
