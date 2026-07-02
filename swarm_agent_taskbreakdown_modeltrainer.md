# Swarm Agent Task Breakdown — RunPod Model Trainer

Prepared: 2026-07-01

## Purpose

This file breaks down the three planning documents into discrete, claimable
swarm agent tasks. Each task has a stable ID, a status, an optional owner, and
a done gate so agents can see what is done, in progress, unclaimed, or reviewed.

Source plans consolidated here:

- `improvementplan_modeltrainer.md` (18 improvement tracks)
- `improvementplan_modeltrainer_modeltiers.md` (13 model tiers, build order A-F)
- `implementationroadmap_modeltrainer.md` (14 phases, 14 PRs)

## Status Legend

| Status        | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `unstarted`   | Task exists but no agent has claimed it.                       |
| `in_progress` | An agent has claimed the task and is actively working on it.   |
| `complete`    | The implementing agent believes the done gate is satisfied.    |
| `reviewed`    | A reviewer agent has verified the done gate and receipts.      |

## How To Claim A Task

1. Pick a task whose status is `unstarted` and whose dependencies are all
   `complete` or `reviewed`.
2. Set status to `in_progress` and fill in the `owner` field with your agent
   id.
3. Implement the task. Run the listed validation commands.
4. Set status to `complete` when you believe the done gate is satisfied.
5. A reviewer agent inspects, runs receipts, and either sets `reviewed` or
   moves it back to `in_progress` with a note.

## Non-Negotiable Invariants (apply to every task)

1. Training is RunPod GPU training. Local code may build manifests and verify
   receipts, but not stand in for remote training.
2. The trusted side owns promotion, broker access, Redis access, DB write
   access, and trading authority.
3. The RunPod worker is untrusted remote compute.
4. Every successful job must include artifact URI, sha256, size, format,
   runtime fingerprint, dataset manifest hash, training manifest hash, and
   prediction/evaluation receipt.
5. Every production dataset must be manifest-first, point-in-time verified,
   and quality-gated.
6. Every model family must declare dataset shape, artifact loader, metrics,
   RunPod image, budget, and promotion eligibility.
7. Every experiment beyond the tree baseline is shadow-only until it beats
   the tree stack after costs and calibration.
8. No callback is trusted unless the HMAC covers artifact, manifest, metrics,
   and runtime fields.
9. No worker input may use generic unallowlisted HTTP dataset fetches.
10. No result is promotion eligible unless the trusted side recomputes or
    verifies the reported metrics from artifacts.

---

## Phase 0 — Baseline, Ownership, And Acceptance Targets

Freeze the current surface and define the first receipts so the branch does
not turn into a broad research rewrite.

### T-0.1 Define Production, Canary, Research Modes

- status: `reviewed`
- owner: Builder 1 (swarm e757e69c600979)
- deps: —
- commit: `79decee`
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
- summary: Add mode semantics (`canary`, `research`, `production`) with
  validation. Production requires GPU, registered L3/L4 dataset, artifact
  verification, quality gates, no CPU fallback. Canary allows small datasets
  but is never promotion eligible. Research allows experimental families with
  promotion disabled unless escalated.
- validation:
  - Unit test request schemas reject production mode without GPU requirement.
  - Documentation states local training is not an acceptance substitute.
- done gate: Builders can point to one mode table when deciding what rules
  apply.
- maps to: Roadmap Phase 0.1; Improvement Track 0 (mode discipline).

### T-0.2 Add Roadmap Checklist Tracker

- status: `unstarted`
- owner: —
- deps: —
- files:
  - this file (`swarm_agent_taskbreakdown_modeltrainer.md`)
- summary: Maintain the swarm task tracker so work can be claimed in
  receipt-sized slices. Link to the three source plans.
- validation: Tracker links to roadmap and both reports.
- done gate: Work can be claimed in receipt-sized slices.
- maps to: Roadmap Phase 0.2.

### Phase 0 Exit Criteria

- Roadmap file exists.
- First three implementation PRs are identified: artifact contract,
  manifest-first dataset loading, GPU image preflight.

---

## Phase 1 — Artifact And Callback Contract

Make it impossible for a RunPod training job to report success without a
verified, loadable model artifact.

### T-1.1 Add Typed Artifact Result

- status: `reviewed`
- owner: Builder 2 (swarm e757e69c600979)
- deps: [T-0.1]
- commit: `0bbb583`
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - `runpod/quant-foundry-training/handler.py`
  - `services/quant_foundry/tests/`
- summary: Extend the training result type so it returns artifacts explicitly
  (artifact_id, artifact_uri, artifact_sha256, artifact_size_bytes,
  artifact_format, artifact_kind, loader_family, model_family,
  dataset_manifest_hash, training_manifest_hash, created_at). Remove handler
  reliance on `getattr(result, "model_bytes", None)`. Fail closed when a
  trainer returns no artifact for a successful job. Keep tiny inline bytes
  only for canary tests.
- validation:
  - Trainer success without artifact fails.
  - Trainer success with artifact URI passes.
  - Artifact sha mismatch fails.
- done gate: A success callback cannot be built without artifact
  URI/hash/size.
- maps to: Roadmap Phase 1.1; Improvement Track 1.

### T-1.2 Add Artifact Writer Interface

- status: `reviewed`
- owner: Builder 1 (swarm 940b5257e9a897)
- deps: [T-1.1]
- commit: `78dee43`
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
- summary: Create an artifact writer with two backends: RunPod volume path
  writer for canary/operator fallback, and presigned object upload writer for
  production path. Writer returns URI/hash/size/format. Worker signs returned
  artifact metadata.
- validation:
  - Fake writer computes expected sha.
  - Writer failure produces signed failure envelope.
  - Disallowed URI scheme is rejected.
- done gate: Worker artifact write is a typed side effect, not inline handler
  code.
- maps to: Roadmap Phase 1.2; Improvement Track 1 (Option B presigned upload).

### T-1.3 Trusted-Side Artifact Verifier

- status: `reviewed`
- owner: Builder 2 (swarm 940b5257e9a897)
- deps: [T-1.1, T-1.2]
- commit: `42be5e7`
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
  - callback verification code
- summary: Fetch artifact from returned URI. Recompute sha256 and size. Load
  artifact with declared loader family. Run deterministic smoke prediction
  against a frozen sample. Write artifact verification receipt.
- validation:
  - Corrupted artifact rejected.
  - Missing artifact rejected.
  - Unknown loader rejected.
- done gate: Trusted side marks callback `artifact_verified=true` only after
  load and hash checks pass.
- maps to: Roadmap Phase 1.3; Improvement Track 1.

### T-1.4 Add RunPodTrainingCallback Schema

- status: `reviewed`
- owner: Builder 1 (swarm 940b5257e9a897)
- deps: [T-1.1]
- commit: `767dbb1`
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
  - `runpod/quant-foundry-training/handler.py`
- summary: Add typed callback result contract: schema_version, job_id,
  training_manifest_hash, dataset_manifest_hash, runtime_fingerprint_hash,
  primary_artifact, auxiliary_artifacts, metrics_summary,
  promotion_eligible, failure_code, failure_reason. HMAC must cover artifact,
  manifest, metrics, and runtime fields.
- validation: Callback with missing required fields is rejected by trusted
  side.
- done gate: Every successful job returns a typed, signed callback.
- maps to: Roadmap Phase 1 contract additions.

### Phase 1 Receipts

- `callback_envelope.json`
- `artifact_manifest.json`
- `artifact_verification_receipt.json`
- `smoke_prediction.json`
- `failure_envelope.json` for negative tests

