"""Minimal training handler for debugging — no heavy imports."""
import json
import time
import hashlib
import hmac
import os


def handler(event):
    """Minimal handler that just returns a canary response."""
    input_data = event.get("input", {})
    task = input_data.get("task", "")
    job_id = input_data.get("job_id", "unknown")
    
    # Simple canary response
    nonce = input_data.get("nonce", "")
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    
    callback_payload = json.dumps({
        "schema_version": 1,
        "job_id": job_id,
        "worker_id": "runpod-canary-minimal",
        "result_type": "callback_secret_canary",
        "payload": {"nonce": nonce},
    }, sort_keys=True)
    
    callback_ts = int(time.time())
    msg = f"{callback_ts}:{job_id}".encode()
    sig = hmac.new(secret.encode(), callback_payload.encode(), hashlib.sha256).hexdigest()
    
    return {
        "job_id": job_id,
        "callback_payload": callback_payload,
        "callback_signature": sig,
        "callback_ts": callback_ts,
        "status": "ok",
        "handler": "minimal",
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
