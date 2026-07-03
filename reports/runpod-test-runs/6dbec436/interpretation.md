# RunPod Training Worker Validation — 6dbec436 — PRODUCTION CANARY PASSED

Last updated: 2026-07-03T22:35Z

## Identity

- Branch: `fix/test-harness-optional-deps-guards`
- SHA: `6dbec436c92b57a788b84622338baacc3df8665d`
- Image: `ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d`
- Workflow run id: `28683991294` (success, 13m28s)
- Endpoint id: `4jc1opwj11zmai` (created fresh, registry auth copied from `z6xy0iflvxcjtr`)
- Endpoint status after test: scaled to `workersMin=0 workersMax=1`

## Test Performed

**Item 5: Fresh live production-handler canary** (per `docs/runpod-fix-plan/07-remaining-work.md` item 5).

This is the first live production-handler canary after the `parents[5]` IndexError fix
(commit `6dbec436`). The Dockerfile copies the production `handler.py` to `/worker/handler.py`
(no diagnostic wrappers, no healthcheck). The only code change from the failed `c508103f`
image is the `parents[5]` guard in `equities.py`/`news.py` (guarded index +
`ModuleNotFoundError` fallback for `build_dataset_manifest` and `news_impact_model.events`).

Endpoint shape:
- GPU: `ADA_24`
- scaler: `QUEUE_DELAY`, scaler value: `4`
- workers: `workersMin=1` (during test), `workersMax=1`
- idle timeout: `300`
- container disk: `20 GB`
- docker args: empty string
- env: `QUANT_FOUNDRY_CALLBACK_SECRET` only (NO diagnostic skip vars — testing the real
  production path)
- registry auth: copied from proven-working smoke endpoint `z6xy0iflvxcjtr`

## LIVE PROBE RESULT — CANARY PASSED (3/3 COMPLETED)

**The production handler with the `parents[5]` fix completed three live RunPod canary jobs
successfully. The worker remained healthy throughout.**

### Timeline

- `22:25Z` (approx) — endpoint `4jc1opwj11zmai` created with full-SHA image tag.
- `22:28Z` (approx) — worker reached `ready=1 idle=1 unhealthy=0` (healthy, poll 10).
- `22:29:21Z` — canary 1 dispatched (`92f886af-...`). Status `IN_QUEUE`.
- `22:29:32Z` — canary 1 reached `COMPLETED` (delayTime=5574ms, executionTime=44ms).
- `22:29:33Z` — post-canary-1 health: `completed=1, failed=0, unhealthy=0`.
- `22:29:41Z` — canary 2 dispatched (`d5115bcb-...`). Reached `COMPLETED` immediately
  (delayTime=18ms, executionTime=43ms).
- `22:29:51Z` — canary 3 dispatched (`c82f4b0f-...`). Reached `COMPLETED` immediately
  (delayTime=20ms, executionTime=50ms).
- `22:30Z` (approx) — endpoint scaled to `workersMin=0`.
- Final health: `completed=3, failed=0, unhealthy=0`.

### Job outputs (redacted)

All three canaries returned the same shape (example — canary 1):

```json
{
  "callback_payload": "{\"job_id\": \"qf:prod-canary:6dbec436:001\", \"payload\": {\"nonce\": \"live-001\"}, \"result_type\": \"callback_secret_canary\", \"schema_version\": 1, \"worker_id\": \"runpod-canary\"}",
  "callback_signature": "39c61c9d...",
  "callback_ts": 1783117768,
  "canary": true,
  "job_id": "qf:prod-canary:6dbec436:001",
  "nonce": "live-001",
  "preflight_result": {
    "callback_url_validated": false,
    "container_user": "root:0",
    "forbidden_vars_found": [],
    "mode": "canary",
    "passed": true,
    "redacted_config": {
      "QUANT_FOUNDRY_CALLBACK_SECRET": "****",
      "QUANT_FOUNDRY_GIT_SHA": "6dbec436c92b57a788b84622338baacc3df8665d",
      "QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS": "1800",
      "QUANT_FOUNDRY_TRAINING_MODE": "canary",
      "QUANT_FOUNDRY_USE_REAL_TRAINER": "true",
      "RUNPOD_WEBHOOK_GET_JOB": "****"
    },
    "uri_allowlists_validated": true,
    "writable_dirs": ["/tmp"]
  }
}
```

### Exact payloads

```json
{"input":{"task":"callback_secret_canary","job_id":"qf:prod-canary:6dbec436:001","nonce":"live-001"}}
{"input":{"task":"callback_secret_canary","job_id":"qf:prod-canary:6dbec436:002","nonce":"live-002"}}
{"input":{"task":"callback_secret_canary","job_id":"qf:prod-canary:6dbec436:003","nonce":"live-003"}}
```

### Health observations

- Before dispatch: `ready=1 idle=1 unhealthy=0`
- After canary 1: `completed=1, failed=0, unhealthy=0` (worker remained healthy)
- After canary 2: `completed=2, failed=0, unhealthy=0` (worker remained healthy)
- After canary 3: `completed=3, failed=0, unhealthy=0` (worker remained healthy)
- Worker ID: `goi504hgln2q6x` (same worker for all three jobs — stable, no recycle)

## What Was Proven

1. **The `parents[5]` IndexError was the root cause of the dispatch-time crash.** At
   `c508103f` (with the bug), the worker reached `ready=1` then went `unhealthy=1` ~6
   seconds after job dispatch. At `6dbec436` (with the fix), the worker reaches `ready=1`
   and completes canary jobs in 44-50ms (executionTime). The only code change between the
   two images is the `parents[5]` guard in `equities.py`/`news.py`.

