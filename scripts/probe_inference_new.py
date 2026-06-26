"""Probe the new RunPod inference endpoint with a single job (async + poll)."""
import httpx
import json
import os
import time

api_key = os.environ["RUNPOD_API_KEY"]
eid = "t31u1z426jy1ub"  # new inference endpoint
H = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

symbols = ["SYM_A", "SYM_B", "SYM_C"]
payload = {
    "request": {
        "job_id": "qf:probe:infer:new:2",
        "artifact_ref": "",
        "symbols": symbols,
        "horizons_ns": [3_600_000_000_000],
    },
    "snapshot": {
        "symbols": symbols,
        "features": {
            "SYM_A": [0.1, 0.2, 0.3, 0.4],
            "SYM_B": [-0.1, 0.5, -0.3, 0.2],
            "SYM_C": [0.0, -0.2, 0.4, -0.1],
        },
        "availability": {s: True for s in symbols},
        "ts_event": int(time.time() * 1_000_000_000),
        "freshness_ns": 500,
    },
    "model_id": "model:probe",
}


def health():
    r = httpx.get(f"https://api.runpod.ai/v2/{eid}/health", headers=H, timeout=30.0)
    return r.json().get("workers", {})


print(f"Health before dispatch: {json.dumps(health())}")
r = httpx.post(f"https://api.runpod.ai/v2/{eid}/run", headers=H, json={"input": payload}, timeout=60.0)
job_id = r.json().get("id")
print(f"Dispatched: {job_id}")

for i in range(30):
    time.sleep(10)
    r = httpx.get(f"https://api.runpod.ai/v2/{eid}/status/{job_id}", headers=H, timeout=30.0)
    d = r.json()
    state = d.get("status")
    print(f"[{i+1}/30] {state}  workers={json.dumps(health())}")
    if state == "COMPLETED":
        out = d.get("output", {})
        for k in ("callback_payload", "callback_signature"):
            if isinstance(out, dict) and k in out:
                out[k] = "***REDACTED***"
        print(f"Output: {json.dumps(out, indent=2)[:2500]}")
        break
    if state == "FAILED":
        print(f"FAILED: {d.get('error')}")
        break