### Phase 1 Exit Criteria

- One RunPod canary job produces a fetchable, hash-verified artifact.
- One corrupted-artifact canary lands in DLQ or rejected state.

---

## Phase 2 — Manifest-First Dataset Loading

Bind the training job to the exact dataset bytes described by the
feature-lake manifest.

### T-2.1 Split Manifest URI From Data URI

- status: `reviewed`
- owner: Builder 1 (swarm e757e69c600979)
- deps: [T-0.1]
- commit: `5356059`
- files:
  - `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - `runpod/quant-foundry-training/handler.py`
- summary: Stop overloading `dataset_manifest_ref` as both manifest-ish id and
  data path. Add explicit `manifest_uri` and `data_uri`. Production dispatch
  requires both. Compatibility adapter may accept direct parquet only in
  canary mode. Add `DatasetLoadSpec` (manifest_uri, manifest_sha256, data_uri,
  data_sha256, data_format, row_count, feature_schema_hash, label_schema_hash,
  quality_report_uri, quality_report_sha256).
- validation:
  - Production request without manifest URI fails.
  - Manifest hash mismatch fails.
  - Direct CSV/parquet in production fails.
- done gate: Worker reads manifest first, not the raw data file first.
- maps to: Roadmap Phase 2.1; Improvement Track 3.

### T-2.2 Add ManifestDatasetLoader

- status: `reviewed`
- owner: Builder 2 (swarm 21af1e2de78590)
- deps: [T-2.1]
- commit: `8d04374`
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
  - `libs/fincept-core/src/fincept_core/datasets/`
- summary: Fetch manifest. Verify manifest sha. Fetch/load data from
  manifest-declared URI. Verify data sha and row count. Verify schema hashes.
  Return typed dataset frame plus column roles.
- validation:
  - Bad data checksum fails.
  - Bad row count fails.
  - Unknown data format fails.
  - Missing required column role fails.
- done gate: `RealLightGBMTrainer` and future trainers receive verified data,
  not a raw path string.
- maps to: Roadmap Phase 2.2; Improvement Track 3.

### T-2.3 Restrict Dataset URI Schemes

- status: `unstarted`
- owner: —
- deps: [T-2.1]
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
- summary: Allow approved RunPod volume roots, approved object store hosts,
  and `file://` only under configured worker data roots. Disallow
  localhost/private IP HTTP fetch, arbitrary public HTTP, and parent path
  traversal.
- validation:
  - `http://127.0.0.1/...` rejected.
  - `file:///etc/passwd` rejected.
  - approved test object URI accepted.
- done gate: Production worker cannot be used as a generic network fetcher.
- maps to: Roadmap Phase 2.3; Improvement Track 8 (URI allowlist).

### Phase 2 Receipts

- `dataset_load_receipt.json`
- `manifest_verification_receipt.json`
- `data_checksum_receipt.json`
- negative receipts for manifest/data mismatch

### Phase 2 Exit Criteria

- RunPod canary refuses mismatched data.
- RunPod canary trains only after manifest/data verification passes.

---

## Phase 3 — Dataset Registry, Readiness Levels, And Quality Gates

Make production RunPod dispatch use only registered datasets with explicit
readiness and quality policy.

### T-3.1 Add Dataset Registry

- status: `reviewed`
- owner: Builder 1 (swarm 21af1e2de78590)
- deps: [T-2.1]
- commit: `c806bd0`
- files:
  - `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
  - `services/quant_foundry/src/quant_foundry/feature_lake.py`
  - CLI/scripts that dispatch RunPod jobs
- summary: Start with JSONL or a small durable table. Register dataset id,
  manifest URI/hash, data URI/hash, quality URI/hash, source receipts,
  readiness level, upload receipt, and status. Provide commands: inspect,
  register, stage/upload, promote readiness, dispatch training.
- validation:
  - Duplicate dataset id rejected or versioned.
  - Unregistered production dispatch rejected.
  - Stale upload receipt rejected.
- done gate: Production dispatch accepts dataset id, not ad hoc raw file
  paths.
- maps to: Roadmap Phase 3.1; Improvement Track 4; Improvement Track 16.

### T-3.2 Add Quality Policy Files

- status: `reviewed`
- owner: Builder 1 (swarm 21af1e2de78590)
- deps: [T-3.1]
- commit: `ff28889`
- files:
  - `services/quant_foundry/src/quant_foundry/data_ingestion/quality_report.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
- summary: Add policies for canary, research, production. Policy checks
  include row count, symbol count, date span, label balance, feature
  coverage, fold validity, duplicate rows, PIT leakage, drift, and schema
  match.
- validation:
  - Missing quality report fails production.
  - Bad label balance fails production.
  - Canary policy allows small datasets but marks promotion ineligible.
- done gate: Every RunPod request carries `quality_policy_id`.
- maps to: Roadmap Phase 3.2; Improvement Track 6.

### T-3.3 Worker-Side QualityGateRunner

- status: `reviewed`
- owner: Builder 2 (swarm 21af1e2de78590)
- deps: [T-3.2, T-2.2]
- commit: `3d50634`
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
- summary: Worker reads manifest and quality report. Worker recomputes cheap
  data checks after loading. Worker fails before training if required gates
  fail. Worker emits signed quality failure callback.
- validation:
  - Worker rejects bad data even if trusted-side preflight was skipped.
  - Failure callback contains gate code.
- done gate: Bad datasets stop before GPU training begins.
- maps to: Roadmap Phase 3.3; Improvement Track 6.

### T-3.4 Strengthen ModuleComposer PIT Metadata

- status: `reviewed`
- owner: Builder 3 (swarm 21af1e2de78590)
- deps: [T-3.1]
- commit: `6b4b813`
- files:
  - `services/quant_foundry/src/quant_foundry/modules/composer.py`
- summary: Require data modules to emit observed_at, available_at, source
  vintage, as-of universe membership, corporate action adjustment version
  where relevant. Keep fixture shortcuts explicit as `fixture_mode=true`.
- validation:
  - Feature with `available_at > decision_time` rejected.
  - Delisted symbol valid only during historical membership.
  - Fixture-mode dataset cannot become L3/L4.
- done gate: Production readiness cannot be granted from flattened fixture
  timestamps.
- maps to: Roadmap Phase 3.4; Improvement Track 5.

### Phase 3 Receipts

- `dataset_registry_entry.json`
- `dataset_readiness_receipt.json`
- `quality_gate_receipt.json`
- `pit_verification_receipt.json`

### Phase 3 Exit Criteria

- One dataset reaches L2 and canary-trains.
- One dataset reaches L3 and is production-dispatch eligible.
- Fixture-like data cannot reach L3.

---

## Phase 4 — GPU Worker Image And Worker Split

Stop using a CPU-oriented worker as the production GPU trainer, and separate
dataset/utility tasks from training tasks.

### T-4.1 Add GPU Healthcheck Task

- status: `reviewed`
- owner: Builder 1 (swarm e757e69c600979)
- deps: [T-0.1]
- commit: `d161a97`
- files:
  - `runpod/quant-foundry-training/handler.py`
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
- summary: New `gpu_healthcheck` task. Record nvidia-smi, CUDA version,
  driver version, GPU model, GPU memory, training library GPU capability
  flags. Production mode fails if GPU is missing.
- validation:
  - CPU-only environment returns `gpu_capable=false`.
  - Production request with missing GPU fails.
  - Canary can report GPU absence but cannot be promotion eligible.
- done gate: Callback includes signed GPU runtime metadata.
- maps to: Roadmap Phase 4.1; Improvement Track 2.

### T-4.2 Build `trainer-gpu-tree` Image