2. **The production handler now boots, accepts jobs, executes, returns terminal results,
   and remains healthy.** All three canaries reached `COMPLETED`. The worker stayed
   `unhealthy=0` throughout. The callback envelope + signature were returned correctly.
   SecurityPreflight passed (`mode=canary, passed=true`).

3. **Previous theories are now fully resolved:**
   - Docker healthcheck: disproved by `c508103f` (no healthcheck, still failed).
   - Base image + libgomp1: disproved by `c508103f` (python:3.12-slim + libgomp1, still failed).
   - SDK/job loop: disproved by `d7ba5a2d` (sentinel completed in same image shape).
   - Module-level ML imports / memory pressure: disproved by this test (production handler
     with full ML imports boots and completes jobs).
   - SecurityPreflight crash: disproved by this test (preflight passed in all 3 canaries).
   - **`parents[5]` IndexError: CONFIRMED as root cause** (fix resolved the crash).

## Operational Note: Image Tag Must Use Full SHA

An initial endpoint (`jtr18kqj9exo62` — no, `jtr18cdh5lgov2`) was created with the short
SHA tag `6dbec436`. The `build-runpod-training.yml` workflow tags images with the full
40-char SHA (`6dbec436c92b57a788b84622338baacc3df8665d`), NOT a short SHA. The short-SHA
image does not exist in the registry, so the container failed to pull and exited immediately
(`desiredStatus=EXITED, docker=None, unhealthy=1` at startup). This was an operational
error, not a code issue. The broken endpoint was scaled to `workersMin=0` and a fresh
endpoint with the correct full-SHA tag was created.

**Lesson for future runs: always use the full 40-char SHA for the image tag, matching
`github.sha` in the workflow.**

## What Remains Unknown / Next Steps

1. **Real training jobs (not just canary):** The canary path exercises preflight + callback
   signing but does NOT run actual model training (`train_model` task). The next step is to
   dispatch a `train_model` or `gpu_healthcheck` job to verify the full training pipeline
   (dataset loading, trainer execution, model export) works live.

2. **Layered handler (handler_layered.py):** The layered handler with `diag_layer` was the
   original plan's approach. The production handler canary passing supersedes the layered
   approach for the immediate fix. The layered handler remains available for future
   diagnostics if a new failure boundary appears.

3. **Repo hygiene (item 12):** Uncommitted unrelated changes (`infra/docker/api.Dockerfile`,
   `SESSION_HANDOFF.md`, `handoffs/`, `kimiSuggestionFix.md`, `reports/ci-triage/`) should
   be classified before the final ship.

## Current Endpoint Cleanup State

- Endpoint `4jc1opwj11zmai` (production canary): scaled to `workersMin=0 workersMax=1`.
- Endpoint `jtr18cdh5lgov2` (broken short-SHA): scaled to `workersMin=0 workersMax=1`.
- No endpoints with `workersMin > 0`.
- No stuck jobs (all 3 canaries reached `COMPLETED`).
- No debug endpoints left with `workersMin=1`.
- No API keys or callback secrets printed in this receipt (all redacted).

## Acceptance Checklist Update

- [x] Smoke worker still completes a live RunPod job. — PROVEN by `d7ba5a2d` (sentinel).
- [x] Training image SDK job loop works. — PROVEN by `d7ba5a2d` (sentinel) + this test.
- [x] **Full production canary path completes live.** — **PROVEN by this test** (3/3 COMPLETED).
- [x] **Worker remains healthy after production canary.** — **PROVEN** (unhealthy=0 throughout).
- [x] No debug endpoint left with `workersMin=1`. — DONE.
- [x] No secrets printed. — DONE.
- [x] Build workflow produces exact SHA-tagged image. — DONE (run `28683991294`).
- [ ] Real training job (`train_model`) completes live. — NOT YET TESTED.

## Next Step

Dispatch a `train_model` or `gpu_healthcheck` job against the same `6dbec436` image to
verify the full training pipeline works live (not just the canary path).

### Exact next prompt

```
Continue driving the Fincept / Quant Foundry RunPod training-worker fix forward.

Read the latest state from:
- reports/runpod-test-runs/6dbec436/interpretation.md (PRODUCTION CANARY PASSED — 3/3 COMPLETED, parents[5] was root cause)
- docs/runpod-fix-plan/07-remaining-work.md
- recent git commits

The production handler canary PASSED live at SHA 6dbec436 (image ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d). Three callback_secret_canary jobs completed (44-50ms executionTime), worker remained healthy (unhealthy=0), same worker ID for all three. The parents[5] IndexError fix is confirmed as the root cause.

IMPORTANT: the build-runpod-training.yml workflow tags images with the FULL 40-char SHA (github.sha), not a short SHA. Always use the full SHA for the image tag.

Next test: dispatch a gpu_healthcheck job (mode=canary) against the same 6dbec436 image to verify the GPU is accessible inside the container. Then dispatch a train_model job with a minimal dataset to verify the full training pipeline works live.

Create a fresh endpoint (or reuse 4jc1opwj11zmai scaled back up) with:
- image: ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d (FULL SHA)
- gpu: ADA_24, scaler QUEUE_DELAY value 4, workersMin=1 workersMax=1, idleTimeout=300, containerDiskInGb=20, dockerArgs=""
- env: QUANT_FOUNDRY_CALLBACK_SECRET only
- registry auth copied from z6xy0iflvxcjtr

Dispatch {"input":{"task":"gpu_healthcheck","mode":"canary","job_id":"qf:gpu-hc:6dbec436:001"}}. Poll /health and /status every 5s.

If gpu_healthcheck COMPLETED: dispatch a minimal train_model job. If gpu_healthcheck FAILS: investigate GPU/driver/runtime inside the container.

Scale down endpoint, cancel stuck jobs, write receipt with exact next prompt.
```
