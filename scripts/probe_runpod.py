"""Probe RunPod training endpoint with a minimal job."""
import httpx
import json
import os
import sys

api_key = os.environ["RUNPOD_API_KEY"]
eid = os.environ["RUNPOD_ENDPOINT_ID"]

r = httpx.post(
    f"https://api.runpod.ai/v2/{eid}/runsync",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "input": {
            "job_id": "qf:probe:3",
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
    # Redact callback payload if present (may contain secrets)
    if "callback_payload" in output:
        cp = output["callback_payload"]
        if isinstance(cp, str):
            try:
                parsed = json.loads(cp)
                if "callback_secret" in parsed:
                    parsed["callback_secret"] = "***REDACTED***"
                output["callback_payload"] = json.dumps(parsed)
            except Exception:
                output["callback_payload"] = "***REDACTED***"
    print(f"Output: {json.dumps(output, indent=2)[:3000]}")
else:
    print(f"Output: {output}")
print(f"Error: {d.get('error', 'none')}")