- status: `reviewed`
- owner: Builder 1 (swarm 14dc196bce3d09)
- deps: [T-4.1]
- commit: `01bb6e2`
- files:
  - `runpod/quant-foundry-training/Dockerfile`
  - RunPod worker README/config scripts
- summary: Use CUDA-capable base image. Install and verify XGBoost GPU,
  CatBoost GPU, LightGBM GPU if reliable (otherwise record LightGBM as CPU
  baseline only). Add non-root user if volume permissions allow. Add startup
  security preflight.
- validation:
  - Image builds.
  - GPU healthcheck passes on RunPod GPU.
  - Small tree-training canary completes.
- done gate: First real GPU endpoint can train at least XGBoost or CatBoost
  on GPU.
- maps to: Roadmap Phase 4.2; Improvement Track 2; Model Tiers Tier 1.

### T-4.3 Split Dataset Utility Worker

- status: `reviewed`
- owner: Builder 2 (swarm 14dc196bce3d09)
- deps: [T-4.1]
- commit: `9d6e62f`
- files:
  - `runpod/quant-foundry-training/handler.py`
  - trusted-side RunPod endpoint config
- summary: Move volume writes, volume stats, volume listing, and ingestion
  tasks out of trainer handler. Trainer handler allowlist: `train_model`,
  `gpu_healthcheck`, `callback_secret_canary`. Unknown task types return
  signed failure.
- validation:
  - Volume write to trainer rejected.
  - Training request to dataset worker rejected.
  - Canary still signs callback.
- done gate: GPU endpoint no longer mutates arbitrary dataset staging state.
- maps to: Roadmap Phase 4.3; Improvement Track 7.

### Phase 4 Receipts

- `gpu_healthcheck_receipt.json`
- `worker_image_fingerprint.json`
- `task_rejection_receipt.json`

### Phase 4 Exit Criteria

- Production-mode training cannot run on CPU-only image.
- Dataset utility tasks no longer run on the trainer worker.

---

## Phase 5 — Security Hardening And Runtime Fingerprint

Treat RunPod as an untrusted compute boundary and prove exactly what runtime
created each model.

### T-5.1 SecurityPreflight

- status: `unstarted`
- owner: —
- deps: [T-4.3]
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
- summary: Refuse forbidden env vars (broker URLs, Redis URLs, database write
  URLs, trading credentials, cloud admin credentials not needed by the
  worker). Validate callback URL host. Validate dataset/artifact URI
  allowlists. Print redacted config summary. Record container user and
  writable dirs.
- validation:
  - Worker refuses `REDIS_URL`.
  - Worker refuses broker secret.
  - Worker redacts secret-like env names.
- done gate: Worker cannot start in production mode with app credentials
  present.
- maps to: Roadmap Phase 5.1; Improvement Track 8.

### T-5.2 Runtime Fingerprint

- status: `unstarted`
- owner: —
- deps: [T-4.1]
- files:
  - `runpod/quant-foundry-training/handler.py`
  - worker support modules
  - `services/quant_foundry/src/quant_foundry/runpod_training.py`
- summary: Add `runtime_fingerprint.json` with git sha, image digest,
  Dockerfile hash, dependency lock hash, Python version, OS image version,
  CUDA version, driver version, GPU model, training library versions, random
  seeds, dataset manifest hash, training manifest hash. Sign hash in
  callback.
- validation:
  - Production job fails if image digest is missing/placeholder.
  - Canary warns but marks promotion ineligible.
- done gate: Every successful job has a signed runtime fingerprint.
- maps to: Roadmap Phase 5.2; Improvement Track 14.

### T-5.3 Signed Failure Envelopes

- status: `unstarted`
- owner: —
- deps: [T-1.4]
- files:
  - `runpod/quant-foundry-training/handler.py`
  - callback verification code
- summary: Standardize failure schema: failure code, failure message,
  retryable, stage, signed context hashes. Send failure callback when
  possible.
- validation:
  - Dataset fetch failure signs failure.
  - Quality gate failure signs failure.
  - Artifact write failure signs failure.
- done gate: Trusted side can distinguish missing callback from signed worker
  failure.
- maps to: Roadmap Phase 5.3; Improvement Track 8.

### Phase 5 Receipts

- `security_preflight_receipt.json`
- `runtime_fingerprint.json`
- `signed_failure_envelope.json`

### Phase 5 Exit Criteria

- Security preflight blocks forbidden env vars.
- Production callbacks include non-placeholder runtime fingerprints.

---

## Phase 6 — Job Ledger, Callback DLQ, And Cost Telemetry

Create a durable trusted-side ledger connecting outbox id, RunPod job id,
dataset id, artifact id, callbacks, failures, retries, and cost.

### T-6.1 Add Training Job Ledger

- status: `done`
- owner: Builder 1 (swarm b07d386fe0e3d7) — commit 5968386f
- deps: [T-1.3]
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_client.py`
  - RunPod dispatcher/outbox code
  - callback gateway/ingestion code
- summary: Start append-only or table-backed. Record state transitions:
  queued, dispatched, runpod_running, callback_received, artifact_verified,
  rejected, failed, expired. Link to receipts.
- validation:
  - Dispatch creates ledger row.
  - Callback updates ledger row.
  - Artifact verification updates row.
- done gate: One job can be traced end to end without reading logs.
- maps to: Roadmap Phase 6.1; Improvement Track 15.

### T-6.2 Callback DLQ And Backoff

- status: `done`
- owner: Builder 2 (swarm b07d386fe0e3d7) — commit 0d10f9af
- deps: [T-6.1, T-5.3]
- files:
  - callback gateway/ingestion code
- summary: Rejected callbacks go to DLQ with reason: signature failed,
  missing required fields, artifact verify failed, duplicate callback, stale
  manifest. Retry policy for retryable failures. Idempotency key from job id
  plus manifest hash.
- validation:
  - Bad signature lands in DLQ.
  - Duplicate callback does not double-promote or double-verify.
  - Retryable failure schedules retry.
- done gate: Callback failures are observable and recoverable.
- maps to: Roadmap Phase 6.2; Improvement Track 15.

### T-6.3 Cost And Queue Telemetry

- status: `done`
- owner: Builder 3 (swarm b07d386fe0e3d7) — commit 7d002b3d
- deps: [T-6.1]
- files:
  - `services/quant_foundry/src/quant_foundry/runpod_client.py`
- summary: Estimate cost by GPU type and duration. Record queue time, image
  pull/start time where available, train time, artifact upload time,
  verification time. Add batch cost report.
- validation:
  - Cost computed for synthetic ledger rows.
  - Missing GPU price marks cost unknown, not zero.
- done gate: Operators can ask what a training batch cost and where time was
  spent.
- maps to: Roadmap Phase 6.3; Improvement Track 15.

### Phase 6 Receipts

- `job_ledger_row.json`
- `callback_dlq_entry.json`
- `cost_summary.json`

### Phase 6 Exit Criteria

- One successful and one failed RunPod job have complete ledger chains.

---

## Phase 7 — Production Tree Challengers

Upgrade the current LightGBM-shaped trainer into a serious tree-model
challenger lane: CatBoost GPU, XGBoost GPU, and the LightGBM baseline under
one contract.

### T-7.1 Model Family Registry

- status: `done`
- owner: Builder 1 (swarm f089940737008a) — commit f87122ae
- deps: [T-1.1, T-4.2]
- files:
  - `services/quant_foundry/src/quant_foundry/alpha_genome.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
