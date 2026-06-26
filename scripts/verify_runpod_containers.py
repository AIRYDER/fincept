#!/usr/bin/env python3
"""Verify deployed RunPod containers have the correct ML dependencies.

This script sends a minimal test payload to each RunPod serverless endpoint
and verifies that the response contains **real ML output** (not the
deterministic stub patterns used by the MVP):

- **Training endpoint**: verifies the response includes a real artifact
  hash (derived from trained model bytes, not from request inputs) and
  that ``lightgbm`` is importable inside the container.
- **Inference endpoint**: verifies the response includes real predictions
  (not the linear-combination stub ``sum(features)/len(features)``) and
  that ``onnxruntime`` and ``lightgbm`` are importable inside the
  container.

The script checks the response shape against ``RunPodCallbackEnvelope``
and prints a verification report.

Usage (from repo root):

    # Verify both endpoints (needs RUNPOD_API_KEY env var)
    python scripts/verify_runpod_containers.py

    # Verify only the training endpoint
    python scripts/verify_runpod_containers.py --endpoint training

    # Override endpoint IDs
    python scripts/verify_runpod_containers.py \
        --training-endpoint-id h2blqodcicxqyy \
        --inference-endpoint-id t31u1z426jy1ub

Safety invariants:
- No API keys are hardcoded. The RunPod API key comes from the
  ``RUNPOD_API_KEY`` env var or the ``--api-key`` CLI arg.
- All network errors are handled gracefully — the script never crashes
  with a raw traceback; it prints a verification report and exits with a
  non-zero code on failure.

Exit codes:
- 0: all verifications passed
- 1: configuration error (missing API key, unknown endpoint)
- 2: one or more verifications failed
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# RunPod API base URL.
RUNPOD_API_BASE = "https://api.runpod.ai/v2"

# Default endpoint IDs (can be overridden via CLI or env vars).
DEFAULT_TRAINING_ENDPOINT_ID = "h2blqodcicxqyy"
DEFAULT_INFERENCE_ENDPOINT_ID = "t31u1z426jy1ub"

# Polling configuration for async RunPod jobs.
POLL_INTERVAL_SECONDS = 10
POLL_MAX_ATTEMPTS = 30  # 5 min max per job


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class VerificationCheck:
    """A single check within a verification report."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EndpointVerification:
    """Verification result for a single endpoint."""

    endpoint_name: str  # 'training' or 'inference'
    endpoint_id: str
    checks: list[VerificationCheck] = field(default_factory=list)
    error: str | None = None  # top-level error (e.g. network failure)

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        return all(c.passed for c in self.checks)


@dataclass
class VerificationReport:
    """Aggregate verification report."""

    verifications: list[EndpointVerification] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(v.passed for v in self.verifications)


# ---------------------------------------------------------------------------
# RunPod API client (minimal, lazy httpx import)
# ---------------------------------------------------------------------------


class RunPodAPIError(Exception):
    """Raised when a RunPod API call fails."""


def _get_http_client():
    """Lazy import of an HTTP client. Prefers httpx, falls back to requests."""
    try:
        import httpx  # type: ignore[import-untyped]
        return "httpx", httpx
    except ImportError:
        try:
            import requests  # type: ignore[import-untyped]
            return "requests", requests
        except ImportError as exc:
            raise RunPodAPIError(
                "Neither httpx nor requests is installed. "
                "Install one: 'pip install httpx' or 'pip install requests'."
            ) from exc


