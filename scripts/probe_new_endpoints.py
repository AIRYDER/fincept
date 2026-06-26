"""Probe the new RunPod training endpoint."""
import httpx
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import TRAINING_ENDPOINT_ID  # noqa: E402

api_key = os.environ["RUNPOD_API_KEY"]
eid = TRAINING_ENDPOINT_ID

r = httpx.post(
    f"https://api.runpod.ai/v2/{eid}/runsync",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "input": {
            "job_id": "qf:probe:new:1",
            "dataset_manifest_ref": "probe",
            "model_family": "gbm",
            "search_space": {"n_estimators": [10]},
            "random_seed": 42,
            "hardware_class": "cpu",
            "extra_constraints": {},
        }
    },
    timeout=120.0,
)
print(f"Status: {r.status_code}")
d = r.json()
print(f"Job status: {d.get('status')}")
output = d.get("output", {})
if isinstance(output, dict):
    print(f"Output keys: {list(output.keys())}")
    if "callback_payload" in output:
        output["callback_payload"] = "***REDACTED***"
    if "callback_signature" in output:
        output["callback_signature"] = "***REDACTED***"
    print(f"Output: {json.dumps(output, indent=2)[:3000]}")
else:
    print(f"Output: {output}")
print(f"Error: {d.get('error', 'none')}")
