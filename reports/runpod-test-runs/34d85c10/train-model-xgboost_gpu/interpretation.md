# A7 live train_model (xgboost_gpu) — interpretation

- **Date:** 2026-07-07T00:13Z
- **Branch:** `tier1a/product-loop`
- **Image SHA:** `34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **Image:** `ghcr.io/airyder/fincept/quant-foundry-training:34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **GPU:** ADA_24 (RTX 4090)
- **Model family:** `xgboost_gpu` (Tier 1.3 — non-deterministic GPU backend)
- **Endpoint:** `bfxupzfcpz6l3d` (template `6qhftrsfww`)
- **Job:** `f84654b1-2887-46dd-b49b-c394c0a4e903-u1`
- **Verdict:** **PASS** — XGBoost GPU trainer backend proven live on CUDA.

## What this run proves

This is the first live training run with `model_family=xgboost_gpu`, proving
the Tier 1.3 GPU trainer backend end-to-end on the production RTX 4090:

1. **Handler routing** — `_real_backend_for_family("xgboost_gpu")` correctly
   routed to the `xgboost` backend, which delegates to
   `RealLightGBMTrainer._train_xgboost`.
2. **CUDA device selection** — `_build_xgboost_params` set `device="cuda"`
   (triggered by `req.model_family == "xgboost_gpu"`). The XGBoost GPU
   trainer ran on the RTX 4090 (no CPU-fallback warning in the live output,
   unlike the local smoke which showed "No visible GPU is found").
3. **Explicit column_roles + task_spec** — the handler required and
   successfully consumed `extra_constraints.column_roles` (JSON: f1/f2/f3
   features, label target, timestamp column) and `extra_constraints.task_spec`
   (JSON: binary classification, label_column=label). This is the first live
   proof that the non-lightgbm explicit-metadata path works end-to-end.
4. **Model export** — XGBoost UBJ format (55,212 bytes) with sha256
   re-verification (`be751642...`) and HMAC write receipt (`2a459b0c...`).
5. **Determinism status** — `model_family=xgboost_gpu` is flagged
   `non_deterministic` in the artifact manifest (GPU floating-point
   summation order differs from CPU). `loader_family=xgboost`.
6. **Metric sanity** — correctly non-promotable (`promotion_allowed=false`,
   `sharpe_ratio_implausible`). The synthetic canary is not a promotion
   candidate.
7. **Canary strict=False** — the trainer used `strict=False` (canary mode),
   which would have fallen back to CPU if no GPU was available. On the live
   RTX 4090, CUDA was available so training ran on GPU as intended.

## Evidence cross-check (receipt-integrity)

| Claim                      | Raw evidence file              | Field                                      |
| -------------------------- | ------------------------------ | ------------------------------------------ |
| Job COMPLETED              | `probe.jsonl` line 2           | `status=COMPLETED`, `completed=1`          |
| Worker healthy throughout  | `probe.jsonl` lines 1-2        | `unhealthy=0` on every poll                |
| Model exported (UBJ)       | `train-model-result.json`      | `artifact_format=xgboost-ubj`, `artifact_sha256` |
| Write receipt present      | `train-model-result.json`      | `write_receipt` (non-empty)                |
| model_family=xgboost_gpu   | `train-model-result.json`      | `model_family=xgboost_gpu`                 |
| loader_family=xgboost      | `train-model-result.json`      | `loader_family=xgboost`                    |
| Callback signed            | `train-model-result.json`      | `callback_signature_present=true`          |
| Preflight passed           | `train-model-result.json`      | `preflight_passed=true`                    |
| Endpoint + template cleaned| `cleanup.json`                 | `deleted=true`, `template_deleted=true`    |

No raw evidence contradicts the PASS verdict.

## Timeline

- `00:10Z` — template + endpoint created.
- `00:13:05Z` — worker `ready=1, idle=1, unhealthy=0` (cold-pull ~165s;
  slower than the lightgbm run's 65s, likely a fresh host without cached
  layers).
- `00:13:20Z` — job dispatched, `IN_QUEUE`.
- `00:13:25Z` — job `COMPLETED` (~5s end-to-end).
- `00:13Z` — endpoint scaled to 0, deleted; template deleted.

## Comparison: lightgbm vs xgboost_gpu on the same image

| Metric              | lightgbm (canary) | xgboost_gpu (canary) |
| ------------------- | ----------------- | -------------------- |
| Artifact format     | pickle            | xgboost-ubj          |
| Artifact size       | 337,368 bytes     | 55,212 bytes         |
| Accuracy            | 0.835             | 0.953                |
| Logloss             | 0.386             | 0.155                |
| Determinism         | deterministic     | non_deterministic    |
| Job completion      | ~10s              | ~5s                  |

The xgboost_gpu canary achieved higher accuracy on the synthetic dataset
(expected — XGBoost's boosted trees fit the noisy-linear signal better than
LightGBM's leaf-wise growth at these defaults). This is not a meaningful
generalization comparison (300-row synthetic canary), but it confirms both
backends produce valid, different models.

## Risks / notes

- The artifact URI is `file:///tmp/...` (canary `output_prefix`). Production
  jobs must use a durable volume/presigned URL per Tier 0.2.
- `training_manifest_hash` is `null` — expected for an inline-dataset canary.
- This run does **not** exercise the PIT proof gate's production fail-closed
  path (requires a real dataset manifest with `pit_proof_verified=true`).
- The `strict=False` canary mode means a no-GPU environment would silently
  fall back to CPU. A production `xgboost_gpu` run should use
  `training_mode=production` to enforce `strict=True` (fail-closed if no GPU).

## Next recommended task

- A production-mode run with a real dataset manifest (`pit_proof_verified=true`)
  to exercise the PIT proof gate fail-closed path (Tier 1.5), using
  `model_family=xgboost_gpu` with `training_mode=production` to also prove
  the GPU strict-mode fail-closed behavior.
