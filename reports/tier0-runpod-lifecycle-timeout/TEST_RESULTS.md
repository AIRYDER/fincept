# Test Results

## Unit tests: runpod/tests/test_runpod_lifecycle.py

```
============================= test session starts =============================
platform win32 -- Python 3.10.6, pytest-7.4.4, pluggy-1.6.0
cachedir: .pytest_cache
rootdir: C:\Users\nolan\CascadeProjects\fincept-terminal
configfile: pyproject.toml
plugins: anyio-4.11.0, Faker-40.28.1, asyncio-0.23.4, cov-4.1.0, mock-3.14.1

collected 38 items

runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_basic_name_has_timestamp_and_sha PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_name_with_suffix PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_sha_truncated_to_sha_len PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_default_timestamp_is_current_time PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_two_calls_with_same_args_differ_when_timestamp_changes PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_different_prefixes_dont_collide PASSED
runpod/tests/test_runpod_lifecycle.py::TestMakeUniqueName::test_empty_suffix_omitted PASSED
runpod/tests/test_runpod_lifecycle.py::TestComputeExecutionTimeout::test_default_is_deadline_plus_slack PASSED
runpod/tests/test_runpod_lifecycle.py::TestComputeExecutionTimeout::test_custom_deadline_and_slack PASSED
runpod/tests/test_runpod_lifecycle.py::TestComputeExecutionTimeout::test_floor_enforced_when_below_minimum PASSED
runpod/tests/test_runpod_lifecycle.py::TestValidateExecutionTimeout::test_valid_timeout_passes PASSED
runpod/tests/test_runpod_lifecycle.py::TestValidateExecutionTimeout::test_below_minimum_raises PASSED
runpod/tests/test_runpod_lifecycle.py::TestValidateExecutionTimeout::test_below_minimum_boundary PASSED
runpod/tests/test_runpod_lifecycle.py::TestValidateExecutionTimeout::test_exact_minimum_passes PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_includes_execution_timeout PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_default_execution_timeout_is_1860 PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_custom_execution_timeout_accepted PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_below_minimum_execution_timeout_raises PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_all_fields_present PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildEndpointInput::test_idle_timeout_configurable PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildTemplateInput::test_basic_template_input PASSED
runpod/tests/test_runpod_lifecycle.py::TestBuildTemplateInput::test_custom_disk_sizes PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_success_on_first_attempt PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_retries_on_failure_then_succeeds PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_all_attempts_fail PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_no_sleep_after_last_attempt PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_logger_called_on_failure PASSED
runpod/tests/test_runpod_lifecycle.py::TestRetryDeleteEndpoint::test_logger_called_on_success PASSED
runpod/tests/test_runpod_lifecycle.py::TestSafeScaleToZero::test_success PASSED
runpod/tests/test_runpod_lifecycle.py::TestSafeScaleToZero::test_failure_returns_false_not_raise PASSED
runpod/tests/test_runpod_lifecycle.py::TestSafeScaleToZero::test_logger_called_on_failure PASSED
runpod/tests/test_runpod_lifecycle.py::TestSafeScaleToZero::test_logger_called_on_success PASSED
runpod/tests/test_runpod_lifecycle.py::TestFormatTimeoutReceipt::test_basic_fields PASSED
runpod/tests/test_runpod_lifecycle.py::TestFormatTimeoutReceipt::test_meets_min_requirement_false_when_below PASSED
runpod/tests/test_runpod_lifecycle.py::TestFormatTimeoutReceipt::test_note_is_present PASSED
runpod/tests/test_runpod_lifecycle.py::TestFormatTimeoutReceipt::test_custom_deadline PASSED
runpod/tests/test_runpod_lifecycle.py::TestEndpointConfigIntegration::test_full_config_produces_valid_input PASSED
runpod/tests/test_runpod_lifecycle.py::TestEndpointConfigIntegration::test_default_config_meets_min_timeout PASSED

============================= 38 passed in 0.24s ==============================
```

## Regression: test_dockerfile_no_healthcheck.py

```
7 passed
```

## Regression: test_receipt_integrity.py

```
4 passed
```

## compileall

```
Compiling 'scripts/runpod/runpod_lifecycle.py'...
Compiling 'scripts/runpod/__init__.py'...
Compiling 'runpod/quant-foundry-training/run_live_canary.py'...
Compiling 'runpod/quant-foundry-training/run_train_model.py'...
Compiling 'runpod/quant-foundry-training/run_gpu_healthcheck.py'...
Compiling 'runpod/tests/test_runpod_lifecycle.py'...
Exit code: 0
```

## Summary

| Suite | Tests | Passed | Failed |
|-------|-------|--------|--------|
| test_runpod_lifecycle.py | 38 | 38 | 0 |
| test_dockerfile_no_healthcheck.py | 7 | 7 | 0 |
| test_receipt_integrity.py | 4 | 4 | 0 |
| compileall | 6 files | 6 | 0 |
| **Total** | **55** | **55** | **0** |
