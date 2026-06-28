"""
RunPod entrypoint for the Quant Foundry training worker (TASK-0501).

This module is the bridge between RunPod's serverless handler protocol and
the quant_foundry.runpod_training.RunPodTrainingHandler. RunPod calls
`handler(event)` for each job; we parse the event into a
RunPodTrainingRequest, invoke the handler, and return the signed callback
envelope + signature for the dispatcher to ingest.

Security invariants (non-negotiable):
- NO broker credentials, NO Redis, NO stream write capability. This handler
  runs in an isolated container with no trading access. It only reads the
  request, trains, and returns a signed callback.
- The callback is signed with QUANT_FOUNDRY_CALLBACK_SECRET (env var). The
  dispatcher verifies the signature before processing.
- Training failures return a safe terminal status (error dict), not a crash.
- Time/budget limits are enforced by the handler.

RunPod protocol:
- Input: `event["input"]` is a dict matching RunPodTrainingRequest.
- Output: a dict with `callback_payload` (JSON string), `callback_signature`,
  `callback_ts`, and `job_id`. On failure: `error_code` + `error_summary`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

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

from quant_foundry.runpod_training import (  # noqa: E402
    LocalTrainer,
    RunPodTrainingHandler,
    TrainingFailure,
)
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402
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

    This is NOT a training job — it bypasses the training pipeline
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


def _get_deadline_seconds() -> int:
    raw = os.environ.get("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    try:
        return int(raw)
    except ValueError:
        return 600


def _build_trainer() -> Any:
    """Select the trainer based on the QUANT_FOUNDRY_USE_REAL_TRAINER env var.

    When ``QUANT_FOUNDRY_USE_REAL_TRAINER=true``, use ``RealLightGBMTrainer``
    which trains a real LightGBM model with walk-forward validation and
    produces real metrics (accuracy, logloss, brier, PBO, Sharpe, drawdown).

    Otherwise, fall back to ``LocalTrainer`` (the deterministic stub) for
    backward-compatible testing and contract proofs.
    """
    use_real = os.environ.get("QUANT_FOUNDRY_USE_REAL_TRAINER", "").lower() == "true"
    if use_real:
        from quant_foundry.real_trainer import RealLightGBMTrainer

        return RealLightGBMTrainer()
    return LocalTrainer()


def _heartbeat_during_training(
    job_id: str, interval: float = 10.0
) -> threading.Event:
    """Start a background heartbeat thread. Returns a stop event.

    The thread writes a heartbeat status file every ``interval`` seconds
    so the gateway can detect stale/crashed workers. The caller must
    ``set()`` the returned event to stop the thread.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            write_heartbeat(job_id)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """RunPod serverless handler entrypoint.

    Args:
        event: RunPod event dict. `event["input"]` must be a dict matching
            RunPodTrainingRequest.

    Returns:
        On success: dict with callback_payload, callback_signature,
        callback_ts, job_id, artifact_id, dossier_id.
        On failure: dict with error_code, error_summary, job_id.
    """
    input_data = event.get("input") if isinstance(event, dict) else None
    if not isinstance(input_data, dict):
        return {
            "error_code": "bad_request",
            "error_summary": "event['input'] must be a dict matching RunPodTrainingRequest",
            "job_id": None,
        }

    # Callback-secret canary: bypasses the training pipeline entirely.
    # The API dispatches this to verify that the worker shares the same
    # QUANT_FOUNDRY_CALLBACK_SECRET. See gateway.runpod_canary().
    if input_data.get("task") == "callback_secret_canary":
        return _handle_canary(input_data)

    # Support inline dataset for E2E testing: if the input includes
    # ``inline_dataset_csv``, write it to a temp file and override the
    # dataset_manifest_ref. This avoids needing a network volume or S3
    # bucket for simple smoke tests. The field is NOT part of the
    # RunPodTrainingRequest schema — it is a handler-level extension, so
    # we must pop it from the input BEFORE schema validation (the schema
    # forbids extra fields).
    inline_csv = input_data.pop("inline_dataset_csv", None)

    try:
        req = RunPodTrainingRequest.model_validate(input_data)
    except Exception as exc:
        return {
            "error_code": "schema_validation_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id"),
        }

    if isinstance(inline_csv, str) and inline_csv.strip():
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="qf_dataset_"))
        csv_path = tmp_dir / "inline_dataset.csv"
        csv_path.write_text(inline_csv, encoding="utf-8")
        req = req.model_copy(update={"dataset_manifest_ref": str(csv_path)})

    # Worker-side status file: mark the job as started so the gateway
    # can detect crashed workers via stale heartbeat_at timestamps.
    write_status(req.job_id, "started")

    handler = RunPodTrainingHandler(
        callback_secret=_get_callback_secret(),
        trainer=_build_trainer(),
        deadline_seconds=_get_deadline_seconds(),
    )

    # Background heartbeat thread: writes a heartbeat status file every
    # 10s while training runs. If the container crashes, the gateway
    # detects a stale heartbeat_at and marks the job as failed.
    heartbeat_stop = _heartbeat_during_training(req.job_id)
    try:
        result = handler.handle(req)
    except TrainingFailure as exc:
        write_status(
            req.job_id,
            "failed",
            error_code=exc.error_code,
            error_summary=exc.error_summary,
        )
        return {
            "error_code": exc.error_code,
            "error_summary": exc.error_summary,
            "job_id": req.job_id,
        }
    finally:
        heartbeat_stop.set()

    write_status(req.job_id, "completed", artifact_id=result.artifact_id)

    return {
        "job_id": req.job_id,
        "callback_payload": result.callback_payload.decode("utf-8"),
        "callback_signature": result.callback_signature,
        "callback_ts": result.callback_ts,
        "artifact_id": result.artifact_id,
        "dossier_id": result.dossier_id,
    }


