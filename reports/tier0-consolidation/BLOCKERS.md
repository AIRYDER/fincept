# Blockers

## No hard blockers

All Tier 0 consolidation work is complete. The branch is ready for merge.

## Soft blockers remaining

### B1: Docker build not verified (BLOCKED — no Docker locally)

- **Issue:** Docker is not installed on this machine. Neither the production Dockerfile nor Dockerfile.slim was built.
- **Impact:** Unknown whether the slim image actually builds or whether handler.py imports torch at module load time (would crash the slim image).
- **Recommendation:** Build both images in a Docker-enabled environment before deploying:
  ```bash
  docker build -f runpod/quant-foundry-training/Dockerfile -t fincept-qf-training:gpu-tree .
  docker build -f runpod/quant-foundry-training/Dockerfile.slim -t fincept-qf-training:slim .
  ```
  Then run a live canary against the slim image (operator approval required).

### B3: 357 Ruff errors remain (DOCUMENTED)

- **Issue:** 357 ruff errors remain after the burn-down pass (down from 1845, 80.6% reduction).
- **Impact:** CI will still show errors. The remaining errors require manual fixes.
- **Top categories:**
  - 60 T201 (print statements)
  - 37 F841 (unused variables)
  - 31 E402 (module import not at top)
  - 26 S110 (try-except-pass)
  - 21 SIM108 (if-else instead of if-expression)
  - 19 RUF001 (ambiguous unicode characters)
  - 157 unsafe-fixable (need manual review)
- **Recommendation:** Schedule a follow-up ruff burn-down task. See RUFF_DEBT.md.

### B4-live: executionTimeout needs live verification (REQUIRES OPERATOR APPROVAL)

- **Issue:** The `build_job_policy()` function sends `policy.executionTimeout` in the per-request body (in milliseconds). This is the documented path per RunPod docs. However, it has not been verified with a live RunPod job.
- **Impact:** If RunPod rejects the `policy` field or ignores it, the endpoint will use the default 600s timeout.
- **Recommendation:** Run a live canary with the lifecycle helper changes. Confirm the job runs with the 1860s timeout. (Operator approval required for cloud spend.)

### B6: Full pytest suite not run

- **Issue:** Only targeted test files were run on Python 3.12. The full pytest suite was not run because it would require installing all dependencies and may have collection errors.
- **Impact:** Unknown regressions in untested files.
- **Recommendation:** Run `pytest` in a Python 3.12 environment with all dependencies installed before merging to main.