- summary: Replace hardcoded allowlists with a versioned registry. Each
  family declares dataset shape, objectives, artifact format, loader,
  required metrics, RunPod image, max budget, promotion eligibility class.
  Initial families: `lightgbm_baseline`, `catboost_gpu`, `xgboost_gpu`,
  `logreg_sanity`, `linear_sanity`.
- validation:
  - Unknown family rejected.
  - Family without artifact loader rejected.
  - Production request family must map to GPU image or explicit baseline
    exception.
- done gate: Adding a model family is declarative and gated.
- maps to: Roadmap Phase 7.1; Improvement Track 12; Model Tiers "What To Add
  To The Existing Alpha Genome".

### T-7.2 CatBoost GPU Trainer

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit ed86ed9a
- deps: [T-7.1, T-4.2, T-8.1]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - worker image files
  - `services/quant_foundry/tests/`
- summary: Add trainer adapter. Support categorical column roles. Save
  artifact in CatBoost format. Emit feature importance. Emit fold metrics.
- validation:
  - Can train tiny canary on RunPod GPU.
  - Artifact loads and scores smoke sample.
  - Categorical role mismatch fails.
- done gate: CatBoost GPU is a promotion-eligible challenger once quality
  gates pass.
- maps to: Roadmap Phase 7.2; Model Tiers Tier 1.

### T-7.3 XGBoost GPU Trainer

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit a80087ce
- deps: [T-7.1, T-4.2, T-8.1]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - worker image files
  - `services/quant_foundry/tests/`
- summary: Add trainer adapter. Use CUDA tree method/device. Save artifact in
  JSON/UBJ format. Emit feature importance and metrics.
- validation:
  - Can train tiny canary on RunPod GPU.
  - Artifact loads and scores smoke sample.
  - GPU capability missing fails production.
- done gate: XGBoost GPU is a promotion-eligible challenger once quality
  gates pass.
- maps to: Roadmap Phase 7.3; Model Tiers Tier 1.

### T-7.4 Calibrated Probability Layer

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 1e2fcc73
- deps: [T-7.2, T-7.3]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - `services/quant_foundry/tests/`
- summary: Add Platt/isotonic calibration as post-fit step. Store
  calibration artifact. Emit ECE, Brier, logloss, reliability buckets.
- validation:
  - Calibration artifact present for classification.
  - Missing calibration marks promotion ineligible if policy requires it.
- done gate: Tree classifiers report calibrated confidence, not raw scores
  only.
- maps to: Roadmap Phase 7.4; Improvement Track 13; Model Tiers Tier 2.

### Phase 7 Receipts

- `model_family_registry.json`
- `catboost_artifact_manifest.json`
- `xgboost_artifact_manifest.json`
- `calibration_report.json`
- `feature_importance_report.json`

### Phase 7 Exit Criteria

- Same registered dataset trains LightGBM baseline, CatBoost GPU, and
  XGBoost GPU remotely.
- Trusted side verifies all three artifacts and metrics.

---

## Phase 8 — Cross-Sectional Ranking And Stacked Ensembles

Move from single-symbol direction prediction to portfolio-relevant ranking
and ensemble accuracy.

### T-8.1 Column Roles, Groups, Weights, Horizons

