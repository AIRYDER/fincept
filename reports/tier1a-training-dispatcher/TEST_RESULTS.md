# Test Results

## New tests — `tests/test_runpod_dispatch.py` (18 passed)

```
tests/test_runpod_dispatch.py::test_request_accepts_presigned_artifact_url PASSED
tests/test_runpod_dispatch.py::test_request_presigned_artifact_url_defaults_none PASSED
tests/test_runpod_dispatch.py::test_request_rejects_extra_fields PASSED
tests/test_runpod_dispatch.py::test_build_training_job_input_includes_presigned_url PASSED
tests/test_runpod_dispatch.py::test_build_training_job_input_includes_none_presigned PASSED
tests/test_runpod_dispatch.py::test_build_training_job_input_merges_extra_fields PASSED
tests/test_runpod_dispatch.py::test_dispatch_includes_presigned_url_in_input PASSED
tests/test_runpod_dispatch.py::test_dispatch_includes_policy_execution_timeout_ms PASSED
tests/test_runpod_dispatch.py::test_dispatch_body_shape_is_input_plus_policy PASSED
tests/test_runpod_dispatch.py::test_dispatch_no_live_calls_mock_client PASSED
tests/test_runpod_dispatch.py::test_build_job_policy_default_meets_minimum PASSED
tests/test_runpod_dispatch.py::test_build_job_policy_rejects_below_minimum PASSED
tests/test_runpod_dispatch.py::test_build_job_policy_ttl_converted_to_ms PASSED
tests/test_runpod_dispatch.py::test_endpoint_template_includes_network_volume_id PASSED
tests/test_runpod_dispatch.py::test_endpoint_template_omits_network_volume_id_when_unset PASSED
tests/test_runpod_dispatch.py::test_endpoint_template_default_volume_mount_path PASSED
tests/test_runpod_dispatch.py::test_endpoint_template_execution_timeout_always_present PASSED
tests/test_runpod_dispatch.py::test_no_live_runpod_api_calls_in_test_suite PASSED

============================= 18 passed in 1.27s ==============================
```

## Regression — `test_runpod_client.py` + `test_schemas.py` (37 passed)

```
37 items — all PASSED
```

## Regression sweep — gateway + connection + shadow + artifact_writer (93 passed)

```
........................................................................ [ 77%]
.....................                                                    [100%]
93 passed
```

## Acceptance criteria verification

| Criterion | Test | Status |
|-----------|------|--------|
| RunPodTrainingRequest accepts presigned_artifact_url | test_request_accepts_presigned_artifact_url | PASS |
| Dispatch includes presigned_artifact_url in job input | test_dispatch_includes_presigned_url_in_input | PASS |
| Dispatch includes policy.executionTimeout in ms (>= 1860000) | test_dispatch_includes_policy_execution_timeout_ms | PASS |
| Endpoint template includes networkVolumeId when volume configured | test_endpoint_template_includes_network_volume_id | PASS |
| No live RunPod API calls made in tests | test_dispatch_no_live_calls_mock_client + test_no_live_runpod_api_calls_in_test_suite | PASS |

## No-live-calls guarantee
All HTTP tests use `httpx.MockTransport` (intercepts requests before any
network I/O). The `MockRunPodClient` is in-process and makes no HTTP
calls at all. No test constructs an `HttpRunPodClient` without a mock
transport.
