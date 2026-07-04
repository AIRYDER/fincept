# Non-Regression Checklist

| # | Rule | Status | Evidence |
|---|------|--------|----------|
| 1 | No nvidia/cuda base switch | PASS | `Select-String -Path Dockerfile,Dockerfile.slim -Pattern "FROM python:3.12-slim"` — both match; nvidia/cuda only in comments |
| 2 | No runpod/base switch | PASS | Same check — runpod/base only in comments |
| 3 | No Docker HEALTHCHECK | PASS | `pytest test_dockerfile_no_healthcheck.py` → 7 passed |
| 4 | No production artifact final destination only in /tmp | PASS | `_validate_output_prefix_durable` + `artifact_destination_not_durable` error code in handler.py |
| 5 | Endpoint timeout >= 1860s | PASS | `MIN_EXECUTION_TIMEOUT_S = 1860` in runpod_lifecycle.py; `EXECUTION_TIMEOUT = compute_execution_timeout()` in all 3 probe scripts |
| 6 | Metric sanity test exists | PASS | `test_metric_sanity.py` — 18 tests, all pass |
| 7 | Lifecycle helper exists | PASS | `scripts/runpod/runpod_lifecycle.py` with 38 unit tests |
| 8 | No live RunPod resources created | PASS | No live tests run; no RUNPOD_API_KEY used |
| 9 | Receipt integrity guard passes | PASS | `pytest test_receipt_integrity.py` → 4 passed |
| 10 | Slim Dockerfile guard passes | PASS | `pytest test_dockerfile_slim.py` → 23 passed |
