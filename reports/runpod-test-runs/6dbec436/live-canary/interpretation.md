# Live Production Canary — Interpretation

**Image SHA:** 6dbec436c92b57a788b84622338baacc3df8665d
**Image tag:** ghcr.io/airyder/fincept/quant-foundry-training:6dbec436c92b57a788b84622338baacc3df8665d
**Date:** 2026-07-03
**Workflow run id:** 28683991294 (success, 13m28s)
**Branch:** fix/test-harness-optional-deps-guards

## Executive Summary

Three consecutive live production canaries PASSED against the exact SHA image
containing the `parents[5]` fix. The production handler is now the direct
RunPod entrypoint (`/worker/handler.py`), and the `IndexError: 5` that
prevented the worker from starting has been eliminated.

## Canary Results

| Run | Endpoint ID | Job ID | Time to ready | Time to COMPLETED | Worker unhealthy |
|-----|-------------|--------|---------------|-------------------|------------------|
| 1 | yyxwraovovy1un | d3441295...-u2 | ~25s | ~5s | 0 |
| 2 | yju9c75p80odby | 8641a636...-u2 | ~25s | ~5s | 0 |
| 3 | rzw1aifoi2zhc7 | 050c1034...-u1 | ~25s | ~5s | 0 |

All three canaries:
- Worker reached `ready=1, idle=1, unhealthy=0` before dispatch
- Job reached `COMPLETED` within 5 seconds of dispatch
- Worker remained `unhealthy=0` after completion
- Callback signature present in output (redacted in receipts)
- Endpoint scaled down and deleted after test

## Endpoint Shape

- GPU: ADA_24
- Scaler: QUEUE_DELAY, value 4
- Workers: workersMin=1, workersMax=1
- Idle timeout: 300s
- Container disk: 20 GB
- Docker args: empty string
- Env: QUANT_FOUNDRY_CALLBACK_SECRET (from operator environment)
- Registry auth: cmqu7l5rz0047nzyt0o28je3d

## What Was Fixed

### parents[5] IndexError (equities.py, news.py)

The root cause of the original dispatch failure was
`pathlib.Path(__file__).resolve().parents[5]` in
`quant_foundry/data_ingestion/equities.py` and `news.py`. In the RunPod
container, the file path `/worker/quant_foundry/data_ingestion/equities.py`
has only 4 parents (indices 0-3), so `parents[5]` raised `IndexError: 5`.
This crashed the production handler at module import time, before
`runpod.serverless.start()` could be called.

The fix guards the index access and skips `sys.path` insertion when the
`scripts/` or `experiments/` directory is unavailable in the worker image.
Downstream imports (`build_dataset_manifest`, `news_impact_model.events`)
are guarded with `ModuleNotFoundError` fallback since those modules are not
in the worker image.

### Dockerfile restoration

The Dockerfile was restored to copy `handler.py` directly to
`/worker/handler.py` (the production handler), instead of the bisection
handler. The bisection handler remains in the repo for future diagnostics
but is not copied into the image.

## Cleanup

All test endpoints and templates have been deleted. No warm endpoints remain.

## Acceptance Criteria Met

- [x] Job reaches `COMPLETED` (all 3 runs)
- [x] Job does not stay `IN_QUEUE` (all 3 completed in ~5s)
- [x] Worker remains `unhealthy=0` after completion (all 3 runs)
- [x] Callback signature is present but secrets are not printed
- [x] Debug endpoint is scaled down or deleted after the test
- [x] `parents[5]` path indexing is fixed without breaking ingestion imports
- [x] RunPod training Dockerfile runs the production handler directly
- [x] Fresh exact-SHA production image completes live canary validation
- [x] Worker health remains acceptable after the job
- [x] Debug endpoints are cleaned up
- [x] Receipts are written and redacted