- status: `done`
- owner: Builder 1 (swarm 344a9957556ca5) — commit 7f1d1fd
- deps: [T-2.2, T-3.1]
- files:
  - `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
- summary: Add manifest roles: feature columns, label columns, timestamp,
  symbol, horizon, weight, group id, sector/industry, excluded audit columns.
  Trainer must not infer features by dropping a few names. Add `ModelTaskSpec`
  (task_type, label_column, horizon, weight_column, group_column,
  calibration_policy).
- validation:
  - Leakage column declared excluded is never used.
  - Ranking request without group id fails.
  - Missing label fails.
- done gate: Every trainer receives explicit features/labels/groups.
- maps to: Roadmap Phase 8.1; Improvement Track 10.

### T-8.2 LambdaRank / Cross-Sectional Ranker

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit a26f8b7
- deps: [T-8.1, T-7.1]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - metrics modules
- summary: Add ranker task type. Group by decision timestamp/universe.
  Metrics: rank IC, NDCG, top-k spread, turnover, cost-adjusted long-short
  return, drawdown.
- validation:
  - Ranker fails without groups.
  - Fold metrics include rank metrics.
  - Top-k spread recomputed from prediction artifact.
- done gate: Ranker produces portfolio-relevant receipts.
- maps to: Roadmap Phase 8.2; Model Tiers Tier 3.

### T-8.3 Out-Of-Fold Prediction Artifacts

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 149d87b
- deps: [T-8.1, T-7.2, T-7.3]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - artifact writer code
- summary: Every base model writes OOF predictions: row id, fold id, symbol,
  timestamp, label, prediction, horizon, weight. Store artifact URI/hash.
- validation:
  - OOF row count matches validation rows.
  - No training-fold predictions leak into meta-learner.
- done gate: Stacking can be trained without fold leakage.
- maps to: Roadmap Phase 8.3; Improvement Track 9; Improvement Track 13.

### T-8.4 Consume Manifest Folds Exactly

- status: `done`
- owner: Builder 4 (swarm 1ae87926866d1f) — commit 0c124766
- deps: [T-8.1, T-2.2]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - `services/quant_foundry/src/quant_foundry/dataset_manifest.py`
- summary: Add stable `row_id` or `(symbol, decision_time, horizon)` keys.
  Store fold assignment as manifest fold windows plus deterministic selection
  rules, or a compact fold assignment file referenced by hash. Teach
  `RealLightGBMTrainer` to consume fold assignments from the manifest. Fail
  closed if a production manifest has no fold spec. Keep trainer-generated
  folds only for canary mode.
- validation:
  - Worker fold row counts match manifest fold row counts.
  - A manifest with invalid overlap after purge/embargo is rejected.
  - A repeated RunPod job on the same manifest emits identical fold metrics.
- done gate: Trainer uses manifest folds as the contract of record.
- maps to: Improvement Track 9.

### T-8.5 Stacked Ensemble

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 8b91b6c
- deps: [T-8.3, T-7.4]
- files:
  - ensemble trainer modules
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
- summary: Train base models. Train simple meta-learner on OOF predictions.
  Store ensemble manifest listing base artifact hashes. Emit ensemble
  calibration and contribution report.
- validation:
  - Ensemble fails if base artifact missing.
  - Ensemble inference loads base artifacts in deterministic order.
  - Meta-learner only sees OOF predictions.
- done gate: Tree stack becomes the new champion/challenger baseline.
- maps to: Roadmap Phase 8.4; Model Tiers Tier 2.

### T-8.6 In-Worker Optuna Tuning

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 12b385c
- deps: [T-7.1]
- files:
  - `services/quant_foundry/src/quant_foundry/real_trainer.py`
  - `services/quant_foundry/src/quant_foundry/training_manifest.py`
- summary: Add `tuning_spec` to the training manifest (search algorithm, max
  trials, max wall-clock seconds, metric to optimize, direction, early
  stopping rounds, pruning policy, seed). Write `study.json`, `best_trial.json`,
  and trial metrics as artifacts. Add a heartbeat update after each trial.
  Add a hard budget timeout enforced inside the worker. Record failed trials
  with reason codes.
- validation:
  - A 3-trial canary produces a study artifact and best-trial artifact.
  - A deliberately bad trial is pruned and recorded.
  - The worker stops before the configured wall-clock budget.
  - The final callback includes the selected hyperparameters and study hash.
- done gate: GPU time is used for actual tuning with pruning and budget
  caps.
- maps to: Improvement Track 11.

### Phase 8 Receipts

- `column_roles_receipt.json`
- `rank_metrics.json`
- `oof_predictions.parquet`
- `ensemble_manifest.json`
- `ensemble_calibration_report.json`
- `tuning_study.json`

### Phase 8 Exit Criteria

- Tree stack and ranker both beat or clearly fail against the baseline with
  recomputable metrics.

---

## Phase 9 — Tabular Neural And Tabular Foundation Models

Add the first true neural GPU model tier while still using mostly tabular
feature-lake data.

### T-9.1 Build `trainer-gpu-tabular-neural` Image

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 2699e597
- deps: [T-4.2]
- files:
  - new Dockerfile/image config
  - worker image definitions
- summary: PyTorch CUDA base. Install TabM or selected tabular neural
  library. Optional TabPFN adapter in shadow mode. Add GPU memory telemetry.
- validation:
  - GPU healthcheck passes.
  - Tiny neural canary trains.
  - Artifact loads and scores.
- done gate: PyTorch CUDA worker is proven before model complexity grows.
- maps to: Roadmap Phase 9.1; Model Tiers Tier 4; Model Tiers "RunPod Image
  Strategy".

### T-9.2 Normalization And Missing-Value Artifacts

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 074dbfd
- deps: [T-9.1, T-8.1]
- files:
  - dataset manifest and normalizer artifact code
- summary: Add normalizer artifact. Store means/stds or robust scaler stats
  by fold where needed. Missing policy stored in manifest.
- validation:
  - Normalizer hash included in callback.
  - Inference fails if normalizer missing.
- done gate: Neural models are reproducible and loadable.
- maps to: Roadmap Phase 9.2; Model Tiers Tier 4 data contract.

### T-9.3 TabM Research Trainer

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit ba7207a
- deps: [T-9.1, T-9.2, T-7.1]
- files:
  - new PyTorch trainer modules
  - model family registry
- summary: Add `tabm` family. Same folds and prediction schema. Research mode
  by default. Promotion only if it improves ensemble OOF performance.
- validation:
  - Can train on small registered dataset remotely.
  - Produces prediction artifact and model artifact.
- done gate: TabM can become a base learner if it earns it.
- maps to: Roadmap Phase 9.3; Model Tiers Tier 4.

### T-9.4 TabPFN Shadow Adapter

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit e984c75
- deps: [T-9.1, T-7.1]
- files:
  - new PyTorch trainer modules
  - model family registry
- summary: Restrict to small/regime datasets. Explicit dataset-size guard.
  Shadow-only by default. Detect and prevent in-context label leakage.
- validation:
  - Oversized dataset rejected.
  - Shadow output cannot be promotion eligible without manual policy change.
- done gate: TabPFN benchmarks small-data/regime tasks safely.
- maps to: Roadmap Phase 9.4; Model Tiers Tier 5.

### Phase 9 Receipts

- `tabular_neural_runtime_receipt.json`
- `normalizer_artifact.json`
- `tabm_artifact_manifest.json`
- `tabpfn_shadow_receipt.json`

### Phase 9 Exit Criteria

- TabM or TabPFN produces a verified shadow/challenger result, compared to
  tree stack on the same folds.

---

## Phase 10 — Sequence Datasets And Sequence Models

Add model families that see temporal windows directly instead of row-wise
features only.

### T-10.1 SequenceDatasetManifest

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit e128efa
- deps: [T-3.1, T-8.1]
- files:
  - dataset builder modules
  - dataset manifest schemas
- summary: Add schema: dataset_id, symbols, channels, window_length, stride,
  horizons, window_start/end, label_timestamp, availability_cutoff,
  normalization_policy, fold_assignment_uri/hash, data_uri/hash.
- validation:
  - Window containing future data rejected.
  - Fold assignments match window ids.
- done gate: Sequence data has its own manifest, not ad hoc `.npz`.
- maps to: Roadmap Phase 10.1; Model Tiers Tier 6 data contract.

### T-10.2 Windowed Tensor Builder

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit f30a000
- deps: [T-10.1]
- files:
  - dataset builder modules
- summary: Build daily bar windows first. Support `.npz` or sharded
  parquet/window store. Include symbol, timestamp, horizon, and row/window
  id. Write checksum and manifest.
- validation:
  - Deterministic output for fixture.
  - No window label timestamp inside feature window.
- done gate: Registered L2 sequence dataset can be staged to RunPod.
- maps to: Roadmap Phase 10.2; Model Tiers Tier 6.

### T-10.3 PatchTST Or N-HiTS Canary

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 9f36369
- deps: [T-10.2, T-9.1]
- files:
  - PyTorch sequence trainer modules
  - worker image `trainer-gpu-sequence`
- summary: Add one sequence model first: PatchTST for transformer-style
  path, or N-HiTS for simpler forecasting baseline. Produce prediction
  artifact at window id grain. Shadow-only first.
- validation:
  - RunPod GPU canary trains on small sequence dataset.
  - Artifact loads and scores.
  - Metrics compare to tree stack on same symbol/horizon subset.
- done gate: First sequence model has a real RunPod receipt.
- maps to: Roadmap Phase 10.3; Model Tiers Tier 6.

### T-10.4 TFT After Covariate Roles Are Clear

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 376e105
- deps: [T-10.3, T-8.1]
- files:
  - PyTorch sequence trainer modules
- summary: Add TFT only after static, known-future, and observed covariates
  are declared. Multi-horizon forecast output.
- validation:
  - TFT request fails if covariate roles missing.
- done gate: TFT is not allowed to infer covariates loosely.
- maps to: Roadmap Phase 10.4; Model Tiers Tier 6.

### T-10.5 Build `trainer-gpu-sequence` Image

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 54ec16f
- deps: [T-9.1]
- files:
  - new Dockerfile/image config
- summary: PyTorch CUDA image with sequence tensor loaders, checkpoint
  resume, mixed precision, TensorBoard or metrics artifact.
- validation: Image builds and GPU healthcheck passes.
- done gate: Sequence image is separate from tabular neural image.
- maps to: Model Tiers "RunPod Image Strategy".

### Phase 10 Receipts

- `sequence_dataset_manifest.json`
- `window_builder_receipt.json`
- `sequence_model_artifact.json`
- `sequence_predictions.parquet`

### Phase 10 Exit Criteria

- Sequence model beats or loses to tree stack on a matched subset with
  cost-aware metrics.

---

## Phase 11 — Time-Series Foundation Shadow Bench

Run TimesFM, Chronos, Moirai, and similar models as forecast-distribution
benchmarks without granting promotion authority too early.

### T-11.1 Foundation Weight Policy

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 2c5e8ca
- deps: [T-9.1]
- files:
  - foundation adapter modules
  - worker image `trainer-gpu-foundation-ts`
- summary: Pin model weights by id and hash. Bake or cache weights through
  approved mechanism. Record weight hash in runtime fingerprint. No surprise
  network downloads in production runs.
- validation:
  - Missing weight hash marks result invalid.
  - Runtime fingerprint includes weight artifact.
- done gate: Foundation runs are reproducible enough to audit.
- maps to: Roadmap Phase 11.1; Model Tiers Tier 7.

### T-11.2 Forecast Distribution Contract

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 89931c5
- deps: [T-11.1]
- files:
  - forecast distribution artifact schema
  - settlement/evaluation code
- summary: Output: mean/median, quantiles, samples if available, uncertainty
  band, horizon, target transform. Convert distribution to alpha signal only
  through an adapter.
- validation:
  - Forecast artifact validates.
  - Alpha adapter records its policy.
- done gate: Foundation outputs are evaluated as distributions, not vague
  scores.
- maps to: Roadmap Phase 11.2; Model Tiers Tier 7.

### T-11.3 Shadow Tournament

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 875067d
- deps: [T-11.2, T-8.5, T-10.3]
- files:
  - foundation adapter modules
  - shadow scoring code
- summary: Run TimesFM, Chronos, Moirai on same registered series dataset.
  Compare to tree stack and sequence model. Settle predictions. Keep
  `promotion_eligible=false` until explicit policy changes.
- validation:
  - Shadow output cannot publish live signal.
  - Metrics recompute from forecast artifact.
- done gate: Foundation models have honest, settled benchmark results.
- maps to: Roadmap Phase 11.3; Model Tiers Tier 7.

### T-11.4 Build `trainer-gpu-foundation-ts` Image

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit c78af8c
- deps: [T-9.1]
- files:
  - new Dockerfile/image config
- summary: GPU image with pinned foundation weights and batch forecast
  adapters. Offline inference mode.
- validation: Image builds and weight hash is recorded.
- done gate: Foundation image is separate from sequence image.
- maps to: Model Tiers "RunPod Image Strategy".

### Phase 11 Receipts

- `foundation_weight_receipt.json`
- `forecast_distribution.parquet`
- `foundation_shadow_scorecard.json`

### Phase 11 Exit Criteria

- At least one foundation model run is complete, verified, and benchmarked
  against the tree stack.

---

## Phase 12 — Event/News Fusion And Graph Alpha

Unlock differentiated alpha from event context and symbol relationships
after the core trainer contract is reliable.

### T-12.1 EventDatasetManifest

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 53c6d2f
- deps: [T-3.1, T-3.4]
- files:
  - event data ingestion modules
  - feature lake modules
- summary: Add schema: event_id, source_id, published_at, available_at,
  affected_symbols, event_type, raw_text_hash, embedding_model_hash,
  label_horizons, data_uri/hash.
- validation:
  - Event with `available_at > decision_time` rejected.
  - Revised metadata cannot leak into training.
- done gate: Event data can be joined point-in-time.
- maps to: Roadmap Phase 12.1; Model Tiers Tier 8 data contract.

### T-12.2 Event Abnormal-Return Model

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 9587a52
- deps: [T-12.1, T-9.1]
- files:
  - text encoder worker image `trainer-gpu-event-text`
  - event trainer modules
- summary: Encode event text or structured event tags. Predict abnormal
  return over 1d/5d/20d. Output shadow predictions by event type.
- validation:
  - Missing event source hash fails.
  - Predictions settle by event type and confidence bucket.
- done gate: Event model proves value in event regimes before joining live
  stack.
- maps to: Roadmap Phase 12.2; Model Tiers Tier 8.

### T-12.3 GraphDatasetManifest

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit e1cc5d8
- deps: [T-3.1, T-3.4]
- files:
  - graph dataset modules
- summary: Add schema: node_ids, edge_ids, edge_type, edge_observed_at,
  edge_available_at, graph_snapshot_time, node_feature_schema, label_horizon,
  data_uri/hash.
- validation:
  - Future edge rejected.
  - Missing node id mapping rejected.
- done gate: Graph snapshots are PIT-safe.
- maps to: Roadmap Phase 12.3; Model Tiers Tier 9 data contract.

### T-12.4 Graph Ranker

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 71e132a
- deps: [T-12.3, T-8.2]
- files:
  - graph trainer modules
  - worker image `trainer-gpu-graph`
- summary: Start simple: nodes are symbols, edges are sector/industry and
  rolling correlation, target is relative return rank. Output ranked list
  and edge attribution.
- validation:
  - Graph rank metrics recompute from prediction artifacts.
  - Edge availability respected.
- done gate: Graph model competes with cross-sectional ranker.
- maps to: Roadmap Phase 12.4; Model Tiers Tier 9.

### T-12.5 Build `trainer-gpu-event-text` Image

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit c268567
- deps: [T-9.1]
- files:
  - new Dockerfile/image config
- summary: GPU text encoder image with offline model weights, event dataset
  builder, embedding cache, event-to-symbol resolver.
- validation: Image builds and embedding canary passes.
- done gate: Event text image is separate from graph image.
- maps to: Model Tiers "RunPod Image Strategy".

### T-12.6 Build `trainer-gpu-graph` Image

- status: `done`
- owner: Builder 4 (swarm 1ae87926866d1f) — commit ff87618
- deps: [T-9.1]
- files:
  - new Dockerfile/image config
- summary: PyTorch Geometric or DGL-compatible image. Graph snapshot loader.
  GPU memory planning for nodes/edges/time steps. Graph artifact format.
- validation: Image builds and graph canary passes.
- done gate: Graph image is separate from event text image.
- maps to: Model Tiers "RunPod Image Strategy".

### Phase 12 Receipts

- `event_dataset_manifest.json`
- `event_model_scorecard.json`
- `graph_dataset_manifest.json`
- `graph_ranker_artifact.json`

### Phase 12 Exit Criteria

- Event and graph models remain shadow-only until they improve router or
  ranker outcomes by settled evidence.

---

## Phase 13 — Policy Layers, Mixture-Of-Experts, RL, And LLM Research

Use the mature model zoo to improve decisions, routing, and feature
generation without giving experimental agents live authority.

### T-13.1 Mixture-Of-Experts Router

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 3e57306
- deps: [T-8.5, T-9.3, T-10.3]
- files:
  - router modules
- summary: Inputs: expert predictions, uncertainty, regime features, model
  disagreement. Output: expert weights, abstention flag. Start with
  linear/logistic router.
- validation:
  - Router cannot use in-fold expert predictions.
  - Router calibration report exists.
- done gate: Router improves settled outcomes by regime without inflating
  confidence.
- maps to: Roadmap Phase 13.1; Model Tiers Tier 12.

### T-13.2 Portfolio Policy Model

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 4ae6ac3
- deps: [T-13.1]
- files:
  - policy modules
- summary: Inputs are verified model outputs. Outputs are target weights or
  abstention. Reward includes cost, drawdown, turnover, and risk constraints.
  Shadow replay only.
- validation:
  - Policy cannot violate hard risk limits in replay.
  - Cost model hash recorded.
- done gate: Policy improves replay outcomes without risk-limit violations.
- maps to: Roadmap Phase 13.2; Model Tiers Tier 11.

### T-13.3 RL Shadow Policy

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 7c409f0
- deps: [T-13.2]
- files:
  - RL modules
  - worker image `trainer-gpu-rl`
- summary: Only after simulator and cost model are receipt-backed.
  Offline/shadow only. Store environment manifest, rollout logs, and policy
  checkpoint.
- validation:
  - Train/eval periods strictly separated.
  - Reward cannot access future labels.
- done gate: RL remains a research policy lane, not next alpha model.
- maps to: Roadmap Phase 13.3; Model Tiers Tier 11.

### T-13.4 LLM Feature Agent

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit e4420fe
- deps: [T-12.2]
- files:
  - LLM feature modules
- summary: Use LLMs for event extraction, tagging, and explanations. Never
  direct trade signals. Every feature has prompt hash, model id, source hash,
  and availability time.
- validation:
  - LLM output schema validation.
  - Missing source hash rejected.
- done gate: LLM-generated features are treated as untrusted, validated
  inputs.
- maps to: Roadmap Phase 13.4; Model Tiers Tier 13.

### T-13.5 Build `trainer-gpu-rl` Image

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit 399667c
- deps: [T-9.1]
- files:
  - new Dockerfile/image config
- summary: GPU image with RL framework, deterministic market simulator,
  environment version hash, cost model hash, policy checkpoint artifact,
  rollout artifact and replay logs.
- validation: Image builds and simulator canary passes.
- done gate: RL image is separate from policy router.
- maps to: Model Tiers "RunPod Image Strategy".

### Phase 13 Receipts

- `router_scorecard.json`
- `policy_replay_receipt.json`
- `rl_environment_manifest.json`
- `llm_feature_manifest.json`

### Phase 13 Exit Criteria

- Decision layers are proven above the model zoo and remain trusted-side or
  shadow-only until policy gates pass.

---

## Cross-Phase Workstream A — UI And Operator Experience

Give operators a usable view of the training system without letting UI imply
health that receipts do not prove.

### T-UI.1 Training Job Ledger View

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit 56f2437
- deps: [T-6.1]
- files:
  - API/dashboard surfaces if present
- summary: Show job id, dataset id, model family, RunPod job id, status, GPU
  type, cost estimate, artifact verification, promotion eligibility, failure
  reason.
- done gate: Operator can distinguish queued, running, failed, rejected,
  verified, and promotion-ineligible states.
- maps to: Roadmap Cross-Phase UI.1.

### T-UI.2 Dataset Registry View

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 6988ee5
- deps: [T-3.1]
- files:
  - API/dashboard surfaces if present
- summary: Show dataset id, readiness level, manifest hash, quality gate
  status, upload/staging status, eligible modes.
- done gate: Operator can see why a dataset cannot be production-trained.
- maps to: Roadmap Cross-Phase UI.2.

### T-UI.3 Model Tournament View

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit b2922fb
- deps: [T-8.5, T-7.4]
- files:
  - API/dashboard surfaces if present
- summary: Show baseline versus challenger metrics, calibration,
  cost-adjusted return, drawdown, rank metrics where relevant, trial count,
  deflated score, shadow/live eligibility.
- done gate: Operator can compare model families without reading raw JSON.
- maps to: Roadmap Cross-Phase UI.3.

---

## Cross-Phase Workstream B — RunPod-Only Operator Workflow

Replace scattered entry points with one RunPod-only command surface.

### T-OP.1 Unified RunPod CLI Surface

- status: `done`
- owner: Builder 4 (swarm 1ae87926866d1f) — commit b191bc2
- deps: [T-3.1, T-6.1]
- files:
  - CLI/scripts that dispatch RunPod jobs
- summary: Replace scattered entry points with one command surface:
  `fincept runpod dataset register`, `fincept runpod dataset upload`,
  `fincept runpod train canary`, `fincept runpod train production`,
  `fincept runpod train status`, `fincept runpod train verify`,
  `fincept runpod train cost`. No command trains locally. Local execution
  limited to manifest validation, request construction, and receipt
  verification. Mark old scripts as deprecated wrappers.
- validation:
  - Running a production command with a raw CSV fails before dispatch.
  - Running a canary command with a small registered dataset succeeds
    remotely.
  - Status command shows dispatch, queue, worker, callback, artifact
    verification, and final eligibility states.
- done gate: Operators get one safe path with unavoidable preflight and
  verification.
- maps to: Improvement Track 17.

---

## Cross-Phase Workstream C — Test And Verification Matrix

Use a tiered verification matrix so local tests stay fast while proving the
remote system.

### T-TV.1 Unit Test Suite

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit e435092
- deps: —
- files:
  - `services/quant_foundry/tests/`
- summary: Schema validation, manifest hashing, request creation, callback
  verification. Runs in local CI.
- validation: `uv run pytest services/quant_foundry/tests -p no:cacheprovider
  --basetemp=reports/pytest-basetemp/quant-foundry`
- done gate: Unit tests cover all schema and contract code.
- maps to: Improvement Track 18.

### T-TV.2 RunPod Hosted Canary Suite

- status: `unstarted`
- owner: —
- deps: [T-4.2, T-1.3, T-2.2, T-3.3]
- files:
  - `runpod/quant-foundry-training/tests/` if present or added
- summary: Dispatch tiny registered dataset. Canary budget caps and
  automatic cleanup. Required canaries: callback secret canary, GPU
  healthcheck canary, manifest-load canary, small training canary, artifact
  verification canary, negative corrupted-artifact canary, bad dataset
  quality-gate canary.
- validation: All canaries pass on RunPod GPU.
- done gate: Hosted behavior is proven from RunPod receipts, not local tests
  only.
- maps to: Improvement Track 18; Roadmap "RunPod Hosted Validation".

### T-TV.3 Receipt Bundle Storage

- status: `done`
- owner: Builder 2 (swarm 1ae87926866d1f) — commit 0aabbcf
- deps: [T-6.1]
- files:
  - receipt storage code
- summary: Store receipt bundles under `reports/runpod-training/<job-id>/`.
  Use stable test datasets small enough for repeatable GPU canaries. Add
  `verify_runpod_training_receipt` command.
- done gate: Every training job has a fetchable receipt bundle.
- maps to: Improvement Track 18.

---

## Cross-Phase Workstream D — LOB / Microstructure Lane (Separate)

Only start this lane if Fincept has or will obtain real LOB/tick data.

### T-LOB.1 LOBDatasetManifest

- status: `done`
- owner: Builder 3 (swarm 1ae87926866d1f) — commit c6b3c1f
- deps: [T-3.1]
- files:
  - dataset manifest schemas
- summary: Add schema: venue, symbol, book depth, event time, receive time,
  sequence id, adjustment policy, label horizon in events or milliseconds,
  train/validation session split. Strong dedupe and ordering checks.
- done gate: LOB data has its own manifest with session splits.
- maps to: Model Tiers Tier 10.

### T-LOB.2 DeepLOB-Style Canary

- status: `done`
- owner: Builder 1 (swarm 1ae87926866d1f) — commit a4276b7
- deps: [T-LOB.1, T-9.1]
- files:
  - LOB trainer modules
- summary: Use one liquid symbol, one venue, one short horizon, one
  DeepLOB-style model. Shadow-only. Evaluate after spread, fees, and latency
  assumptions.
- done gate: First LOB model has a shadow receipt.
- maps to: Model Tiers Tier 10.

---

## Dependency Graph

```text
T-0.1 modes
  -> T-1.1 artifact result
  -> T-2.1 manifest/data URI split
  -> T-4.1 GPU healthcheck

