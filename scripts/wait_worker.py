"""Wait for RunPod endpoint worker to become ready."""

import json
import os
import sys
import time

import httpx

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

for i in range(30):
    time.sleep(10)
    r = httpx.get(
        f"https://api.runpod.ai/v2/{endpoint_id}/health",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    data = r.json()
    workers = data.get("workers", {})
    print(f"[{i + 1}/30] workers: {json.dumps(workers)}")
    if workers.get("ready", 0) > 0 or workers.get("running", 0) > 0:
        print("Worker is ready/running!")
        sys.exit(0)
    if workers.get("unhealthy", 0) > 0:
        print("Worker is unhealthy!")
        sys.exit(1)

print("Worker did not become ready within 300s")
sys.exit(1)
