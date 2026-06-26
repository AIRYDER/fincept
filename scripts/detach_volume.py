"""Detach network volume from RunPod endpoint and purge workers."""
import httpx
import json
import os
import sys
import time

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

# 1. Purge workers and detach volume
print(f"Detaching network volume from endpoint {endpoint_id}...")
r = httpx.post(
    f"https://rest.runpod.io/v1/endpoints/{endpoint_id}/update",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={"workersMax": 0, "networkVolumeIds": []},
    timeout=30.0,
)
print(f"  Status: {r.status_code}")
d = r.json()
print(f"  networkVolumeId: {d.get('networkVolumeId', 'NOT SET')}")
print(f"  networkVolumeIds: {d.get('networkVolumeIds', 'NOT SET')}")

# 2. Wait for workers to purge
print("  Waiting 15s for workers to purge...")
time.sleep(15)

# 3. Restore workersMax=1
print("  Restoring workersMax=1...")
r2 = httpx.post(
    f"https://rest.runpod.io/v1/endpoints/{endpoint_id}/update",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={"workersMax": 1},
    timeout=30.0,
)
print(f"  Status: {r2.status_code}")
d2 = r2.json()
print(f"  networkVolumeId: {d2.get('networkVolumeId', 'NOT SET')}")
print(f"  networkVolumeIds: {d2.get('networkVolumeIds', 'NOT SET')}")
