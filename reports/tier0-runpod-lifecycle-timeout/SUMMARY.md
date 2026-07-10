# Tier 0 — RunPod Lifecycle & Timeout Helper

**Task ID:** task-mr6i5c9k-fd637970
**Branch:** `tier0/runpod-lifecycle-timeout`
**Agent:** Builder 1
**Date:** 2026-07-04

## What was done

1. **Set `executionTimeout >= 1860s`** on all RunPod endpoint creation code.
   The handler enforces `QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS=1800` (30 min).
   RunPod's default endpoint job timeout is 600s. Without an explicit
   `executionTimeout`, a 20-minute training job would be `TIMED_OUT` by RunPod
   *before* the handler's signed failure envelope fires — the platform loses
   the signed receipt. The fix sets `executionTimeout=1860` (deadline + 60s
   slack) on every endpoint creation.

2. **Extracted shared lifecycle logic** into `scripts/runpod/runpod_lifecycle.py`:
   - Unique template/endpoint naming (timestamp + SHA suffix)
   - Retry cleanup (`retry_delete_endpoint` with configurable attempts/delay)
   - Safe scale-to-zero helper
   - Timeout configuration (`compute_execution_timeout`, `validate_execution_timeout`)
   - Receipt-friendly logging (`format_timeout_receipt`)
   - Template/endpoint input builders (`build_template_input`, `build_endpoint_input`)

3. **Updated all three RunPod probe tools** to import and use the shared helper:
   - `run_live_canary.py` — added `EXECUTION_TIMEOUT`, uses helper for naming/cleanup
   - `run_train_model.py` — imports `EXECUTION_TIMEOUT`, uses helper for naming/cleanup
   - `run_gpu_healthcheck.py` — imports `EXECUTION_TIMEOUT`, uses helper for naming/cleanup

4. **Added 38 unit tests** for the lifecycle helper (all pass, all mocked — no
   real HTTP calls).

## Hard constraints respected

- No base image switch (still `python:3.12-slim`)
- No Docker HEALTHCHECK reintroduced
- No live/paid RunPod tests run
- No files outside owned list touched
- No Tier 1+ work started

## Acceptance criteria met

- [x] `executionTimeout >= 1860` set in endpoint template creation code
- [x] Shared `runpod_lifecycle.py` helper with: unique naming, retry cleanup, scale-to-zero, timeout config
- [x] All three RunPod scripts use the helper
- [x] Unit tests pass (38/38)
- [x] compileall passes on all changed files
- [x] Regression guards pass (test_dockerfile_no_healthcheck, test_receipt_integrity)
- [x] No base image switch, no HEALTHCHECK, no live tests
- [x] Receipt bundle written
