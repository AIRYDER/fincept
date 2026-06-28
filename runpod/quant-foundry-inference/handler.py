"""
RunPod shadow inference handler (TASK-0601).

This is the entry point for the RunPod inference container. It accepts a
``RunPodInferenceRequest``, loads the feature snapshot, runs the
``ShadowInferenceEngine``, and returns the signed callback envelope.

Usage:
    python handler.py

The handler is a thin wrapper around ``quant_foundry.shadow_inference``.
The actual inference logic (model loading, scoring) lives in the
``ShadowInferenceEngine`` class, which can be injected with a real model
loader for production use.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

# Add the quant_foundry package to the path (for RunPod container).
# In the container, quant_foundry is at /app/quant_foundry/.
# For local testing from the repo, it's under services/quant_foundry/src/.
_quant_foundry_paths = [
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "quant_foundry", "src"),
    os.path.join(os.path.dirname(__file__), "quant_foundry"),
    "/app",
]
for _p in _quant_foundry_paths:
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

# Add the shared RunPod utilities to sys.path so we can import
# worker_status. In the container the shared module may be at different
# paths (sibling to the handler, or under /app/runpod/shared). For local
# testing it's under runpod/shared relative to the repo root.
_shared_paths = [
    os.path.join(os.path.dirname(__file__), "..", "shared"),
    os.path.join(os.path.dirname(__file__), "shared"),
    "/app/runpod/shared",
]
for _p in _shared_paths:
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

try:
    from worker_status import clear_status, write_heartbeat, write_status
except ImportError:  # pragma: no cover - fallback if shared module missing
    # Best-effort: define no-op stubs so the handler still runs even if
    # the worker_status module is unavailable (e.g. older container image).
    def write_status(*args, **kwargs):  # type: ignore[no-redef]
        pass

    def write_heartbeat(*args, **kwargs):  # type: ignore[no-redef]
        pass

    def clear_status(*args, **kwargs):  # type: ignore[no-redef]
        pass

from quant_foundry.real_inference import RealInferenceEngine  # noqa: E402
from quant_foundry.schemas import RunPodInferenceRequest  # noqa: E402
from quant_foundry.shadow_inference import (  # noqa: E402
    FeatureSnapshot,
    InferenceDisabledError,
    ShadowInferenceEngine,
)
from quant_foundry.signatures import sign_callback  # noqa: E402


def _get_callback_secret() -> str:
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not secret:
        # Fail closed: no callback secret means callbacks cannot be
        # signed, which would allow forgery.  Refuse to start rather
        # than silently falling back to a known-weak default.
        raise RuntimeError(
            "QUANT_FOUNDRY_CALLBACK_SECRET is not set. "
            "This secret is required to sign HMAC callbacks to the API. "
            "Set it in the RunPod template environment or container env."
        )
    return secret


def _handle_canary(input_data: dict[str, Any]) -> dict[str, Any]:
    """Handle a callback-secret canary job.

    The canary is a minimal round-trip that proves the RunPod worker and
    the API share the same ``QUANT_FOUNDRY_CALLBACK_SECRET``. The API
    dispatches a canary job with a random nonce; the worker signs the
    nonce-bearing payload and returns it. The API verifies the signature.

    This is NOT an inference job — it bypasses the inference pipeline
    entirely and returns immediately.
    """
    job_id = input_data.get("job_id") or "canary-unknown"
    nonce = input_data.get("nonce") or ""
    callback_payload = json.dumps(
        {
            "schema_version": 1,
            "job_id": job_id,
            "worker_id": "runpod-canary",
            "result_type": "callback_secret_canary",
            "payload": {"nonce": nonce},
        },
        sort_keys=True,
    ).encode("utf-8")
    callback_ts = int(time.time())
    callback_signature = sign_callback(
        callback_payload,
        secret=_get_callback_secret(),
        ts=callback_ts,
        job_id=job_id,
    )
    return {
        "job_id": job_id,
        "callback_payload": callback_payload.decode("utf-8"),
        "callback_signature": callback_signature,
        "callback_ts": callback_ts,
        "canary": True,
        "nonce": nonce,
    }


def _heartbeat_during_inference(
    job_id: str, interval: float = 5.0
) -> threading.Event:
    """Start a background heartbeat thread. Returns a stop event.

    Inference is faster than training, so a shorter interval (5s) is
    used. The thread writes a heartbeat status file every ``interval``
    seconds so the gateway can detect stale/crashed workers. The caller
    must ``set()`` the returned event to stop the thread.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            write_heartbeat(job_id)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """RunPod handler entry point.

    Args:
    - ``event``: the RunPod event dict containing the job input.

    Returns:
    - On success: dict with callback envelope, predictions, latency_ms.
    - On failure: dict with error_code, error_summary, job_id.
    """
    # --- Input validation (matches training handler pattern) ---
    input_data = event.get("input") if isinstance(event, dict) else None
    if not isinstance(input_data, dict):
        return {
            "error_code": "bad_request",
            "error_summary": "event['input'] must be a dict with 'request' and 'snapshot' keys",
            "job_id": None,
        }

    # Callback-secret canary: bypasses the inference pipeline entirely.
    # The API dispatches this to verify that the worker shares the same
    # QUANT_FOUNDRY_CALLBACK_SECRET. See gateway.runpod_canary().
    if input_data.get("task") == "callback_secret_canary":
        return _handle_canary(input_data)

    # Validate the request sub-dict.
    request_raw = input_data.get("request")
    if not isinstance(request_raw, dict):
        return {
            "error_code": "bad_request",
            "error_summary": "event['input']['request'] must be a dict matching RunPodInferenceRequest",
            "job_id": input_data.get("job_id"),
        }

    try:
        request = RunPodInferenceRequest.model_validate(request_raw)
    except Exception as exc:
        return {
            "error_code": "schema_validation_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id"),
        }

    # Validate the snapshot sub-dict.
    snapshot_raw = input_data.get("snapshot")
    if not isinstance(snapshot_raw, dict):
        return {
            "error_code": "bad_request",
            "error_summary": "event['input']['snapshot'] must be a dict matching FeatureSnapshot",
            "job_id": input_data.get("job_id") or request.job_id,
        }

    try:
        snapshot = FeatureSnapshot.model_validate(snapshot_raw)
    except Exception as exc:
        return {
            "error_code": "schema_validation_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id") or request.job_id,
        }

    # Get the model_id.
    model_id = input_data.get("model_id", "unknown")

    # Check if inference is enabled.
    enabled = os.environ.get("QUANT_FOUNDRY_MODE", "") == "runpod_shadow"

    # Decide whether to use the real model-loading engine or the stub.
    # Real inference is used when QUANT_FOUNDRY_USE_REAL_INFERENCE=true AND
    # the request carries an artifact_ref that points at a model artifact
    # (file:// or s3://). Otherwise we fall back to the stub engine for
    # backward-compatible testing.
    use_real = (
        os.environ.get("QUANT_FOUNDRY_USE_REAL_INFERENCE", "").lower() == "true"
        and bool(request.artifact_ref)
    )

    if use_real:
        engine = RealInferenceEngine(enabled=enabled)
    else:
        engine = ShadowInferenceEngine(enabled=enabled)

    # Worker-side status file: mark the job as inferring so the gateway
    # can detect crashed workers via stale heartbeat_at timestamps.
    write_status(request.job_id, "inferring")

    # Background heartbeat thread: writes a heartbeat status file every
    # 5s while inference runs. If the container crashes, the gateway
    # detects a stale heartbeat_at and marks the job as failed.
    heartbeat_stop = _heartbeat_during_inference(request.job_id)
    try:
        result = engine.run(request=request, snapshot=snapshot, model_id=model_id)
        callback_payload = result.callback.model_dump_json().encode("utf-8")
        callback_ts = int(time.time())
        write_status(
            request.job_id,
            "completed",
            extra={
                "callback_ts": callback_ts,
                "latency_ms": result.latency_ms,
            },
        )
        return {
            "job_id": request.job_id,
            "callback_payload": callback_payload.decode("utf-8"),
            "callback_signature": sign_callback(
                callback_payload,
                secret=_get_callback_secret(),
                ts=callback_ts,
                job_id=request.job_id,
            ),
            "callback_ts": callback_ts,
            "callback": result.callback.model_dump(),
            "predictions": [p.model_dump() for p in result.predictions],
            "latency_ms": result.latency_ms,
        }
    except InferenceDisabledError as e:
        write_status(
            request.job_id,
            "failed",
            error_code="inference_disabled",
            error_summary=str(e),
        )
        return {
            "error_code": "inference_disabled",
            "error_summary": str(e),
            "job_id": request.job_id,
            "callback": None,
            "predictions": [],
        }
    except Exception as exc:
        # Catch-all: any unhandled exception returns a structured error
        # envelope instead of crashing the RunPod worker.
        write_status(
            request.job_id,
            "failed",
            error_code="inference_failed",
            error_summary=str(exc),
        )
        return {
            "error_code": "inference_failed",
            "error_summary": str(exc),
            "job_id": request.job_id,
            "callback": None,
            "predictions": [],
        }
    finally:
        heartbeat_stop.set()


if __name__ == "__main__":
    # Try RunPod serverless mode first (uses runpod SDK)
    try:
        import runpod

        runpod.serverless.start({"handler": handler})
    except ImportError:
        # runpod SDK not installed — fall back to stdin mode for local testing
        event = json.loads(sys.stdin.read())
        result = handler(event)
        sys.stdout.write(json.dumps(result, indent=2))