T-1.1 artifact result
  -> T-1.2 artifact writer
  -> T-1.4 callback schema
  -> T-7.1 model family registry

T-1.2 artifact writer
  -> T-1.3 artifact verifier

T-1.3 artifact verifier
  -> T-6.1 job ledger

T-2.1 manifest/data URI split
  -> T-2.2 manifest dataset loader
  -> T-2.3 URI scheme restrictions
  -> T-3.1 dataset registry

T-3.1 dataset registry
  -> T-3.2 quality policy
  -> T-3.4 PIT metadata
  -> T-8.1 column roles
  -> T-10.1 sequence manifest
  -> T-12.1 event manifest
  -> T-12.3 graph manifest
  -> T-LOB.1 LOB manifest

T-3.2 quality policy
  -> T-3.3 worker quality gate

T-4.1 GPU healthcheck
  -> T-4.2 trainer-gpu-tree image
  -> T-4.3 worker split
  -> T-5.2 runtime fingerprint

T-4.3 worker split
  -> T-5.1 security preflight

T-4.2 trainer-gpu-tree
  -> T-7.1 model family registry
  -> T-9.1 trainer-gpu-tabular-neural

T-9.1 tabular neural image
  -> T-9.2 normalizer artifacts
  -> T-10.5 sequence image
  -> T-11.1 foundation weight policy
  -> T-12.5 event text image
  -> T-12.6 graph image
  -> T-13.5 RL image

