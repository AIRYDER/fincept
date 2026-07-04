# Tier 0 Swarm — Current Test Commands

## What Works Locally (Python 3.10.6)

**CRITICAL:** Local Python is 3.10.6 but the project requires >=3.12. Most pytest
runs will fail on import of 3.12-only syntax. The following commands work for
static validation:

### Ruff (works)
```bash
ruff check .                    # lint check (1823 errors currently)
ruff check . --fix              # auto-fix safe issues
ruff format .                   # format
```

### Compile-all (works for syntax validation)
```bash
python -m compileall runpod/quant-foundry-training/handler.py
python -m compileall services/quant_foundry/src/quant_foundry/
```

### Receipt Integrity Guard (may work — check imports)
```bash
pytest runpod/tests/test_receipt_integrity.py -q
```

### HEALTHCHECK Guard (may work — check imports)
```bash
pytest runpod/tests/test_dockerfile_no_healthcheck.py -q
```

## What Requires Python 3.12+ (will fail locally)

```bash
pytest services/quant_foundry/tests/test_artifact_writer.py -q   # imports handler.py → 3.12 syntax
pytest services/quant_foundry/tests/ -k "artifact or writer or manifest" -q
python runpod/quant-foundry-training/run_train_model.py --local  # imports handler → 3.12 syntax
```

**These must be validated via `python -m compileall` + `ruff check` instead, or in a 3.12 environment.**

## What Requires Docker (NOT available locally)

```bash
docker build -t fincept-qf-training:gpu-tree -f runpod/quant-foundry-training/Dockerfile .
```

**Image-slimming worker must do static validation only and document why Docker build isn't possible.**

## What Requires Live RunPod (needs operator approval)

```bash
python runpod/quant-foundry-training/run_live_canary.py --sha <full-sha>
python runpod/quant-foundry-training/run_gpu_healthcheck.py --sha <full-sha>
python runpod/quant-foundry-training/run_train_model.py --sha <full-sha>
```

**Do NOT run these without explicit operator approval.**

## Recommended Validation Strategy for Workers

1. **Static syntax:** `python -m compileall <changed_files>`
2. **Lint:** `ruff check <changed_files>`
3. **Targeted pytest:** Try `pytest <test_file> -q` — if it fails on import, fall back to compileall
4. **Full pytest:** `pytest` — will likely fail on 3.12 imports; document the blocker
5. **Docker build:** Not possible; document why
