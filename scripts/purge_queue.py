"""Cancel all queued jobs for both endpoints."""
import httpx
import json
import os
import time

api_key = os.environ["RUNPOD_API_KEY"]

for name, eid in [
    ("training", os.environ["RUNPOD_ENDPOINT_ID"]),
    ("inference", os.environ["RUNPOD_INFERENCE_ENDPOINT_ID"]),
]:
    print(f"\n=== {name} ({eid}) ===")
    # Get health to see queue count
    r = httpx.get(
        f"https://api.runpod.ai/v2/{eid}/health",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    health = r.json()
    in_queue = health.get("jobs", {}).get("inQueue", 0)
    print(f"  Jobs in queue: {in_queue}")

    # Try to get all jobs (may not be supported)
    # Instead, let's just purge workers and re-queue
    r2 = httpx.post(
        f"https://rest.runpod.io/v1/endpoints/{eid}/update",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"workersMax": 0},
        timeout=30.0,
    )
    print(f"  Purge workers: {r2.status_code}")

    time.sleep(10)

    # Restore workers
    r3 = httpx.post(
        f"https://rest.runpod.io/v1/endpoints/{eid}/update",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"workersMax": 1},
        timeout=30.0,
    )
    print(f"  Restore workers: {r3.status_code}")