T-7.1 model family registry
  -> T-7.2 CatBoost GPU
  -> T-7.3 XGBoost GPU
  -> T-8.2 LambdaRank
  -> T-9.3 TabM
  -> T-9.4 TabPFN

T-8.1 column roles
  -> T-8.2 LambdaRank
  -> T-8.3 OOF predictions
  -> T-8.4 manifest folds
  -> T-9.2 normalizer artifacts

T-8.3 OOF predictions
  -> T-8.5 stacked ensemble

T-7.4 calibration
  -> T-8.5 stacked ensemble

T-8.5 stacked ensemble
  -> T-11.3 shadow tournament
  -> T-13.1 mixture router

T-10.3 sequence canary
  -> T-11.3 shadow tournament
  -> T-13.1 mixture router

T-13.1 mixture router
  -> T-13.2 portfolio policy

T-13.2 portfolio policy
  -> T-13.3 RL shadow policy

T-12.2 event model
  -> T-13.4 LLM feature agent
```

---

## Suggested PR Sequence

Each PR maps to one or more tasks. An agent may claim a PR-sized slice
rather than a single task if dependencies allow.

| PR  | Tasks                         | Theme                                    |
| --- | ----------------------------- | ---------------------------------------- |
| 1   | T-1.1, T-1.2, T-1.3, T-1.4    | Artifact contract repair                 |
| 2   | T-2.1, T-2.2, T-2.3           | Manifest-first dataset loader            |
| 3   | T-3.1, T-3.2, T-3.3, T-3.4    | Dataset registry and quality policies    |
| 4   | T-4.1, T-4.2                  | GPU tree worker image and healthcheck    |
| 5   | T-4.3, T-5.1, T-5.2, T-5.3    | Worker split and security preflight      |
| 6   | T-6.1, T-6.2, T-6.3           | Job ledger and callback DLQ              |
| 7   | T-7.1, T-7.2, T-7.3, T-7.4    | CatBoost/XGBoost GPU trainers            |
| 8   | T-8.1, T-8.2, T-8.4           | Column roles, ranker, manifest folds     |
| 9   | T-8.3, T-8.5, T-8.6           | OOF predictions, stacked ensemble, Optuna|
| 10  | T-9.1, T-9.2, T-9.3, T-9.4    | Tabular neural worker and TabM           |
| 11  | T-10.1..T-10.5                | Sequence dataset and first sequence model|
| 12  | T-11.1..T-11.4                | Foundation forecast shadow bench         |
| 13  | T-12.1..T-12.6                | Event and graph datasets                 |
| 14  | T-13.1..T-13.5                | Router and policy shadow layer           |

---

## Acceptance Gates By Maturity

### Canary Eligible

- registered dataset or fixture dataset
- signed callback
- artifact may use volume URI
- no promotion eligibility
- small budget

### Research Eligible

- registered L2+ dataset
- manifest-first loader
- quality report present
- artifact verified
- runtime fingerprint present
- shadow output only

### Production Training Eligible

- registered L3/L4 dataset
- GPU required and proven
- no CPU fallback
- production quality policy passed
- artifact verified
- prediction artifacts present
- metrics recomputable
- callback HMAC valid
- ledger complete

### Promotion Eligible

- trusted-side artifact verification complete
- prediction metrics recomputed
- calibration acceptable
- cost-adjusted performance beats baseline
- risk/tournament gates pass
- no unresolved DLQ or waiver issues
- human or trusted promotion path approves

---

## Model Tier Acceptance Gates (every non-LightGBM tier)

1. It runs on RunPod, not as local training.
2. It uses a registered dataset manifest.
3. It emits verified artifact URI/hash/size.
4. It emits prediction artifacts at row/window/event grain.
5. It uses purged folds from the manifest.
6. It records trial count and search budget.
7. It has calibration or uncertainty reporting.
8. It is compared against LightGBM/CatBoost/XGBoost stack.
9. It is scored after costs, not only by AUC or loss.
10. It stays shadow-only until promotion gates pass.
11. It records GPU runtime metadata.
12. It cannot publish live signals from the RunPod worker.

---

## Final Target State

```text
raw sources
  -> data modules with PIT availability
  -> feature/event/sequence/graph manifests
  -> dataset registry and readiness gates
  -> training manifest
  -> RunPod GPU worker
  -> verified model artifacts
  -> prediction/evaluation artifacts
  -> trusted-side metric recompute
  -> job ledger and cost report
  -> tournament/promotion gates
  -> shadow/live authority decision
