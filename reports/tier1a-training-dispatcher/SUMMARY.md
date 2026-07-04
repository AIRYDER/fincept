# Tier 1A — Training Dispatcher Wiring (Builder 2)

## Task
Wire the RunPod training dispatch path to pass presigned URLs, mount
network volumes, and include the per-request execution timeout policy.

## What was built

1. **`presigned_artifact_url` field on `RunPodTrainingRequest`** — Added
   the optional `presigned_artifact_url: str | None = None` field to the
   cross-boundary request schema in `schemas.py`. The worker's
   `handler.py` already pops this field from the job input (line ~3114)
   and uses `PresignedUploadArtifactWriter` to upload the model artifact
   via HTTP PUT when present. This satisfies the `/tmp` deny gate that
   requires a durable `output_prefix` or a presigned URL.

2. **`runpod_policy.py` service module** — Created a new
   `services/quant_foundry/src/quant_foundry/runpod_policy.py` module
   containing:
   - `build_job_policy()` — per-request `policy` dict with
     `executionTimeout` in milliseconds (>= 1860000), the documented
     RunPod per-job timeout override.
   - `build_endpoint_input()` + `EndpointConfig` — endpoint template
     builder with network volume support (`networkVolumeId`,
     `volumeInGb`, `volumeMountPath`).
   - `build_training_job_input()` — helper that serializes a
     `RunPodTrainingRequest` to a JSON-safe job input dict and ensures
     `presigned_artifact_url` is present as a top-level key.
   - `compute_execution_timeout()` / `validate_execution_timeout()` —
     timeout floor enforcement (>= 1860s).

   The probe scripts in `scripts/runpod/runpod_lifecycle.py` keep their
   existing import (unchanged); the service package imports from the new
   module to avoid a dependency on the non-installed `scripts` package.

3. **`build_job_policy()` wired into dispatch** —
   `HttpRunPodClient.dispatch()` now sends the body
   `{"input": request_payload, "policy": build_job_policy()}` instead of
   just `{"input": request_payload}`. This ensures RunPod never inherits
   its 600s default job timeout — the per-request policy is the
   reliable, documented override path.

4. **Volume mounting in endpoint template** — `EndpointConfig` accepts
   `network_volume_id`, `volume_in_gb`, and `volume_mount_path` (default
   `/runpod-volume`). When `network_volume_id` is set,
   `build_endpoint_input()` includes `networkVolumeId`, `volumeInGb`,
   and `volumeMountPath` in the endpoint input dict.

5. **Tests** — 18 new tests in `test_runpod_dispatch.py` covering all
   acceptance criteria with mocked RunPod clients (httpx MockTransport +
   MockRunPodClient). No live RunPod API calls.

## Acceptance status
- [x] RunPodTrainingRequest includes presigned_artifact_url field
- [x] Dispatch path passes presigned_artifact_url to worker
- [x] build_job_policy() called in dispatch path
- [x] Endpoint template supports volume mounting
- [x] All tests pass with mocked client (no live calls)
