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
from pathlib import Path
from typing import Any

from quant_foundry.runpod_training import (
    LocalTrainer,
    RunPodTrainingHandler,
    TrainingFailure,
)
from quant_foundry.schemas import RunPodTrainingRequest


def _get_callback_secret() -> str:
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not secret:
        # In production, the container should fail to start without a
        # secret. For local testing, we allow a default but warn.
        return "dev-callback-secret-DO-NOT-USE-IN-PROD"
    return secret


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

    try:
        req = RunPodTrainingRequest.model_validate(input_data)
    except Exception as exc:
        return {
            "error_code": "schema_validation_failed",
            "error_summary": str(exc),
            "job_id": input_data.get("job_id"),
        }

    # Support inline dataset for E2E testing: if the input includes
    # ``inline_dataset_csv``, write it to a temp file and override the
    # dataset_manifest_ref. This avoids needing a network volume or S3
    # bucket for simple smoke tests. The field is NOT part of the
    # RunPodTrainingRequest schema — it is a handler-level extension.
    inline_csv = input_data.get("inline_dataset_csv")
    if isinstance(inline_csv, str) and inline_csv.strip():
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="qf_dataset_"))
        csv_path = tmp_dir / "inline_dataset.csv"
        csv_path.write_text(inline_csv, encoding="utf-8")
        req = req.model_copy(update={"dataset_manifest_ref": str(csv_path)})

    handler = RunPodTrainingHandler(
        callback_secret=_get_callback_secret(),
        trainer=_build_trainer(),
        deadline_seconds=_get_deadline_seconds(),
    )

    try:
        result = handler.handle(req)
    except TrainingFailure as exc:
        return {
            "error_code": exc.error_code,
            "error_summary": exc.error_summary,
            "job_id": req.job_id,
        }

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
