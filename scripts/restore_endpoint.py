"""Restore training endpoint config to match working inference endpoint."""
import httpx
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import NETWORK_VOLUME_ID  # noqa: E402

api_key = os.environ["RUNPOD_API_KEY"]
eid = os.environ["RUNPOD_ENDPOINT_ID"]

# Purge workers, re-attach volume, enable flashboot, increase idle timeout
r = httpx.post(
    f"https://rest.runpod.io/v1/endpoints/{eid}/update",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "workersMax": 0,
        "flashboot": True,
        "networkVolumeId": NETWORK_VOLUME_ID,
        "idleTimeout": 300,
    },
    timeout=30.0,
)
print(f"Restore config: {r.status_code}")
d = r.json()
print(f"  flashboot: {d.get('flashboot')}")
print(f"  networkVolumeId: {d.get('networkVolumeId')}")
print(f"  idleTimeout: {d.get('idleTimeout')}")

print("Waiting 15s for workers to purge...")
time.sleep(15)

# Restore workersMax=1
r2 = httpx.post(
    f"https://rest.runpod.io/v1/endpoints/{eid}/update",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={"workersMax": 1},
    timeout=30.0,
)
print(f"Restore workers: {r2.status_code}")
