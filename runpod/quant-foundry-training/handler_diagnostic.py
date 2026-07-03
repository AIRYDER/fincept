"""Diagnostic handler — full quant_foundry imports + try/except wrapper.

This handler imports everything the real handler imports, then wraps the
handler() logic in a try/except to capture and return any exception instead
of crashing the worker. This helps isolate the exact crash point.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Add shared utilities to sys.path
_shared_paths = [
    os.path.join(os.path.dirname(__file__), "..", "shared"),
    os.path.join(os.path.dirname(__file__), "shared"),
    "/app/runpod/shared",
    "/worker/runpod/shared",
]
for _p in _shared_paths:
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

try:
    from worker_status import clear_status, write_heartbeat, write_status
except ImportError:
    def write_status(*args, **kwargs): pass
    def write_heartbeat(*args, **kwargs): pass
    def clear_status(*args, **kwargs): pass

from pydantic import BaseModel, ConfigDict, Field

# === ALL quant_foundry imports (same as real handler) ===
print("[diag] importing quant_foundry...", flush=True)
sys.stdout.flush()

try:
    from quant_foundry.data_ingestion.quality_report import (
        QUALITY_POLICY_REGISTRY,
        DatasetQualityReport,
        FailedCheck,
        QualityGateResult,
        QualityPolicy,
        resolve_quality_policy,
        validate_quality_policy,
    )
    print("[diag] imported quality_report", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED quality_report: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.dataset_manifest import (
        ColumnRoles as QFColumnRoles,
    )
    from quant_foundry.dataset_manifest import (
        FoldSpec as QFFoldSpec,
    )
    print("[diag] imported dataset_manifest", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED dataset_manifest: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.real_trainer import (
        TypedArtifactResult,
        build_artifact_result,
    )
    print("[diag] imported real_trainer", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED real_trainer: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.runpod_training import (
        LocalTrainer,
        RunPodTrainingHandler,
        SignedFailureEnvelope,
        TrainingFailure,
        build_callback,
        build_failure_envelope,
        verify_failure_envelope,
    )
    print("[diag] imported runpod_training", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED runpod_training: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.schemas import RunPodTrainingRequest
    print("[diag] imported schemas", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED schemas: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.signatures import sign_callback
    print("[diag] imported signatures", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED signatures: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from quant_foundry.training_manifest import (
        MODE_RULES,
        ModelTaskSpec,
        TrainingMode,
    )
    print("[diag] imported training_manifest", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED training_manifest: {e}", flush=True)
    sys.stdout.flush()
    raise

try:
    from fincept_core.datasets import (
        DatasetLoadError,
        LoadedDataset,
        ManifestDatasetLoader,
    )
    print("[diag] imported fincept_core.datasets", flush=True)
    sys.stdout.flush()
except Exception as e:
    print(f"[diag] FAILED fincept_core.datasets: {e}", flush=True)
    sys.stdout.flush()
    raise

print("[diag] ALL IMPORTS OK", flush=True)
sys.stdout.flush()


def handler(event: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic handler — wraps everything in try/except."""
    try:
        print("[diag] handler() called", flush=True)
        sys.stdout.flush()

        input_data = event.get("input") if isinstance(event, dict) else None
        if not isinstance(input_data, dict):
            return {"error": "input must be dict", "status": "error"}

        print(f"[diag] input_data keys: {list(input_data.keys())}", flush=True)
        sys.stdout.flush()

        task = input_data.get("task", "")
        job_id = input_data.get("job_id", "unknown")
        nonce = input_data.get("nonce", "")
        secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")

        print(f"[diag] task={task!r} job_id={job_id!r} secret_set={bool(secret)}", flush=True)
        sys.stdout.flush()

        # Test sign_callback
        callback_payload = json.dumps({
            "schema_version": 1,
            "job_id": job_id,
            "worker_id": "runpod-diagnostic",
            "result_type": "callback_secret_canary",
            "payload": {"nonce": nonce},
        }, sort_keys=True).encode("utf-8")

        callback_ts = int(time.time())
        print("[diag] calling sign_callback...", flush=True)
        sys.stdout.flush()
        callback_signature = sign_callback(
            callback_payload,
            secret=secret,
            ts=callback_ts,
            job_id=job_id,
        )
        print("[diag] sign_callback OK", flush=True)
        sys.stdout.flush()

        # Test build_failure_envelope
        print("[diag] testing build_failure_envelope...", flush=True)
        sys.stdout.flush()
        envelope = build_failure_envelope(
            error_code="test",
            error_message="diagnostic test",
            mode="canary",
            context={"job_id": job_id, "stage": "diag"},
            secret=secret,
            worker_id="runpod-diagnostic",
        )
        print(f"[diag] build_failure_envelope OK: {envelope.error_code}", flush=True)
        sys.stdout.flush()

        # Test RunPodTrainingRequest validation
        print("[diag] testing RunPodTrainingRequest...", flush=True)
        sys.stdout.flush()
        try:
            req = RunPodTrainingRequest.model_validate(input_data)
            print(f"[diag] RunPodTrainingRequest OK: {req.job_id}", flush=True)
        except Exception as e:
            print(f"[diag] RunPodTrainingRequest validation failed (expected for canary): {e}", flush=True)
        sys.stdout.flush()

        result = {
            "job_id": job_id,
            "callback_payload": callback_payload.decode("utf-8"),
            "callback_signature": callback_signature,
            "callback_ts": callback_ts,
            "status": "ok",
            "handler": "diagnostic",
            "task": task,
        }
        print("[diag] returning result", flush=True)
        sys.stdout.flush()
        return result

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[diag] EXCEPTION: {e}", flush=True)
        print(f"[diag] TRACEBACK:\n{tb}", flush=True)
        sys.stdout.flush()
        return {
            "error": str(e),
            "traceback": tb,
            "status": "error",
            "job_id": event.get("input", {}).get("job_id", "unknown") if isinstance(event, dict) else "unknown",
        }


if __name__ == "__main__":
    import sys
    try:
        import runpod
        print(f"[diag] runpod SDK version: {getattr(runpod, '__version__', 'unknown')}", flush=True)
        sys.stdout.flush()
        runpod.serverless.start({"handler": handler})
        # Force flush and hard exit
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    except ImportError:
        event = json.loads(sys.stdin.read())
        result = handler(event)
        sys.stdout.write(json.dumps(result, indent=2))
    except Exception as e:
        print(f"[diag] STARTUP ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
