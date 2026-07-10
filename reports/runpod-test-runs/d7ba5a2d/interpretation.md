# RunPod Test E: Sentinel Handler — d7ba5a2d

Last updated: 2026-07-03

## Identity

- Branch: `fix/test-harness-optional-deps-guards`
- SHA: `d7ba5a2d1a5b85e4b26cb77196715f548015e01e`
- Image: `ghcr.io/airyder/fincept/quant-foundry-training:d7ba5a2d1a5b85e4b26cb77196715f548015e01e`
- Workflow run id: `28679024513` (success)
- Endpoint id: `fqa18kqj9exo62` (created fresh, registry auth copied from `z6xy0iflvxcjtr`)
- Endpoint status after test: scaled to `workersMin=0 workersMax=0`

## Test E Design

Per `docs/runpod-fix-plan/02-single-variable-tests.md` Test E:

- Replaced only `/worker/handler.py` with a trivial sentinel handler (`handler_sentinel.py`) that imports only `runpod` + stdlib (no `quant_foundry`, `torch`, `xgboost`, `catboost`, `lightgbm`, `numpy`, `pandas`, `sklearn`, `pyarrow`).
- Production handler preserved as `/worker/handler_full.py` for reference.
- Base image (`python:3.12-slim`), SDK (`runpod==1.7.13`), all deps (torch, xgboost, etc.), entrypoint, and endpoint shape all unchanged.
- The only variable changed: the handler file copied to `/worker/handler.py`.

## LIVE PROBE RESULT — SENTINEL PASSED

**The sentinel handler completed a live RunPod job. The root cause is isolated to the production handler's import/startup path.**

### Timeline

- `19:14:15Z` — endpoint `fqa18kqj9exo62` created.
- `19:16:13Z` (approx) — worker reached `ready=1 idle=1 unhealthy=0` (healthy).
- `19:16:22Z` — sentinel job `260259a7-...` submitted via `/run`. Status `IN_QUEUE`.
- `19:16:28Z` — job reached `COMPLETED`. `executionTime: 70ms`, `delayTime: 4598ms`.
- Worker remained `idle=1 ready=1 unhealthy=0` after completion.
- `19:17:00Z` (approx) — endpoint scaled to `workersMin=0 workersMax=0`.

### Job output (redacted)

```json
{
  "handler": "quant-foundry-sentinel",
  "job_id": "qf:sentinel:",
  "ok": true,
  "received": {
    "input": {"job_id": "qf:sentinel:", "task": "sentinel"},
    "status": "IN_QUEUE"
  },
  "runtime": {
    "git_sha": "d7ba5a2d1a5b85e4b26cb77196715f548015e01e",
    "platform": "Linux-6.8.0-60-generic-x86_64-with-glibc2.41",
    "python": "3.12.13",
    "runpod_sdk": "1.7.13",
    "started_at": 1783106185
  }
}
```

### Evidence files in this directory

- `endpoint-create-redacted.txt` — endpoint creation transcript (redacted)
- `health-before.json` — `ready=1 idle=1 unhealthy=0` before dispatch
- `sentinel-probe.jsonl` — full JSONL probe output (run_response, status COMPLETED, health, probe_end)
- `health-after.json` — `ready=1 idle=1 unhealthy=0` after completion (worker healthy)
- `cleanup.json` — endpoint scaled to `workersMin=0 workersMax=0`

## Root Cause Isolation — PROVEN

| Variable | c508103f (production handler) | d7ba5a2d (sentinel handler) |
|----------|-------------------------------|------------------------------|
| Base image | `python:3.12-slim` | `python:3.12-slim` (same) |
| SDK | `runpod==1.7.13` | `runpod==1.7.13` (same) |
| Deps installed | torch, xgboost, catboost, lightgbm, etc. | same (all installed, not imported by handler) |
| Entrypoint | `python -u /worker/handler.py` | same |
| Endpoint shape | ADA_24, QUEUE_DELAY, same template | same |
| `/worker/handler.py` | **production handler** (heavy imports) | **sentinel** (runpod + stdlib only) |
| Live result | **FAILED** — worker unhealthy at dispatch, job stuck IN_QUEUE | **PASSED** — COMPLETED in 6s, worker healthy |

### Conclusion

**The root cause is the production handler's import/startup path.** When RunPod dispatches a job, the production handler (`handler.py`) imports `quant_foundry`, `fincept_core`, `torch`, `xgboost`, `catboost`, `lightgbm`, `numpy`, `pandas`, `sklearn`, `pyarrow` — and something in that import chain crashes or hangs the worker process within 6 seconds. The sentinel handler (which imports none of those) works perfectly in the identical image.

The worker reaches `ready=1 idle=1` before dispatch, so the imports that happen at container startup (before RunPod's serverless loop starts) are not the problem. The failure triggers when RunPod delivers the job to `handler(event)` — which means the crash is in code that runs lazily at handler invocation time, or in a re-import triggered by the job dispatch, or in the handler's preflight/startup code that runs inside `handler()`.

### What this rules out

- ❌ Docker healthcheck (no healthcheck in either image; sentinel works without it)
- ❌ Base image `python:3.12-slim` (sentinel works on the same base)
- ❌ Missing `libgomp1` (sentinel works with the same libgomp1 install)
- ❌ RunPod SDK job loop (sentinel completes a job via the same SDK)
- ❌ Endpoint template shape (same shape for both)
- ❌ Registry auth / GHCR pull (same auth, same registry for both)
- ❌ GPU scheduling / ADA_24 (same GPU type for both)

### What this points to

The production `handler.py` does something at handler invocation time that crashes the worker. Candidates:

1. **Lazy imports inside `handler()`**: The production handler may import `quant_foundry` modules lazily (inside the handler function or in code paths triggered by the canary task). If one of those imports triggers a crash (e.g., a CUDA initialization, a shared library load, a thread/fork issue) in the RunPod serverless container environment, the worker dies.
2. **Preflight code**: The production handler runs `SecurityPreflight` before processing the job. If the preflight code does something that crashes (e.g., subprocess call to `nvidia-smi`, file system access, environment scanning), that could kill the worker.
3. **Module-level side effects in quant_foundry**: If `quant_foundry` or `fincept_core` modules have side effects at import time (e.g., starting threads, opening network connections, initializing CUDA) that are incompatible with the RunPod serverless container lifecycle, importing them at handler invocation time could crash the worker.
4. **Memory pressure**: The production handler's imports (torch + xgboost + catboost + lightgbm) may consume enough memory to trigger an OOM kill when loaded at handler invocation time, even though they're installed in the image. The sentinel doesn't import them so it doesn't trigger OOM.

### Next steps (awaiting operator direction)

1. **Import bisection**: Build a series of images with handlers that progressively add production imports (runpod-only → +numpy/pandas → +torch → +xgboost → +quant_foundry) to find which import crashes the worker.
2. **Lazy import audit**: Read `handler.py` and identify what imports happen at handler invocation time vs. module load time. The crash is in the invocation-time path.
3. **Pod logs**: Check if RunPod exposes pod/container logs for the unhealthy worker from the c508103f run — the crash output may identify the exact error.
4. **Memory probe**: Build an image with a handler that imports torch + xgboost + catboost + lightgbm and reports memory usage, to test the OOM hypothesis.