```

The model zoo should look like this:

```text
baseline:
  lightgbm_baseline
  logreg_sanity
  linear_sanity

near-term challengers:
  catboost_gpu
  xgboost_gpu
  tree_stack
  lambdarank_tree

research challengers:
  tabm
  tabpfn_shadow
  patchtst
  nhits
  tft

frontier shadow:
  timesfm_shadow
  chronos_shadow
  moirai_shadow
  event_text_encoder
  graph_ranker

policy:
  mixture_router
  policy_replay
  rl_policy_shadow
  llm_feature_agent
```

---

## Task Status Summary

| Phase | Theme                                  | Tasks                  | unstarted | in_progress | complete | reviewed |
| ----- | -------------------------------------- | ---------------------- | --------- | ----------- | -------- | -------- |
| 0     | Baseline and acceptance targets       | T-0.1, T-0.2           | 1         | 0           | 0        | 1        |
| 1     | Artifact and callback contract        | T-1.1..T-1.4           | 0         | 0           | 0        | 4        |
| 2     | Manifest-first dataset loading        | T-2.1..T-2.3           | 1         | 0           | 0        | 2        |
| 3     | Dataset registry and quality gates    | T-3.1..T-3.4           | 0         | 0           | 0        | 4        |
| 4     | GPU worker image and worker split     | T-4.1..T-4.3           | 0         | 0           | 0        | 3        |
| 5     | Security hardening and fingerprint    | T-5.1..T-5.3           | 0         | 0           | 0        | 3        |
| 6     | Job ledger, DLQ, cost telemetry       | T-6.1..T-6.3           | 0         | 0           | 0        | 3        |
| 7     | Production tree challengers           | T-7.1..T-7.4           | 3         | 0           | 0        | 1        |
| 8     | Ranking and stacked ensembles         | T-8.1..T-8.6           | 6         | 0           | 0        | 0        |
| 9     | Tabular neural and foundation models  | T-9.1..T-9.4           | 4         | 0           | 0        | 0        |
| 10    | Sequence datasets and models          | T-10.1..T-10.5         | 5         | 0           | 0        | 0        |
| 11    | Time-series foundation shadow bench   | T-11.1..T-11.4         | 4         | 0           | 0        | 0        |
| 12    | Event/news and graph alpha            | T-12.1..T-12.6         | 6         | 0           | 0        | 0        |
| 13    | Policy, mixture, RL, LLM research     | T-13.1..T-13.5         | 5         | 0           | 0        | 0        |
| UI    | UI and operator experience            | T-UI.1..T-UI.3         | 3         | 0           | 0        | 0        |
| OP    | RunPod-only operator workflow         | T-OP.1                 | 1         | 0           | 0        | 0        |
| TV    | Test and verification matrix          | T-TV.1..T-TV.3         | 2         | 0           | 0        | 1        |
| LOB   | LOB / microstructure lane             | T-LOB.1, T-LOB.2       | 2         | 0           | 0        | 0        |
| TOTAL |                                        | 66                     | 43        | 0           | 0        | 23       |
