"""Canary test to verify callback secret parity between local env and RunPod."""
import json
import os
import sys
import time
import requests

sys.path.insert(0, "services/quant_foundry/src")
from quant_foundry.signatures import verify_callback

key = os.environ["RUNPOD_API_KEY"]
base = os.environ["RUNPOD_BASE_URL"]
ep = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
local_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")

# Dispatch canary
canary_input = {
    "task": "callback_secret_canary",
    "job_id": "canary-test-001",
    "nonce": "test-nonce-12345",
}
r = requests.post(
    f"{base}/{ep}/run",
    headers={"Authorization": f"Bearer {key}"},
    json={"input": canary_input},
    timeout=30,
)
print(f"Dispatch: {r.status_code}")
job_id = r.json().get("id")
print(f"Job ID: {job_id}")

# Poll
time.sleep(5)
r2 = requests.get(
    f"{base}/{ep}/status/{job_id}",
    headers={"Authorization": f"Bearer {key}"},
    timeout=15,
)
result = r2.json()
status = result.get("status")
print(f"Status: {status}")
output = result.get("output", {})
print(f"Output keys: {list(output.keys())}")
print(f"Canary: {output.get('canary')}")
print(f"Nonce: {output.get('nonce')}")

# Verify signature
if "callback_signature" in output:
    payload_str = output.get("callback_payload", "")
    if isinstance(payload_str, bytes):
        payload = payload_str
    else:
        payload = payload_str.encode("utf-8")
    sig = output["callback_signature"]
    ts = int(output.get("callback_ts", 0))
    print(f"Callback ts: {ts}")
    print(f"Signature: {sig[:32]}...")
    valid = verify_callback(payload, secret=local_secret, signature=sig, ts=ts, job_id="canary-test-001")
    print(f"HMAC valid with local secret: {valid}")
    if not valid:
        print("The RunPod container has a DIFFERENT callback secret than local env.")
        print("To fix: update the RunPod template env var QUANT_FOUNDRY_CALLBACK_SECRET")
        print(f"to match: {local_secret[:8]}...")
else:
    print("No callback_signature in output — canary may have failed")
    print(f"Full output: {json.dumps(output, indent=2)}")
