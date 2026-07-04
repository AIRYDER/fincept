# Non-Regression Checklist

| # | Rule | Status | Evidence |
|---|------|--------|----------|
| 1 | No nvidia/cuda base switch | PASS | Both Dockerfiles use `python:3.12-slim`; nvidia/cuda only in comments |
| 2 | No runpod/base switch | PASS | runpod/base only in comments |
| 3 | No pytorch/pytorch base switch | PASS | pytorch/pytorch only in comments |
| 4 | No Docker HEALTHCHECK | PASS | `test_dockerfile_no_healthcheck.py` → 7 passed |
| 5 | No production artifact final destination only in /tmp | PASS | `/tmp` deny gate moved BEFORE training (B5 fix). `test_artifact_writer.py` → 41 passed |
| 6 | Endpoint timeout >= 1860s | PASS | `MIN_EXECUTION_TIMEOUT_S=1860` in lifecycle helper + `build_job_policy()` sends per-request `executionTimeout` in ms (B4 fix). `test_runpod_lifecycle.py` → 46 passed |
| 7 | Metric sanity test exists | PASS | `test_metric_sanity.py` → 18 passed |
| 8 | Lifecycle helper exists | PASS | `scripts/runpod/runpod_lifecycle.py` with 46 tests |
| 9 | No live RunPod resources created | PASS | No live tests run; no RUNPOD_API_KEY used |
| 10 | Receipt integrity guard passes | PASS | `test_receipt_integrity.py` → 4 passed |
| 11 | Slim Dockerfile guard passes | PASS | `test_dockerfile_slim.py` → 23 passed |
| 12 | Handler compiles on Python 3.12 | PASS | `py_compile handler.py` → OK |
