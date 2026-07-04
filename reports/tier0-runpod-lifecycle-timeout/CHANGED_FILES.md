# Changed Files

## New files

| File | Purpose |
|------|---------|
| `scripts/runpod/__init__.py` | Package init for scripts/runpod |
| `scripts/runpod/runpod_lifecycle.py` | Shared RunPod endpoint/template lifecycle helper (353 lines) |
| `runpod/tests/test_runpod_lifecycle.py` | Unit tests for the lifecycle helper (38 tests) |

## Modified files

| File | Changes |
|------|---------|
| `runpod/quant-foundry-training/run_live_canary.py` | Added `EXECUTION_TIMEOUT` constant (1860s), imported lifecycle helpers, `save_template` uses `build_template_input`, `create_endpoint` uses `build_endpoint_input` with `executionTimeout`, template/endpoint names use `make_unique_name`, endpoint receipt includes `executionTimeout` + `timeout_config`, cleanup uses `retry_delete_endpoint` + `safe_scale_to_zero` |
| `runpod/quant-foundry-training/run_train_model.py` | Imported `EXECUTION_TIMEOUT` + lifecycle helpers, template/endpoint names use `make_unique_name`, endpoint receipt includes `executionTimeout` + `timeout_config`, cleanup uses `retry_delete_endpoint` + `safe_scale_to_zero` |
| `runpod/quant-foundry-training/run_gpu_healthcheck.py` | Imported `EXECUTION_TIMEOUT` + lifecycle helpers, template/endpoint names use `make_unique_name`, endpoint receipt includes `executionTimeout` + `timeout_config`, cleanup uses `retry_delete_endpoint` + `safe_scale_to_zero` |
