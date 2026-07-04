# Merge Log

## Branch topology

```
fix/test-harness-optional-deps-guards (base, 416 commits ahead of main)
  └── tier0/consolidation (6 commits ahead of base)
        ├── b89dd4c5  metric sanity bounds
        ├── f9816d3b  image slimming + lifecycle + verifier
        ├── be92a7af  ruff burn-down pass 1
        ├── 51781bc5  swarm receipts
        ├── f4881809  B5 fix: deny gate before training
        └── 5700e51c  B4 fix: executionTimeout per-request policy
```

## Commits (chronological)

### 1. `b89dd4c5` — tier0: metric sanity bounds
- **Originally:** `tier0/metric-sanity` branch (Builder 5)
- **Changes:** `validate_metric_sanity()` in `runpod_training.py`, env-tunable thresholds, `MetricSanityReport` dataclass, wired into `build_callback()`.
- **Tests:** 18 new tests in `test_metric_sanity.py`, 67 existing tests pass.

### 2. `f9816d3b` — tier0: image slimming + lifecycle helper + artifact verifier
- **Originally:** Uncommitted working tree (Builders 1, 2, 3)
- **Changes:**
  - `Dockerfile.slim` — python:3.12-slim base, drops torch (~2GB)
  - `scripts/runpod/runpod_lifecycle.py` — shared lifecycle helper, `MIN_EXECUTION_TIMEOUT_S=1860`
  - `scripts/verify_artifact_manifest.py` — standalone manifest verifier
  - All three probe scripts updated to use lifecycle helper
- **Tests:** 23 slim Dockerfile tests, 38 lifecycle tests.

### 3. `be92a7af` — tier0: ruff burn-down pass 1
- **Originally:** Uncommitted working tree (Builder 4)
- **Changes:** `ruff check --fix` + `ruff format .` across 287 files. 965 errors fixed. 1 UP017 fix reverted (Python 3.10 compat).
- **Tests:** compileall passed on all changed files.

### 4. `51781bc5` — docs(tier0): swarm receipts
- **Changes:** All receipt files from `reports/tier0-*` and `reports/tier0-integration-review/`.

### 5. `f4881809` — tier0: move /tmp deny gate before training starts (B5 fix)
- **Changes:** Moved `_validate_output_prefix_durable()` call from after training (line ~3519) to before training (line ~3352). Stage name changed to `artifact_destination_deny_gate_pre_training`.
- **Tests:** All 41 artifact writer tests pass.

### 6. `5700e51c` — tier0: fix executionTimeout — add per-request policy (B4 fix)
- **Changes:**
  - Added `build_job_policy()` to `runpod_lifecycle.py` — returns `{"executionTimeout": <ms>, "lowPriority": false, "ttl"?: <ms>}`
  - Updated `run_job()` in `run_live_canary.py` to include `policy` in request body
  - `run_train_model.py` and `run_gpu_healthcheck.py` import `run_job` from `run_live_canary`, so they get the fix automatically
  - Kept `executionTimeout` in `build_endpoint_input()` as best-effort (undocumented endpoint-level field)
- **Tests:** 8 new tests for `build_job_policy()` (46 total lifecycle tests pass).

## Merge target

The consolidation branch is based on `fix/test-harness-optional-deps-guards` (not `main`) because `fix/test-harness-optional-deps-guards` is 416 commits ahead of `main` and contains the RunPod investigation work that Tier 0 depends on.

To merge into `main`:
```bash
git checkout main
git merge fix/test-harness-optional-deps-guards --no-ff
git merge tier0/consolidation --no-ff
```

Or merge `tier0/consolidation` directly into `fix/test-harness-optional-deps-guards` first, then merge that into `main`.
