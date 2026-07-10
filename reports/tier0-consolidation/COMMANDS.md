# Commands Run

## Environment verification

```bash
cd "C:/Users/nolan/CascadeProjects/fincept-terminal"
git branch --show-current          # tier0/consolidation
git branch --list "tier0/*"       # 5 tier0 branches + consolidation
python --version                   # Python 3.10.6 (local)
C:\Python312\python.exe --version  # Python 3.12.6 (for tests)
docker --version                   # docker: command not found
ruff check . --statistics          # 357 errors remaining
```

## Branch creation and commits

```bash
git checkout -b tier0/consolidation
git add runpod/quant-foundry-training/Dockerfile.slim runpod/tests/test_dockerfile_slim.py
git add scripts/runpod/ runpod/tests/test_runpod_lifecycle.py scripts/verify_artifact_manifest.py
git add runpod/quant-foundry-training/run_live_canary.py runpod/quant-foundry-training/run_train_model.py runpod/quant-foundry-training/run_gpu_healthcheck.py
git commit -F .git/COMMIT_MSG_tier0_features.txt    # f9816d3b

git add "*.py" "infra/docker/api.Dockerfile"
git commit -F .git/COMMIT_MSG_ruff.txt               # be92a7af

git add reports/tier0-* reports/tier0-integration-review docs/runpod-fix-plan/RECEIPT_INDEX.md
git commit -m "docs(tier0): swarm receipts..."       # 51781bc5

# B5 fix: move /tmp deny gate before training
git add runpod/quant-foundry-training/handler.py
git commit -m "tier0: move /tmp deny gate..."        # f4881809

# B4 fix: executionTimeout per-request policy
git add scripts/runpod/runpod_lifecycle.py runpod/quant-foundry-training/run_live_canary.py runpod/tests/test_runpod_lifecycle.py
git commit -m "tier0: fix executionTimeout..."       # 5700e51c
```

## Regression guards (Python 3.10 — no quant_foundry dependency)

```bash
python -m pytest runpod/tests/test_dockerfile_no_healthcheck.py -q   # 7 passed
python -m pytest runpod/tests/test_receipt_integrity.py -q           # 4 passed
python -m pytest runpod/tests/test_dockerfile_slim.py -q             # 23 passed
python -m pytest runpod/tests/test_runpod_lifecycle.py -q            # 46 passed
```

## Full test suite (Python 3.12 — quant_foundry installed)

```bash
C:\Python312\python.exe -m pip install -e services/quant_foundry
C:\Python312\python.exe -m pytest services/quant_foundry/tests/test_metric_sanity.py -q    # 18 passed
C:\Python312\python.exe -m pytest services/quant_foundry/tests/test_artifact_writer.py -q  # 41 passed
```

## Handler compilation check

```bash
C:\Python312\python.exe -m py_compile runpod/quant-foundry-training/handler.py  # OK
```

## Ruff debt check

```bash
ruff check . --statistics    # 357 errors, 2 fixable, 157 unsafe-fixable
```

## Docker build (BLOCKED — Docker not available)

```bash
# Cannot run — docker command not found
# docker build -f runpod/quant-foundry-training/Dockerfile.slim runpod/quant-foundry-training
```
