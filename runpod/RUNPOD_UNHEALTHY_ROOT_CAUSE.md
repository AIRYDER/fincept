# RunPod Serverless Training Worker - Root Cause and Fix

## Date
2026-07-03

## Current Conclusion

The `python:3.12-slim` image path was a false positive. It proved that a
minimal smoke worker could receive and complete jobs, but commit `8c45c484`
reproduced the production failure on the original training endpoint: the
worker became ready, the full training job stayed `IN_QUEUE`, and health later
dropped to zero active workers.

The safe baseline is the `8bcb9c69` worker shape:

- `nvidia/cuda:12.4.1-runtime-ubuntu22.04`
- apt-installed Python 3.10 runtime
- startup compatibility shims for `enum.StrEnum`, `datetime.UTC`, and
  `typing.Self`
- embedded startup preflight in the Dockerfile
- layered handler entrypoint defaulting to the real training handler

Do not promote an image from `/health` or canary-smoke success alone. The
promotion gate for this worker is a completed full LightGBM training job on
the original endpoint, `mxp0bv8itggwev`.

## Symptoms

Several distinct failures were mixed together during debugging:

### Unhealthy Before Job Pickup

- `pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime` and some RunPod base images
  caused workers to go unhealthy during initialization.
- Increasing `RUNPOD_INIT_TIMEOUT` and changing healthchecks did not make that
  image family reliable.
- This was a container/runtime startup failure, not model-training logic.

### Ready But Job Stuck In Queue

- The worker reached `ready=1, idle=1`.
- A training job was submitted.
- The job stayed `IN_QUEUE`.
- Worker health later dropped to zero active workers or became unhealthy.

This is the most dangerous failure mode because health initially looks good,
but the platform never completes the job.

### Handler Import/runtime Mismatch

The real training handler imports project code that expects newer Python
symbols such as `enum.StrEnum`, `datetime.UTC`, and `typing.Self`. On the
CUDA/Ubuntu image, Python 3.10 is installed from apt, so those symbols must be
shimmed before project imports.

## Evidence

| Image / commit | Endpoint | Result |
|---|---|---|
| `8bcb9c69` training image | `mxp0bv8itggwev` | Completed full LightGBM training job `9c587ec9-c449-4050-b983-efba0638f634-u2` and returned artifact `artifact:8518eac762e77c78` |
| `8c45c484` python:3.12-slim training image | `mxp0bv8itggwev` | Job `243dba18-ff96-4754-98eb-40abe1132dd5-u1` stayed `IN_QUEUE`; endpoint health dropped to zero active workers |
| minimal python:3.12-slim smoke worker | isolated smoke endpoints | Completed smoke jobs, but this did not prove the real training image |

## Root Cause

The practical root cause was a bad promotion signal. Minimal smoke endpoints
proved that RunPod job dispatch could work in isolation, but they did not prove
the real training container. The production worker needed both of these:

1. a container base that can initialize on RunPod serverless GPU workers, and
2. a Python/runtime shape compatible with the real handler import tree.

The `8bcb9c69` line satisfied both conditions. The later `8c45c484` rewrite
changed too many variables at once and failed the real promotion gate.

## Fix

Restore the training worker to the verified `8bcb9c69` shape and build a new
image from the latest branch head. The Dockerfile should use:

```dockerfile
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
```

It should keep the Python 3.10 compatibility shims and layered handler used by
the verified full-training image.

## Required Verification

For every future candidate image:

1. Build and push `ghcr.io/airyder/fincept/quant-foundry-training:<git_sha>`.
2. Deploy that exact image tag to `mxp0bv8itggwev`.
3. Purge or cancel any stale queued jobs.
4. Submit one full LightGBM training job.
5. Poll `/status` and `/health` until the job reaches a terminal state.
6. Promote only if the job is `COMPLETED` and the result includes a real
   artifact id, artifact URI, model family, metrics, and no typed callback
   failure code.

Smoke-only endpoints remain useful for narrowing platform problems, but they
are not release evidence for the training worker.

## Operational Notes

- Use bearer auth for RunPod API calls; do not put API keys in query strings.
- Keep `QUANT_FOUNDRY_CALLBACK_SECRET` out of Docker `ENV` layers.
- If diagnostic output ever includes API keys, callback secrets, or job-take
  URLs, rotate the affected secrets before treating the environment as clean.
