# Changed Files

## Modified

### `services/quant_foundry/src/quant_foundry/schemas.py`
- Added `presigned_artifact_url: str | None = None` field to
  `RunPodTrainingRequest` (line ~66). The worker's handler.py pops this
  from the job input before validating the remainder as a
  RunPodTrainingRequest. Satisfies the /tmp deny gate.

### `services/quant_foundry/src/quant_foundry/runpod_client.py`
- Added import: `from quant_foundry.runpod_policy import build_job_policy`
- `HttpRunPodClient.dispatch()` (line ~297): changed the `/run` request
  body from `{"input": request_payload}` to
  `{"input": request_payload, "policy": build_job_policy()}`. The
  policy.executionTimeout is in ms and always >= 1860000 (1860s).

## Created

### `services/quant_foundry/src/quant_foundry/runpod_policy.py`
- New service-package module (280 lines) containing:
  - `build_job_policy()` — per-request execution timeout policy (ms).
  - `build_endpoint_input()` + `EndpointConfig` — endpoint template
    builder with network volume support (networkVolumeId, volumeInGb,
    volumeMountPath).
  - `build_training_job_input()` — serializes RunPodTrainingRequest to
    job input dict, ensures presigned_artifact_url is a top-level key.
  - `compute_execution_timeout()` / `validate_execution_timeout()` —
    timeout floor enforcement (>= 1860s).
  - Constants: `DEFAULT_DEADLINE_S`, `DEFAULT_SLACK_S`,
    `MIN_EXECUTION_TIMEOUT_S`, `DEFAULT_IDLE_TIMEOUT_S`,
    `DEFAULT_VOLUME_MOUNT_PATH`.

### `services/quant_foundry/tests/test_runpod_dispatch.py`
- 18 new tests covering:
  - RunPodTrainingRequest accepts presigned_artifact_url (set + None default)
  - extra="forbid" still enforced
  - build_training_job_input includes presigned_artifact_url
  - HttpRunPodClient.dispatch sends presigned_artifact_url in input
  - HttpRunPodClient.dispatch includes policy.executionTimeout >= 1860000 ms
  - /run body shape is {"input": ..., "policy": ...}
  - MockRunPodClient makes no live calls
  - build_job_policy default meets minimum, rejects below-min, ttl→ms
  - Endpoint template includes/omits networkVolumeId
  - Default volume_mount_path is /runpod-volume
  - executionTimeout always present in endpoint input
  - No-live-calls meta guard

## Not changed (by design)
- `runpod/quant-foundry-training/handler.py` — worker code unchanged
  (already reads presigned_artifact_url at line ~3114).
- `scripts/runpod/runpod_lifecycle.py` — probe scripts keep their
  existing import (unchanged).
- Base image — not switched.
- No HEALTHCHECK added.
