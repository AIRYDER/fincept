# Test Results

## All tests run on 2026-07-04

### Regression guards (Python 3.10.6 — no quant_foundry dependency)

| Test file | Result | Notes |
|-----------|--------|-------|
| `runpod/tests/test_dockerfile_no_healthcheck.py` | **7 passed** | Verifies no HEALTHCHECK in production Dockerfile |
| `runpod/tests/test_receipt_integrity.py` | **4 passed** | Verifies receipt bundles don't contradict raw evidence |
| `runpod/tests/test_dockerfile_slim.py` | **23 passed** | Static validation of Dockerfile.slim (base image, no torch, no HEALTHCHECK) |
| `runpod/tests/test_runpod_lifecycle.py` | **46 passed** | 38 original + 8 new for `build_job_policy()` |

### Feature tests (Python 3.12.6 — quant_foundry installed)

| Test file | Result | Notes |
|-----------|--------|-------|
| `services/quant_foundry/tests/test_metric_sanity.py` | **18 passed** | Metric sanity bounds (Sharpe, annual return, max drawdown) |
| `services/quant_foundry/tests/test_artifact_writer.py` | **41 passed** | Artifact writers, /tmp deny gate, output_prefix validation, HMAC receipts |

### Compilation

| Check | Result |
|-------|--------|
| `py_compile handler.py` | OK (Python 3.12) |

### Summary

**139 tests pass, 0 failures, 0 errors.**

### Tests NOT run (blocked)

| Test | Reason |
|------|--------|
| Full `pytest` suite | Python 3.10 can't import quant_foundry (requires >=3.12). Only targeted test files were run on 3.12. |
| Docker build | Docker not installed locally. |
| Live RunPod canary | Not run — requires operator approval for cloud spend. |