# RunPod's serverless module loader looks for a `handler` function at the
# top level. When running on RunPod serverless, use the runpod SDK to start
# the worker. When run as a script (local testing), accept JSON on stdin.
if __name__ == "__main__":  # pragma: no cover
    import sys
    import traceback

    # Debug logging to network volume (try both mount paths)
    def _log(msg):
        print(msg, flush=True)  # noqa: T201 - CLI debug output
        for path in ["/runpod-volume/handler-debug.log", "/workspace/handler-debug.log"]:
            try:
                with open(path, "a") as f:
                    f.write(msg + "\n")
            except Exception:  # noqa: S110 - best-effort debug log
                pass

    _log(f"=== Handler starting at {__file__} ===")
    _log(f"PYTHONPATH={os.environ.get('PYTHONPATH', 'NOT SET')}")
    _log(f"sys.path={sys.path}")

    # Check if handler file exists
    _log(f"Handler file exists: {os.path.exists(__file__)}")

    # Try RunPod serverless mode first (uses runpod SDK)
    try:
        import runpod
        _log(f"runpod SDK imported, version: {getattr(runpod, '__version__', 'unknown')}")

        # Dump RUNPOD_* env vars to diagnose serverless vs local mode.
        # The SDK checks for RUNPOD_WEBHOOK_GET_JOB to decide whether to
        # poll the real job queue (serverless) or start a local FastAPI
        # test server on :8000 (local mode). If this var is missing,
        # jobs will stay IN_QUEUE forever while the worker looks "ready".
        runpod_env = {k: v for k, v in os.environ.items() if k.startswith("RUNPOD_")}
        _log(f"RUNPOD_* env vars: {json.dumps(runpod_env, indent=2)}")
        if not runpod_env:
            _log("WARNING: No RUNPOD_* env vars found! SDK will likely enter local/test mode.")
            _log("  This means the worker will NOT poll the real job queue.")
            _log("  Jobs will stay IN_QUEUE indefinitely while the worker shows 'ready'.")

        _log("Starting runpod.serverless.start()...")
        runpod.serverless.start({"handler": handler})
    except ImportError as e:
        _log(f"ImportError: {e}")
        # runpod SDK not installed — fall back to stdin mode for local testing
        raw = sys.stdin.read()
        event = json.loads(raw) if raw else {}
        result = handler(event)
        print(json.dumps(result, indent=2))  # noqa: T201 - CLI entrypoint output
    except Exception as e:
        _log(f"ERROR in runpod.serverless.start(): {e}")
        _log(traceback.format_exc())
        raise
