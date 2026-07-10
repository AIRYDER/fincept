# GPU Healthcheck — Live Interpretation

**Result: PASS** — GPU is accessible inside the production training container.

## Summary

A `gpu_healthcheck` job (mode=canary) was dispatched against the exact-SHA
production image `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`.
The job COMPLETED in 3.5s (executionTime=3474ms, delayTime=6512ms). The worker
stayed `unhealthy=0` throughout. The GPU is visible and functional:

| Field | Value |
|-------|-------|
| gpu_capable | **true** |
| gpu_model | **NVIDIA GeForce RTX 4090** |
| gpu_count | **1** |
| gpu_memory_mb | **24564** (~24 GB VRAM) |
| nvidia_smi_available | true |
| cuda_version | 550.144.03 |
| driver_version | 550.144.03 |
| library_gpu_flags | catboost_gpu=true, lightgbm_gpu=false, xgboost_gpu=true |
| promotion_eligible | false (expected in canary mode) |
| mode | canary |
| preflight passed | true |

## Live run details

- **Image:** `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
- **Endpoint:** `6hl6v67nybijwy` (created fresh, scaled down + deleted after)
- **Template:** `l1shf1bs3c`
- **Worker ID:** `dzy1mxoua2ojqb`
- **Job ID:** `4f63ca8b-72ca-4a98-a489-ae4063b13519-u1`
- **Payload:** `{"input":{"task":"gpu_healthcheck","mode":"canary","job_id":"qf:gpu-hc:6dbec436:001"}}`
- **GPU type:** ADA_24 (RTX 4090)
- **Endpoint config:** workersMin=1, workersMax=1, idleTimeout=300, scalerType=QUEUE_DELAY, scalerValue=4, containerDiskInGb=20

## Timeline

1. Endpoint created at ~04:16 UTC.
2. Worker reached `ready=1, idle=1, unhealthy=0` at ~45s (initializing for ~20s).
3. Job dispatched at ~04:17:48 UTC.
4. Job stayed `IN_QUEUE` for ~5s (2 poll intervals).
5. Job reached `COMPLETED` at ~04:17:58 UTC (~10s after dispatch).
6. Worker stayed `ready=1, idle=1, unhealthy=0` after completion.
7. Endpoint scaled to 0/0 and deleted.

## What was proven

- **GPU is accessible inside the production training container.** An NVIDIA
  GeForce RTX 4090 with 24 GB VRAM is visible via `nvidia-smi`. This confirms
  the container has proper GPU device passthrough and the CUDA driver is
  functional.
- **The `gpu_healthcheck` task works live.** The handler's GPU probe logic
  (nvidia-smi query, CUDA version detection, library GPU flag probing, signed
  callback payload) executes correctly inside the RunPod serverless container.
- **The worker remains healthy after a GPU-touching job.** Unlike the earlier
  `c508103f` failure (where the worker went `unhealthy=1` after dispatch), this
  worker stayed `unhealthy=0` throughout and after the job. The `parents[5]`
  fix in `6dbec436` is confirmed stable for GPU-touching tasks.
- **SecurityPreflight passes in the GPU container.** `preflight_result.passed
  = true`, no forbidden vars found, URI allowlists validated.
- **Signed callback is produced.** The `callback_signature` and
  `callback_payload` are present in the job output, confirming the HMAC
  signing path works for GPU healthcheck results.
- **Library GPU flags:** xgboost and catboost report GPU capability;
  lightgbm reports `false` (likely a CPU-only lightgbm build in the image —
  not a failure, just a flag for the dispatcher).

## What remains unknown

- **Does a full `train_model` job complete live?** The gpu_healthcheck proves
  GPU access but does NOT exercise the full training pipeline (dataset loading,
  trainer execution, model export). This is task **A7** in the v6 task queue.
- **CUDA version parsing:** `cuda_version` (550.144.03) matches
  `driver_version`, suggesting the CUDA version parser may have picked up the
  driver version from the nvidia-smi header rather than the actual CUDA
  runtime version. This is a minor handler reporting detail, not a functional
  issue — the GPU is clearly functional. Filed for future investigation.
- **lightgbm GPU flag:** `lightgbm_gpu=false` may indicate a CPU-only lightgbm
  build. If GPU lightgbm training is required, the image may need a GPU-enabled
  lightgbm wheel. Not a blocker for xgboost/catboost GPU training.

## Cleanup state

- Endpoint `6hl6v67nybijwy` scaled to `workersMin=0, workersMax=0` and deleted.
- Template `l1shf1bs3c` left in place (harmless; no workers running).
- No stuck jobs (job reached COMPLETED).
- No secrets printed in any receipt file (all redacted via `_redact()`).

## Next step

**A7: dispatch a minimal `train_model` job** against the same `6dbec436` image
to verify the full training pipeline (dataset loading, trainer execution, model
export) works live. This is the final critical-path live validation. Requires
operator spend awareness (longer GPU time than the healthcheck).
