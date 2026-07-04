# Tier 0.4 — Image Slimming: Slim Training Dockerfile (no torch)

**Task ID:** task-mr6i5cf2-5972ffe9
**Agent:** Builder 2
**Branch:** `tier0/image-slimming`
**Date:** 2026-07-04

## Objective

Create a slim variant of the RunPod serverless training worker Dockerfile
that drops the ~2 GB torch CUDA wheel for lightgbm/xgboost/catboost-only
training, reducing the image from ~6 GB to an estimated <1.5 GB. The
production Dockerfile (with torch) is preserved as a separate `-torch` tag
for future neural-network work.

## What was done

1. **Created `runpod/quant-foundry-training/Dockerfile.slim`** — a
   size-optimised variant of the production Dockerfile that:
   - Uses the same `python:3.12-slim` base (never nvidia/cuda,
     pytorch/pytorch, or runpod/base).
   - Installs the same OS dependencies (libgomp1, libglib2.0-0,
     build-essential, curl, ca-certificates).
   - Copies the same source trees (quant_foundry, fincept_core subpackages,
     shared worker_status, handler.py, preflight.py).
   - **DROPS** the `torch==2.4.1` pip install (the ~2 GB CUDA 12.4 wheel
     from download.pytorch.org/whl/cu124) entirely.
   - Keeps lightgbm, xgboost, catboost, pandas, pyarrow, scikit-learn,
     numpy, pydantic, pydantic-settings, httpx, runpod.
   - Same non-root user (trainer, uid 1000), GIT_SHA arg, ENV vars,
     ENTRYPOINT.
   - NO Docker HEALTHCHECK.

2. **Created `runpod/tests/test_dockerfile_slim.py`** — 23 static
   validation tests covering:
   - Base image is `python:3.12-slim` (not forbidden bases).
   - No import-based HEALTHCHECK (reuses `healthcheck_guard.py`).
   - No HEALTHCHECK directive at all (stricter check).
   - torch is NOT installed.
   - pytorch.org wheel index is not used in pip install commands.
   - lightgbm, xgboost, runpod ARE installed.
   - Supporting packages (catboost, pandas, pyarrow, scikit-learn, numpy,
     pydantic, pydantic-settings, httpx) are installed.
   - ENTRYPOINT matches the production Dockerfile exactly.
   - GIT_SHA arg, non-root user, handler/preflight COPY, quant_foundry
     source COPY, libgomp1 all present.

3. **Created git branch** `tier0/image-slimming`.

4. **Ran validation:**
   - `pytest runpod/tests/test_dockerfile_slim.py -q` → 23 passed.
   - `pytest runpod/tests/test_dockerfile_no_healthcheck.py -q` → 7 passed
     (regression guard).
   - `python -m compileall runpod/tests/test_dockerfile_slim.py` → no
     errors.

## Key design decisions

- **Dockerfile.minimal was NOT used as the base.** The existing
  `Dockerfile.minimal` is a bare-bones canary handler
  (`handler_minimal.py`) with only `runpod>=1.6,<2` and — critically — a
  **HEALTHCHECK directive** that would break RunPod job dispatch. It does
  not implement the slim training path. The new `Dockerfile.slim` is built
  from the production Dockerfile with torch removed, preserving the full
  dispatch + training path.

- **catboost is kept.** Although catboost GPU (`task_type="GPU"`) requires
  the torch-bundled CUDA runtime and won't work in the slim image, the
  roadmap expects a GPU backend later. catboost CPU training works fine
  without torch. Dropping catboost would be a separate image-choice
  decision, documented here but not actioned.

- **No Docker build was performed.** Docker is not available in the local
  environment. Static validation (Dockerfile text parsing) is the
  acceptance gate. See RISKS.md for details.

## Acceptance criteria status

| Criterion | Status |
|-----------|--------|
| Dockerfile.slim exists with python:3.12-slim base | PASS |
| Dockerfile.slim has no torch | PASS |
| Dockerfile.slim has no HEALTHCHECK | PASS |
| Static validation tests pass (23/23) | PASS |
| test_dockerfile_no_healthcheck.py still passes (7/7) | PASS |
| Size estimate documents expected reduction | PASS (see SIZE_ESTIMATE.md) |
| Receipt bundle written | PASS |
