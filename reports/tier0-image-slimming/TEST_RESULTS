# Test Results

## Test run 1: Slim Dockerfile static validation

**Command:**
```
python -m pytest runpod/tests/test_dockerfile_slim.py -q
```

**Result:**
```
23 passed in 3.87s
```

**Tests (23 total):**

| # | Test name | Status |
|---|-----------|--------|
| 1 | test_slim_uses_python_312_slim_base | PASS |
| 2 | test_slim_does_not_use_forbidden_base | PASS |
| 3 | test_slim_has_no_import_based_healthcheck | PASS |
| 4 | test_slim_has_no_healthcheck_directive_at_all | PASS |
| 5 | test_slim_does_not_install_torch | PASS |
| 6 | test_slim_does_not_reference_pytorch_index_url | PASS |
| 7 | test_slim_installs_required_package[lightgbm] | PASS |
| 8 | test_slim_installs_required_package[xgboost] | PASS |
| 9 | test_slim_installs_required_package[runpod] | PASS |
| 10 | test_slim_installs_supporting_package[catboost] | PASS |
| 11 | test_slim_installs_supporting_package[pandas] | PASS |
| 12 | test_slim_installs_supporting_package[pyarrow] | PASS |
| 13 | test_slim_installs_supporting_package[scikit-learn] | PASS |
| 14 | test_slim_installs_supporting_package[numpy] | PASS |
| 15 | test_slim_installs_supporting_package[pydantic] | PASS |
| 16 | test_slim_installs_supporting_package[pydantic-settings] | PASS |
| 17 | test_slim_installs_supporting_package[httpx] | PASS |
| 18 | test_slim_entrypoint_matches_production | PASS |
| 19 | test_slim_has_git_sha_arg | PASS |
| 20 | test_slim_has_non_root_user | PASS |
| 21 | test_slim_copies_handler_and_preflight | PASS |
| 22 | test_slim_copies_quant_foundry_source | PASS |
| 23 | test_slim_installs_libgomp1 | PASS |

## Test run 2: Regression guard (production Dockerfile HEALTHCHECK)

**Command:**
```
python -m pytest runpod/tests/test_dockerfile_no_healthcheck.py -q
```

**Result:**
```
7 passed in 3.11s
```

## Test run 3: Bytecode compilation

**Command:**
```
python -m compileall runpod/tests/test_dockerfile_slim.py -q
```

**Result:**
```
(no output — exit code 0 — no syntax/compilation errors)
```
