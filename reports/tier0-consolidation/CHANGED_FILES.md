# Changed Files

## vs. base branch (`fix/test-harness-optional-deps-guards`)

**430 files changed, 104,615 insertions(+), 7,089 deletions(-)**

### New files (code)

| File | Purpose |
|------|---------|
| `runpod/quant-foundry-training/Dockerfile.slim` | Slim training image (drops torch) |
| `runpod/tests/test_dockerfile_slim.py` | Static validation for slim Dockerfile (23 tests) |
| `runpod/tests/test_runpod_lifecycle.py` | Lifecycle helper tests (46 tests) |
| `scripts/runpod/__init__.py` | Package init for runpod lifecycle helpers |
| `scripts/runpod/runpod_lifecycle.py` | Shared RunPod endpoint/template lifecycle helper |
| `scripts/verify_artifact_manifest.py` | Standalone artifact manifest verifier |

### Modified files (code — key files only)

| File | Changes |
|------|---------|
| `runpod/quant-foundry-training/handler.py` | /tmp deny gate (moved before training), metric sanity wiring, ruff format |
| `runpod/quant-foundry-training/run_live_canary.py` | Lifecycle helper imports, `build_job_policy()` in `run_job()` |
| `runpod/quant-foundry-training/run_train_model.py` | Lifecycle helper imports |
| `runpod/quant-foundry-training/run_gpu_healthcheck.py` | Lifecycle helper imports |
| `services/quant_foundry/src/quant_foundry/runpod_training.py` | `validate_metric_sanity()`, `MetricSanityReport`, env-tunable thresholds |
| `services/quant_foundry/tests/test_artifact_writer.py` | 17 new tests for /tmp deny gate + output_prefix validation |
| `services/quant_foundry/tests/test_metric_sanity.py` | 18 new tests for metric sanity bounds |

### Reformatted files (ruff format)

287 `.py` files were reformatted by `ruff format .`. These are whitespace-only changes (line length, trailing commas, import sorting) and do not change AST.

### New files (receipts)

All files under:
- `reports/tier0-durable-artifacts/`
- `reports/tier0-runpod-lifecycle-timeout/`
- `reports/tier0-image-slimming/`
- `reports/tier0-metric-sanity/`
- `reports/tier0-ci-ruff-burndown/`
- `reports/tier0-integration-review/`
- `reports/tier0-consolidation/` (this receipt)
