"""Minimal handler with heavy ML imports to test OOM hypothesis."""
import json
import time
import hashlib
import hmac
import os

# Heavy ML imports — same as the real training handler
import numpy as np
import pandas as pd
import sklearn
import lightgbm
import xgboost
import catboost
import pyarrow


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
        "handler": "heavy-imports",
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
    import sys
    try:
        import runpod
        runpod.serverless.start({"handler": handler})
    except ImportError:
        event = json.loads(sys.stdin.read())
        result = handler(event)
        sys.stdout.write(json.dumps(result, indent=2))
