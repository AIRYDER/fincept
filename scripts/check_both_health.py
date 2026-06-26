"""Check health of both RunPod endpoints."""
import httpx
import json
import os

api_key = os.environ["RUNPOD_API_KEY"]
for name, eid in [
    ("training", os.environ["RUNPOD_ENDPOINT_ID"]),
    ("inference", os.environ["RUNPOD_INFERENCE_ENDPOINT_ID"]),
]:
    r = httpx.get(
        f"https://api.runpod.ai/v2/{eid}/health",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    print(f"{name}: {json.dumps(r.json())}")