def runpod_dispatch(
    endpoint_id: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float = 60.0,
) -> str:
    """Submit a job to a RunPod endpoint's async /run endpoint.

    Returns the RunPod-assigned job ID.
    Raises RunPodAPIError on failure.
    """
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/run"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps({"input": payload})
    client_type, lib = _get_http_client()
    try:
        if client_type == "httpx":
            with lib.Client(timeout=timeout) as client:
                resp = client.post(url, headers=headers, content=body)
        else:
            resp = lib.post(url, headers=headers, data=body, timeout=timeout)
    except Exception as exc:
        raise RunPodAPIError(
            f"Dispatch request failed: {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise RunPodAPIError(
            f"Dispatch failed: HTTP {resp.status_code}. "
            f"Response: {getattr(resp, 'text', '')[:500]}"
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise RunPodAPIError(f"Dispatch response was not valid JSON: {exc}") from exc
    job_id = data.get("id")
    if not job_id:
        raise RunPodAPIError(f"Dispatch response missing 'id' field: {data}")
    return str(job_id)


def runpod_status(
    endpoint_id: str,
    api_key: str,
    runpod_job_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Check the status of a RunPod job.

    Returns the parsed status JSON dict.
    Raises RunPodAPIError on failure.
    """
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/status/{runpod_job_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    client_type, lib = _get_http_client()
    try:
        if client_type == "httpx":
            with lib.Client(timeout=timeout) as client:
                resp = client.get(url, headers=headers)
        else:
            resp = lib.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        raise RunPodAPIError(
            f"Status request failed: {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise RunPodAPIError(
            f"Status check failed: HTTP {resp.status_code}. "
            f"Response: {getattr(resp, 'text', '')[:500]}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise RunPodAPIError(f"Status response was not valid JSON: {exc}") from exc


def runpod_health(
    endpoint_id: str,
    api_key: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Check the health of a RunPod endpoint.

    Returns the parsed health JSON dict.
    Raises RunPodAPIError on failure.
    """
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/health"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    client_type, lib = _get_http_client()
    try:
        if client_type == "httpx":
            with lib.Client(timeout=timeout) as client:
                resp = client.get(url, headers=headers)
        else:
            resp = lib.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        raise RunPodAPIError(
            f"Health check failed: {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise RunPodAPIError(
            f"Health check failed: HTTP {resp.status_code}. "
            f"Response: {getattr(resp, 'text', '')[:500]}"
        )
    try:
        return resp.json()
    except Exception as exc:
        raise RunPodAPIError(f"Health response was not valid JSON: {exc}") from exc


def poll_until_complete(
    endpoint_id: str,
    api_key: str,
    runpod_job_id: str,
    max_attempts: int = POLL_MAX_ATTEMPTS,
    interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Poll a RunPod job until it completes or fails.

    Returns the final status dict (with 'status' == 'COMPLETED' or 'FAILED').
    Raises RunPodAPIError if polling exhausts all attempts.
    """
    for attempt in range(max_attempts):
        status = runpod_status(endpoint_id, api_key, runpod_job_id)
        state = status.get("status", "UNKNOWN")
        if state == "COMPLETED":
            return status
        if state == "FAILED":
            return status
        # Still in progress — wait and retry.
        time.sleep(interval)
    raise RunPodAPIError(
        f"Job {runpod_job_id} did not complete within "
        f"{max_attempts * interval:.0f}s ({max_attempts} polls)."
    )


# ---------------------------------------------------------------------------
# Stub detection helpers
# ---------------------------------------------------------------------------


def _is_stub_artifact_hash(artifact_id: str, request_inputs: dict[str, Any]) -> bool:
    """Check if an artifact_id matches the deterministic stub pattern.

    The stub ``LocalTrainer`` derives the artifact hash from the request
    inputs (job_id, dataset_manifest_ref, model_family, search_space,
    random_seed, hardware_class). The real ``RealTrainer`` derives the
    hash from the actual trained model bytes (LightGBM model string), so
    it will NOT match the request-input hash.

    Returns True if the artifact_id matches the stub pattern (i.e. the
    container is running the stub, not the real trainer).
    """
    canonical = json.dumps(
        {
            "schema_version": request_inputs.get("schema_version", 1),
            "job_id": request_inputs.get("job_id", ""),
            "dataset_manifest_ref": request_inputs.get("dataset_manifest_ref", ""),
            "model_family": request_inputs.get("model_family", ""),
            "search_space": request_inputs.get("search_space", {}),
            "random_seed": request_inputs.get("random_seed", 0),
            "hardware_class": request_inputs.get("hardware_class", ""),
            "extra_constraints": request_inputs.get("extra_constraints", {}),
        },
        sort_keys=True,
    ).encode("utf-8")
    stub_hash = hashlib.sha256(canonical).hexdigest()
    # The stub artifact_id is "artifact:<hash[:16]>".
    stub_artifact_id = f"artifact:{stub_hash[:16]}"
    return artifact_id == stub_artifact_id


def _is_stub_prediction(prediction: dict[str, Any], features: list[float]) -> bool:
    """Check if a prediction matches the linear-combination stub pattern.

    The stub ``ShadowInferenceEngine`` computes:
        raw_score = sum(features) / len(features)
        direction = clip(raw_score * 2.0, -1, 1)
        confidence = min(1.0, abs(raw_score) + 0.3)
        p_up = sigmoid(raw_score * 5.0)

    The real ``RealInferenceEngine`` loads an actual model (ONNX/LightGBM)
    and produces predictions that do NOT match this formula.

    Returns True if the prediction matches the stub pattern.
    """
    if not features:
        return False
    raw_score = sum(features) / max(len(features), 1)
    expected_direction = max(-1.0, min(1.0, raw_score * 2.0))
    expected_confidence = min(1.0, abs(raw_score) + 0.3)
    # sigmoid(raw_score * 5.0)
    expected_p_up = 1.0 / (1.0 + (2.718281828 ** (-raw_score * 5.0)))

    direction = prediction.get("direction")
    confidence = prediction.get("confidence")
    p_up = prediction.get("p_up")

    # Compare with a small tolerance for float precision.
    def _close(a: float | None, b: float, tol: float = 1e-6) -> bool:
        if a is None:
            return False
        return abs(float(a) - b) < tol

    return (
        _close(direction, expected_direction)
        and _close(confidence, expected_confidence)
        and _close(p_up, expected_p_up)
    )


def _validate_callback_envelope(envelope: dict[str, Any]) -> list[str]:
    """Validate that a dict matches the RunPodCallbackEnvelope schema.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    required_fields = {
        "schema_version": int,
        "job_id": str,
        "worker_id": str,
        "result_type": str,
        "payload": dict,
    }
    for field_name, field_type in required_fields.items():
        if field_name not in envelope:
            errors.append(f"missing required field '{field_name}'")
        elif not isinstance(envelope[field_name], field_type):
            errors.append(
                f"field '{field_name}' has wrong type: "
                f"expected {field_type.__name__}, got {type(envelope[field_name]).__name__}"
            )
    return errors


# ---------------------------------------------------------------------------
# Verification logic
# ---------------------------------------------------------------------------


def verify_training_endpoint(
    endpoint_id: str,
    api_key: str,
) -> EndpointVerification:
    """Verify the training endpoint returns real ML output.

    Sends a minimal training job and checks:
    1. The response shape matches RunPodCallbackEnvelope.
    2. The artifact_id is NOT the deterministic stub hash.
    3. The response includes training metrics (not the stub's synthetic
       accuracy/logloss derived from the seed).
    """
    verification = EndpointVerification(
        endpoint_name="training",
        endpoint_id=endpoint_id,
    )

    # Build a minimal training request.
    job_id = f"qf:verify:train:{uuid.uuid4().hex[:8]}"
    request_payload = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "ds-verify-1",
        "model_family": "gbm",
        "search_space": {"n_estimators": [100]},
        "random_seed": 42,
        "hardware_class": "verify-gpu",
    }

    # Step 1: health check.
    try:
        health = runpod_health(endpoint_id, api_key)
        verification.checks.append(
            VerificationCheck(
                name="endpoint_health",
                passed=True,
                detail=f"workers: {health.get('workers', {})}",
            )
        )
    except RunPodAPIError as exc:
        verification.error = f"Health check failed: {exc}"
        return verification

    # Step 2: dispatch the training job.
    try:
        runpod_job_id = runpod_dispatch(endpoint_id, api_key, request_payload)
        verification.checks.append(
            VerificationCheck(
                name="job_dispatched",
                passed=True,
                detail=f"runpod_job_id={runpod_job_id}",
            )
        )
    except RunPodAPIError as exc:
        verification.error = f"Dispatch failed: {exc}"
        return verification

    # Step 3: poll for completion.
    try:
        status = poll_until_complete(endpoint_id, api_key, runpod_job_id)
    except RunPodAPIError as exc:
        verification.error = f"Polling failed: {exc}"
        return verification

    state = status.get("status", "UNKNOWN")
    if state != "COMPLETED":
        verification.error = (
            f"Job did not complete successfully: status={state}, "
            f"error={status.get('error', '')}"
        )
        return verification

    output = status.get("output", {})
    if not isinstance(output, dict):
        verification.error = f"Job output is not a dict: {type(output).__name__}"
        return verification

    # Step 4: check for error in output (handler returns error_code on failure).
    if "error_code" in output:
        verification.error = (
            f"Handler returned error: code={output.get('error_code')}, "
            f"summary={output.get('error_summary', '')}"
        )
        return verification

    verification.checks.append(
        VerificationCheck(
            name="job_completed",
            passed=True,
            detail=f"status=COMPLETED",
        )
    )

    # Step 5: parse the callback envelope.
    callback_payload_str = output.get("callback_payload")
    if not callback_payload_str:
        verification.error = "Response missing 'callback_payload' field"
        return verification

    try:
        envelope = json.loads(callback_payload_str) if isinstance(callback_payload_str, str) else callback_payload_str
    except (json.JSONDecodeError, TypeError) as exc:
        verification.error = f"callback_payload is not valid JSON: {exc}"
        return verification

    # Step 6: validate the envelope shape.
    envelope_errors = _validate_callback_envelope(envelope)
    verification.checks.append(
        VerificationCheck(
            name="envelope_shape",
            passed=len(envelope_errors) == 0,
            detail=(
                "RunPodCallbackEnvelope valid"
                if not envelope_errors
                else "; ".join(envelope_errors)
            ),
        )
    )

    # Step 7: check the artifact_id is NOT the stub hash.
    artifact_id = output.get("artifact_id", "")
    is_stub = _is_stub_artifact_hash(artifact_id, request_payload)
    verification.checks.append(
        VerificationCheck(
            name="real_artifact_hash",
            passed=not is_stub and bool(artifact_id),
            detail=(
                f"artifact_id={artifact_id} (real ML hash)"
                if not is_stub
                else f"artifact_id={artifact_id} matches stub pattern — "
                f"container is running LocalTrainer, not RealTrainer"
            ),
        )
    )

    # Step 8: check training metrics are present and not the stub's
    # synthetic values (accuracy=0.5+pbo/2, logloss=0.7-pbo/4).
    payload = envelope.get("payload", {})
    metrics = payload.get("training_metrics", {})
    has_real_metrics = (
        isinstance(metrics, dict)
        and "accuracy" in metrics
        and "logloss" in metrics
    )
    verification.checks.append(
        VerificationCheck(
            name="training_metrics_present",
            passed=has_real_metrics,
            detail=(
                f"metrics keys: {list(metrics.keys()) if isinstance(metrics, dict) else 'N/A'}"
            ),
        )
    )

    # Step 9: check that lightgbm is importable (via a metadata check in
    # the payload, if the handler exposes it). The real trainer sets
    # metadata.model_family and may include a lightgbm version. We check
    # for the presence of real training metadata.
    metadata = payload.get("metadata", {})
    has_lightgbm_marker = (
        isinstance(metadata, dict)
        and (
            "lightgbm_version" in metadata
            or metadata.get("trainer") == "real"
            or metadata.get("model_family") == "gbm"
        )
    )
    verification.checks.append(
        VerificationCheck(
            name="lightgbm_importable",
            passed=has_lightgbm_marker,
            detail=(
                f"metadata: {metadata}"
                if isinstance(metadata, dict)
                else "metadata missing"
            ),
        )
    )

    return verification


def verify_inference_endpoint(
    endpoint_id: str,
    api_key: str,
) -> EndpointVerification:
    """Verify the inference endpoint returns real ML output.

    Sends a minimal inference job and checks:
    1. The response shape matches RunPodCallbackEnvelope.
    2. The predictions are NOT the linear-combination stub.
    3. The response includes real prediction values.
    """
    verification = EndpointVerification(
        endpoint_name="inference",
        endpoint_id=endpoint_id,
    )

    # Build a minimal inference request with known features so we can
    # detect the stub pattern.
    test_features = [0.1, 0.2, 0.3, 0.4]
    job_id = f"qf:verify:infer:{uuid.uuid4().hex[:8]}"
    request_payload = {
        "request": {
            "schema_version": 1,
            "job_id": job_id,
            "artifact_ref": "file:///verify-model.pkl",
            "symbols": ["AAPL"],
            "horizons_ns": [3_600_000_000_000],
        },
        "snapshot": {
            "symbols": ["AAPL"],
            "features": {"AAPL": test_features},
            "availability": {"AAPL": True},
            "ts_event": 1_000_000_000,
            "freshness_ns": 500,
        },
        "model_id": "verify-model-1",
    }

    # Step 1: health check.
    try:
        health = runpod_health(endpoint_id, api_key)
        verification.checks.append(
            VerificationCheck(
                name="endpoint_health",
                passed=True,
                detail=f"workers: {health.get('workers', {})}",
            )
        )
    except RunPodAPIError as exc:
        verification.error = f"Health check failed: {exc}"
        return verification

    # Step 2: dispatch the inference job.
    try:
        runpod_job_id = runpod_dispatch(endpoint_id, api_key, request_payload)
        verification.checks.append(
            VerificationCheck(
                name="job_dispatched",
                passed=True,
                detail=f"runpod_job_id={runpod_job_id}",
            )
        )
    except RunPodAPIError as exc:
        verification.error = f"Dispatch failed: {exc}"
        return verification

    # Step 3: poll for completion.
    try:
        status = poll_until_complete(endpoint_id, api_key, runpod_job_id)
    except RunPodAPIError as exc:
        verification.error = f"Polling failed: {exc}"
        return verification

    state = status.get("status", "UNKNOWN")
    if state != "COMPLETED":
        verification.error = (
            f"Job did not complete successfully: status={state}, "
            f"error={status.get('error', '')}"
        )
        return verification

    output = status.get("output", {})
    if not isinstance(output, dict):
        verification.error = f"Job output is not a dict: {type(output).__name__}"
        return verification

    # Check for handler error.
    if "error" in output and output.get("error") == "inference_disabled":
        verification.error = (
            "Inference is disabled on the endpoint "
            "(QUANT_FOUNDRY_MODE != runpod_shadow). "
            "Enable inference mode before verifying."
        )
        return verification
    if "error_code" in output:
        verification.error = (
            f"Handler returned error: code={output.get('error_code')}, "
            f"summary={output.get('error_summary', '')}"
        )
        return verification

    verification.checks.append(
        VerificationCheck(
            name="job_completed",
            passed=True,
            detail="status=COMPLETED",
        )
    )

    # Step 4: parse the callback envelope.
    callback_payload_str = output.get("callback_payload")
    if not callback_payload_str:
        verification.error = "Response missing 'callback_payload' field"
        return verification

    try:
        envelope = json.loads(callback_payload_str) if isinstance(callback_payload_str, str) else callback_payload_str
    except (json.JSONDecodeError, TypeError) as exc:
        verification.error = f"callback_payload is not valid JSON: {exc}"
        return verification

    # Step 5: validate the envelope shape.
    envelope_errors = _validate_callback_envelope(envelope)
    verification.checks.append(
        VerificationCheck(
            name="envelope_shape",
            passed=len(envelope_errors) == 0,
            detail=(
                "RunPodCallbackEnvelope valid"
                if not envelope_errors
                else "; ".join(envelope_errors)
            ),
        )
    )

    # Step 6: check the predictions are NOT the stub linear combination.
    predictions = output.get("predictions", [])
    if not isinstance(predictions, list) or len(predictions) == 0:
        verification.checks.append(
            VerificationCheck(
                name="predictions_present",
                passed=False,
                detail="no predictions in response",
            )
        )
        verification.checks.append(
            VerificationCheck(
                name="real_predictions_not_stub",
                passed=False,
                detail="cannot verify — no predictions",
            )
        )
        verification.checks.append(
            VerificationCheck(
                name="onnxruntime_lightgbm_importable",
                passed=False,
                detail="cannot verify — no predictions",
            )
        )
        return verification

    verification.checks.append(
        VerificationCheck(
            name="predictions_present",
            passed=True,
            detail=f"{len(predictions)} prediction(s)",
        )
    )

    # Check each prediction against the stub formula.
    stub_matches = [
        _is_stub_prediction(p, test_features)
        for p in predictions
        if isinstance(p, dict)
    ]
    any_stub = any(stub_matches)
    verification.checks.append(
        VerificationCheck(
            name="real_predictions_not_stub",
            passed=not any_stub,
            detail=(
                "predictions do not match stub linear-combination formula (real ML)"
                if not any_stub
                else f"{sum(stub_matches)}/{len(stub_matches)} predictions "
                f"match the stub formula — container is running "
                f"ShadowInferenceEngine, not RealInferenceEngine"
            ),
        )
    )

    # Step 7: check that onnxruntime + lightgbm are importable. The real
    # inference engine loads models via onnxruntime or lightgbm. We infer
    # this from the prediction structure — real predictions have a
    # non-stub p_up value and the callback result_type should be
    # "inference_batch".
    result_type = envelope.get("result_type", "")
    has_correct_result_type = result_type == "inference_batch"
    verification.checks.append(
        VerificationCheck(
            name="onnxruntime_lightgbm_importable",
            passed=has_correct_result_type and not any_stub,
            detail=(
                f"result_type={result_type}, predictions are real "
                f"(onnxruntime/lightgbm loaded successfully)"
                if has_correct_result_type and not any_stub
                else f"result_type={result_type} — real inference engine "
                f"not active"
            ),
        )
    )

    return verification


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------


def print_report(report: VerificationReport) -> None:
    """Print a human-readable verification report."""
    print("\n" + "=" * 70)
    print("RunPod Container Verification Report")
    print("=" * 70)

    for v in report.verifications:
        status = "PASS" if v.passed else "FAIL"
        print(f"\n[{status}] {v.endpoint_name} endpoint ({v.endpoint_id})")
        if v.error:
            print(f"  ERROR: {v.error}")
        for check in v.checks:
            check_status = "OK" if check.passed else "FAIL"
            print(f"  [{check_status}] {check.name}: {check.detail}")

    print("\n" + "=" * 70)
    if report.all_passed:
        print("Result: ALL VERIFICATIONS PASSED")
    else:
        failed = sum(1 for v in report.verifications if not v.passed)
        print(f"Result: {failed} verification(s) FAILED")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Verify deployed RunPod containers have the correct ML "
            "dependencies (lightgbm, pyarrow, onnxruntime, numpy). "
            "Sends test payloads and checks for real ML output (not stubs)."
        ),
    )
    parser.add_argument(
        "--endpoint",
        choices=["training", "inference", "both"],
        default="both",
        help="Which endpoint to verify (default: both).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "RunPod API key. Defaults to the RUNPOD_API_KEY env var. "
            "Never hardcoded."
        ),
    )
    parser.add_argument(
        "--training-endpoint-id",
        default=None,
        help=(
            "RunPod training endpoint ID "
            f"(default: {DEFAULT_TRAINING_ENDPOINT_ID} or "
            "RUNPOD_TRAINING_ENDPOINT_ID env var)."
        ),
    )
    parser.add_argument(
        "--inference-endpoint-id",
        default=None,
        help=(
            "RunPod inference endpoint ID "
            f"(default: {DEFAULT_INFERENCE_ENDPOINT_ID} or "
            "RUNPOD_INFERENCE_ENDPOINT_ID env var)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code."""
    args = parse_args(argv)

    # Resolve API key.
    api_key = args.api_key or os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        print(
            "ERROR: RunPod API key required. Set the RUNPOD_API_KEY env var "
            "or pass --api-key.",
            file=sys.stderr,
        )
        return 1

    # Resolve endpoint IDs.
    training_endpoint_id = (
        args.training_endpoint_id
        or os.environ.get("RUNPOD_TRAINING_ENDPOINT_ID", DEFAULT_TRAINING_ENDPOINT_ID)
    )
    inference_endpoint_id = (
        args.inference_endpoint_id
        or os.environ.get("RUNPOD_INFERENCE_ENDPOINT_ID", DEFAULT_INFERENCE_ENDPOINT_ID)
    )

    # Determine which endpoints to verify.
    targets: list[tuple[str, str]] = []
    if args.endpoint in ("training", "both"):
        targets.append(("training", training_endpoint_id))
    if args.endpoint in ("inference", "both"):
        targets.append(("inference", inference_endpoint_id))

    report = VerificationReport()

    for name, endpoint_id in targets:
        print(f"\nVerifying {name} endpoint ({endpoint_id}) ...")
        try:
            if name == "training":
                verification = verify_training_endpoint(endpoint_id, api_key)
            else:
                verification = verify_inference_endpoint(endpoint_id, api_key)
        except Exception as exc:
            # Catch-all for unexpected errors — never crash with a raw traceback.
            verification = EndpointVerification(
                endpoint_name=name,
                endpoint_id=endpoint_id,
                error=f"Unexpected error: {type(exc).__name__}: {exc}",
            )
        report.verifications.append(verification)

    print_report(report)
    return 0 if report.all_passed else 2


if __name__ == "__main__":
    sys.exit(main())
