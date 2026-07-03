"""Minimal handler + quant_foundry imports to isolate crash cause."""
import json
import time
import hashlib
import hmac
import os
import sys

# Heavy ML imports — same as the real training handler
import numpy as np
import pandas as pd
import sklearn
import lightgbm
import xgboost
import catboost
import pyarrow

# Quant foundry imports — same as the real training handler
print("[handler] importing quant_foundry...", flush=True)
sys.stdout.flush()
from quant_foundry.data_ingestion.quality_report import DatasetQualityReport
print("[handler] imported DatasetQualityReport", flush=True)
sys.stdout.flush()
from quant_foundry.dataset_manifest import FeatureLakeManifest, TrainingMode
print("[handler] imported FeatureLakeManifest, TrainingMode", flush=True)
sys.stdout.flush()
from quant_foundry.real_trainer import RealLightGBMTrainer, RealXGBoostTrainer
print("[handler] imported RealLightGBMTrainer, RealXGBoostTrainer", flush=True)
sys.stdout.flush()
from quant_foundry.runpod_training import RunPodTrainingHandler
print("[handler] imported RunPodTrainingHandler", flush=True)
sys.stdout.flush()
from quant_foundry.schemas import RunPodTrainingRequest
print("[handler] imported RunPodTrainingRequest", flush=True)
sys.stdout.flush()
from quant_foundry.signatures import sign_callback
print("[handler] imported sign_callback", flush=True)
sys.stdout.flush()
from quant_foundry.training_manifest import TrainingManifest
print("[handler] imported TrainingManifest", flush=True)
sys.stdout.flush()
from fincept_core.datasets import make_folds
print("[handler] imported make_folds", flush=True)
sys.stdout.flush()
print("[handler] ALL IMPORTS OK", flush=True)
sys.stdout.flush()


def handler(event):
    """Minimal handler that just returns a canary response."""
    input_data = event.get("input", {})
    task = input_data.get("task", "")
    job_id = input_data.get("job_id", "unknown")

    nonce = input_data.get("nonce", "")
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")

    callback_payload = json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "worker_id": "runpod-canary-heavy",
        "result_type": "callback_secret_canary",
        "payload": {"nonce": nonce},
    }, sort_keys=True)

    callback_ts = int(time.time())
    sig = hmac.new(secret.encode(), callback_payload.encode(), hashlib.sha256).hexdigest()

    return {
        "job_id": job_id,
        "callback_payload": callback_payload,
        "callback_signature": sig,
        "callback_ts": callback_ts,
        "status": "ok",
        "handler": "quant-foundry-imports",
        "versions": {
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "sklearn": sklearn.__version__,
            "lightgbm": lightgbm.__version__,
            "xgboost": xgboost.__version__,
            "catboost": catboost.__version__,
        },
    }


if __name__ == "__main__":
    try:
        import runpod
        runpod.serverless.start({"handler": handler})
    except ImportError:
        event = json.loads(sys.stdin.read())
        result = handler(event)
        sys.stdout.write(json.dumps(result, indent=2))
