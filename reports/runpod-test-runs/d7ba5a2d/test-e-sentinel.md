# RunPod Training Worker Validation — Test E Sentinel — d7ba5a2d

Last updated: 2026-07-03T19:20Z

## Identity

- Branch: `fix/test-harness-optional-deps-guards`
- SHA: `d7ba5a2d1a5b85e4b26cb77196715f548015e01e`
- Image: `ghcr.io/airyder/fincept/quant-foundry-training:d7ba5a2d1a5b85e4b26cb77196715f548015e01e`
- Workflow run id: `28679024513` (success, 6m59s)
- Endpoint id: `17e5k8rxp9cf5q` (created fresh, registry auth copied from `z6xy0iflvxcjtr`)
- Endpoint status after test: scaled to `workersMin=0 workersMax=0`

## Test Performed

**Test E: SDK Job Loop Sentinel Inside Training Image** (per `docs/runpod-fix-plan/02-single-variable-tests.md` Test E).

The Dockerfile at this SHA copies `handler_sentinel.py` to `/worker/handler.py` instead of the production handler. The sentinel handler imports ONLY `runpod` + stdlib (same as the proven-working smoke worker). The production handler remains available as `/worker/handler_full.py` for reference. No other variable changed: same base image (`python:3.12-slim`), same SDK (`runpod==1.7.13`), same entrypoint, same ML libraries installed (torch, xgboost, catboost, lightgbm).

## LIVE PROBE RESULT — SENTINEL PASSED

**The sentinel handler completed a live RunPod job successfully.**

### Timeline

- `19:14:44Z` — endpoint `17e5k8rxp9cf5q` created.
- `19:15:24Z` (approx) — worker reached `ready=1 idle=1 unhealthy=0` (healthy, poll 13).
- `19:16:58Z` — sentinel job `eb7d9109-3501-4ea0-b225-6f8f9b6b53c4-u1` submitted via `/run`. Status `IN_QUEUE`.
- `19:16:58Z` — job reached `COMPLETED` (delayTime=17ms, executionTime=67ms).
- `19:16:58Z` — post-completion health: `completed=1, failed=0, unhealthy=0`.
- `19:19:00Z` (approx) — endpoint scaled to `workersMin=0 workersMax=0`.

### Job output (redacted)

```json
{
  "handler": "quant-foundry-sentinel",
  "job_id": "qf:sentinel:d7ba5a2d:001",
  "ok": true,
  "received": {
    "input": {"job_id": "qf:sentinel:d7ba5a2d:001", "task": "sentinel"}
  },
  "runtime": {
    "git_sha": "d7ba5a2d1a5b85e4b26cb77196715f548015e01e",
    "platform": "Linux-5.15.0-133-generic-x86_64-with-glibc2.41",
    "python": "3.12.13",
    "runpod_sdk": "1.7.13",
    "started_at": 1783106213
  }
}
```

### Exact payload

```json
{"input":{"task":"sentinel","job_id":"qf:sentinel:d7ba5a2d:001"}}
```

### Health observations

- Before dispatch: `ready=1 idle=1 unhealthy=0`
- After completion: `completed=1 failed=0 unhealthy=0` (worker remained healthy)
- Worker ID: `ujv2hhc5a67axd`

## What Was Proven

1. **The RunPod SDK job loop works inside the training image shape.** The sentinel handler (trivial, no heavy imports) completed a live job in 67ms. The base image (`python:3.12-slim` + `libgomp1`), SDK (`runpod==1.7.13`), entrypoint, and container runtime are all functional.

2. **The failure is isolated to the production handler's code path.** The production handler (`handler.py`) goes unhealthy ~6 seconds after job dispatch (per `c508103f` receipt). The sentinel handler completes in 67ms. The only difference is the handler code: the production handler imports `quant_foundry`, `fincept_core`, `torch` (via quant_foundry), `xgboost`, `catboost`, `lightgbm` at module level and runs `SecurityPreflight` + task dispatch at request time. The sentinel imports only `runpod` + stdlib.

3. **Previous theories are now fully disproved:**
   - Docker healthcheck: disproved by `c508103f` (no healthcheck, still failed).
   - Base image + libgomp1: disproved by `c508103f` (python:3.12-slim + libgomp1, still failed).
   - SDK/job loop: disproved by this test (sentinel completes in same image shape).

## What Remains Unknown

The exact boundary within the production handler that crashes the worker at dispatch time. The leading hypotheses are:

