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
from typing import Any

# Add the quant_foundry package to the path (for RunPod container).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "quant_foundry", "src"))

from quant_foundry.schemas import RunPodInferenceRequest
from quant_foundry.shadow_inference import (
    FeatureSnapshot,
    InferenceDisabledError,
    ShadowInferenceEngine,
)


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """RunPod handler entry point.

    Args:
    - ``event``: the RunPod event dict containing the job input.

    Returns:
    - A dict with the callback envelope and predictions.
    """
    # Parse the inference request from the event.
    input_data = event.get("input", {})
    request = RunPodInferenceRequest(**input_data["request"])

    # Parse the feature snapshot.
    snapshot = FeatureSnapshot(**input_data["snapshot"])

    # Get the model_id.
    model_id = input_data.get("model_id", "unknown")

    # Check if inference is enabled.
    enabled = os.environ.get("QUANT_FOUNDRY_MODE", "") == "runpod_shadow"

    # Run the inference engine.
    engine = ShadowInferenceEngine(enabled=enabled)
    try:
        result = engine.run(request=request, snapshot=snapshot, model_id=model_id)
        return {
            "callback": result.callback.model_dump(),
            "predictions": [p.model_dump() for p in result.predictions],
            "latency_ms": result.latency_ms,
        }
    except InferenceDisabledError as e:
        return {
            "error": "inference_disabled",
            "message": str(e),
            "callback": None,
            "predictions": [],
        }


if __name__ == "__main__":
    # For local testing: read JSON from stdin.
    event = json.loads(sys.stdin.read())
    result = handler(event)
    sys.stdout.write(json.dumps(result, indent=2))
