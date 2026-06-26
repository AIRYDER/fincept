"""Probe RunPod inference endpoint with a minimal job."""
import httpx
import json
import os

api_key = os.environ["RUNPOD_API_KEY"]
eid = os.environ["RUNPOD_INFERENCE_ENDPOINT_ID"]

r = httpx.post(
    f"https://api.runpod.ai/v2/{eid}/runsync",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "input": {
            "request": {
                "job_id": "qf:probe:infer:1",
                "artifact_ref": "",
                "symbols": ["SYM_A"],
                "horizons_ns": [3600000000000],
            },
            "snapshot": {
                "symbols": ["SYM_A"],
                "features": {"SYM_A": [0.1, 0.2, 0.3, 0.4]},
                "availability": {"SYM_A": True},
                "ts_event": 1700000000000000000,
                "freshness_ns": 500,
            },
            "model_id": "probe",
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
    print(f"Output: {json.dumps(output, indent=2)[:2000]}")
else:
    print(f"Output: {output}")
print(f"Error: {d.get('error', 'none')}")