1. **Memory pressure from module-level ML imports:** The production handler imports `torch`, `xgboost`, `catboost`, `lightgbm` (via `quant_foundry.real_trainer`, `quant_foundry.runpod_training`, etc.) at module level. These imports succeed at startup (worker reaches `ready=1`), but the loaded libraries consume significant memory. When the RunPod SDK dispatches a job, additional memory allocation could push the container over its OOM limit, killing the process ~6 seconds after dispatch. The sentinel does NOT import these libraries (they're installed but not loaded), so it has ample free memory.

2. **SecurityPreflight crash at dispatch time:** The production handler runs `SecurityPreflight.run()` at the top of `handler(event)`. This does `socket.getaddrinfo` (callback URL validation), directory probes, and env var scanning. A hang or crash in preflight could kill the worker. The sentinel does not run preflight.

3. **Handler body crash:** Something in `_handle_canary` or the task dispatch logic crashes the process in a way that the try/except wrapper cannot catch (e.g., SIGKILL from OOM, segfault from a native library).

## Current Endpoint Cleanup State

- Endpoint `17e5k8rxp9cf5q`: scaled to `workersMin=0 workersMax=0`.
- No stuck jobs (sentinel job `eb7d9109-...` reached `COMPLETED`).
- No debug endpoints left with `workersMin=1`.
- No API keys or callback secrets printed in this receipt.

## Acceptance Checklist Update

- [x] Smoke worker still completes a live RunPod job. — **PROVEN by this test** (sentinel = same shape as smoke, trivial handler, COMPLETED).
- [x] Training image SDK job loop works. — **PROVEN by this test** (sentinel inside training image, COMPLETED).
- [ ] Full production canary path completes live. — **STILL FAILING** (production handler goes unhealthy at dispatch).
- [ ] Worker remains healthy after production canary. — **STILL FAILING**.
- [x] No debug endpoint left with `workersMin=1`. — **DONE**.
- [x] No secrets printed. — **DONE**.
- [x] Build workflow produces exact SHA-tagged image. — **DONE** (run `28679024513`).

## Next Step: Test E2 — Sentinel + Production Imports

The next single-variable test isolates whether the production handler's module-level imports cause the crash at dispatch time (memory pressure hypothesis).

### Plan

1. Create `handler_sentinel_with_imports.py` that imports the SAME modules as `handler.py` at module level (`quant_foundry.*`, `fincept_core.datasets`, etc.) but has a trivial handler body (returns a dict like the sentinel, no preflight, no task dispatch).
2. Change only the Dockerfile `COPY` line to use this new handler as `/worker/handler.py`.
3. Build/push the image.
4. Create a fresh endpoint with the same shape.
5. Dispatch the sentinel payload.
6. Interpret:
   - If COMPLETED: the crash is in the handler FUNCTION BODY (preflight, canary logic, task dispatch), not in the imports. Next: bisect the handler body (add preflight, then add canary logic).
   - If FAILS (unhealthy at dispatch): the crash is caused by the module-level imports (memory pressure from loaded ML libraries). Solution: convert to lazy imports (import torch/xgboost/etc. inside the handler function only when needed).

### Exact next prompt

```
Continue driving the Fincept / Quant Foundry RunPod training-worker fix forward.

Read the latest state from:
- reports/runpod-test-runs/d7ba5a2d/test-e-sentinel.md (Test E PASSED — sentinel completes live, production handler fails)
- docs/runpod-fix-plan/02-single-variable-tests.md
- recent git commits

The sentinel handler (trivial, no heavy imports) COMPLETED live inside the training image shape. This proves the SDK, base image, and container runtime are fine. The failure is isolated to the production handler's code path.

Next test: Test E2 — Sentinel + Production Imports.
Create handler_sentinel_with_imports.py that imports the SAME modules as handler.py at module level (quant_foundry.data_ingestion.quality_report, quant_foundry.dataset_manifest, quant_foundry.real_trainer, quant_foundry.runpod_training, quant_foundry.schemas, quant_foundry.signatures, quant_foundry.training_manifest, fincept_core.datasets) but has a trivial handler body (returns a dict like handler_sentinel.py, no preflight, no task dispatch).

Change only the Dockerfile COPY line to use handler_sentinel_with_imports.py as /worker/handler.py. Do not change base image, SDK, entrypoint, or handler logic.

Build/push, create fresh endpoint (workersMin=1, ADA_24, registry auth from z6xy0iflvxcjtr, idleTimeout=300, scalerType=QUEUE_DELAY, scalerValue=4, containerDiskInGb=20, dockerArgs=""), dispatch {"input":{"task":"sentinel","job_id":"qf:sentinel-imports:<short_sha>:001"}}, poll /health and /status every 5s.

If COMPLETED: crash is in the handler function body (preflight/canary logic). Next: add preflight to the sentinel+imports handler and retest.
If FAILS: crash is from module-level ML imports (memory pressure). Solution: convert production handler to lazy imports.

Scale down endpoint, cancel stuck jobs, write receipt with exact next prompt.
```
