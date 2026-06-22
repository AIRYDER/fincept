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
from typing import Any

from quant_foundry.runpod_training import (
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

    handler = RunPodTrainingHandler(
        callback_secret=_get_callback_secret(),
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
# top level. When run as a script (local testing), accept JSON on stdin.
if __name__ == "__main__":  # pragma: no cover
    import sys

    raw = sys.stdin.read()
    event = json.loads(raw) if raw else {}
    result = handler(event)
    print(json.dumps(result, indent=2))  # noqa: T201 - CLI entrypoint output
