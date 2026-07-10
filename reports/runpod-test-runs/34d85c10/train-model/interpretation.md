# A7 live train_model — interpretation

- **Date:** 2026-07-06T23:50Z
- **Branch:** `tier1a/product-loop`
- **Image SHA:** `34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **Image:** `ghcr.io/airyder/fincept/quant-foundry-training:34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e`
- **GPU:** ADA_24 (RTX 4090)
- **Endpoint:** `brbjxg4umv147n` (template `hai0ae66yp`)
- **Job:** `a6b9515d-ebdf-4859-bbb9-4062007d2477-u2`
- **Verdict:** **PASS** — full training pipeline proven live on the current HEAD image.

## What this run proves

This is the first live training run against an image that contains the Tier 1
work landed since the proven `6dbec436` baseline: `xgboost_gpu` backend
(`6e3f9226`), `dataset_manifests` table + point-in-time proof
(`1d0f50d7`), Optuna hyperparameter search wiring (`943bdee7`), and the PIT
proof gate in the handler (`93b6a42f`). The canary path exercised the
deterministic LightGBM CPU baseline (`model_family=lightgbm`,
`training_mode=canary`) end-to-end:

1. **Dataset load** — inline 300-row synthetic CSV -> temp CSV ->
   `RealLightGBMTrainer._load_csv`.
2. **Trainer.fit** — 2 walk-forward folds (`make_folds`, horizon=15/purge=15)
   + final model fit. `metrics_summary` present (accuracy 0.835, logloss
   0.386) => validation ran on real data.
3. **Model export** — pickle (337,368 bytes) written to
   `file:///tmp/a7-train-artifacts/model.pkl` with sha256 re-verification
   (`ac0b69ba...`) and an HMAC write receipt (`01963310...`).
4. **Signed callback** — `callback_signature_present=true`; preflight passed.
5. **Metric sanity** — correctly flags the synthetic canary as non-promotable
   (`promotion_allowed=false`, `sharpe_ratio_implausible`,
   `max_drawdown_implausible`). This is the expected canary behavior: the
   PIT proof gate and promotion gate are not bypassed.

## Evidence cross-check (receipt-integrity)

| Claim                      | Raw evidence file              | Field                                      |
| -------------------------- | ------------------------------ | ------------------------------------------ |
| Job COMPLETED              | `probe.jsonl` line 3           | `status=COMPLETED`, `completed=1`          |
| Worker healthy throughout  | `probe.jsonl` lines 1-3        | `unhealthy=0` on every poll                |
| Model exported + sha256    | `train-model-result.json`      | `artifact_sha256`, `artifact_size_bytes`   |
| Write receipt present      | `train-model-result.json`      | `write_receipt` (non-empty)                |
| Callback signed            | `train-model-result.json`      | `callback_signature_present=true`          |
| Preflight passed           | `train-model-result.json`      | `preflight_passed=true`                    |
| Endpoint + template cleaned| `cleanup.json`                 | `deleted=true`, `template_deleted=true`    |

No raw evidence contradicts the PASS verdict.

## Timeline

- `23:49Z` — template + endpoint created.
- `23:50:12Z` — worker `ready=1, idle=1, unhealthy=0` (cold-pull completed in
  ~65s; the `34d85c10` image shares layers with the cached `6dbec436` build).
- `23:50:47Z` — job dispatched, `IN_QUEUE`.
- `23:50:57Z` — job `COMPLETED` (~10s end-to-end including queue + execution).
- `23:51Z` — endpoint scaled to 0, deleted; template deleted.

## Fixes applied to unblock this run

Two regressions on the `tier1a/product-loop` branch blocked the live probe
tool chain and were fixed before this dispatch (the image itself was
unaffected — the Dockerfile copies only `handler.py` + `quant_foundry/` +
`fincept_core/`, not the probe tools):

1. **`build_job_policy` deleted by ruff burn-down pass `f0c7c4a9`.** The
   function body was removed from `scripts/runpod/runpod_lifecycle.py` but
   left in `__all__`, so every `from runpod.runpod_lifecycle import ...` raised
   `ImportError`. Restored from `5700e51c`. The matching test file
   `runpod/tests/test_runpod_lifecycle.py` had its `from ... import (` line
   stripped by the same pass — restored.
2. **RunPod GraphQL schema change: `executionTimeout` -> `executionTimeoutMs`.**
   `build_endpoint_input` now emits `executionTimeoutMs` (milliseconds). The
   per-request `policy.executionTimeout` from `build_job_policy` (already in
   ms) is unchanged and remains the documented reliable override.

## Risks / notes

- The artifact URI is `file:///tmp/...` — this is the canary
  `output_prefix=/tmp/a7-train-artifacts`. A real production job must use a
  durable `output_prefix` (RunPod network volume or presigned S3/R2 URL) per
  Tier 0.2; `/tmp` is denied for production by the handler. The canary
  intentionally uses `/tmp` to prove the pipeline without a volume mount.
- `training_manifest_hash` is `null` — expected for an inline-dataset canary
  (no FoldSpec / production manifest). `dataset_manifest_hash` is populated.
- This run does **not** exercise `xgboost_gpu`, Optuna, or the PIT proof gate's
  production fail-closed path (those require a real dataset manifest with
  `pit_proof_verified=true`). It proves the baseline pipeline still works on
  the new image. A follow-up canary with `model_family=xgboost_gpu` and a real
  manifest is the recommended next probe.

## Next recommended task

- Canary with `model_family=xgboost_gpu` against the same `34d85c10` image to
  prove the GPU trainer backend live (Tier 1.3), then a production-mode run
  with a real dataset manifest to exercise the PIT proof gate fail-closed
  path (Tier 1.5).
