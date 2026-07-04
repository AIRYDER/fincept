"""Fetch RunPod pod logs for the training endpoint."""

import json
import os

import httpx

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = os.environ["RUNPOD_ENDPOINT_ID"]

# Query pods for the endpoint
query = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      pods {
        id
        name
        dockerId
        machineId
      }
    }
  }
}
"""

r = httpx.post(
    f"https://api.runpod.io/graphql?api_key={api_key}",
    json={"query": query, "variables": {"id": endpoint_id}},
    headers={"Content-Type": "application/json"},
    timeout=30.0,
)
data = r.json()
if "errors" in data:
    print(f"Errors: {json.dumps(data['errors'], indent=2)}")
    exit(1)

ep = data["data"]["myself"]["endpoint"]
pods = ep.get("pods", [])
print(f"Endpoint: {ep['name']} ({ep['id']})")
print(f"Pods: {len(pods)}")
for pod in pods:
    print(f"  Pod: {json.dumps(pod, indent=4)}")

    # Try to get pod logs
    pod_id = pod["id"]
    print(f"  Fetching logs for pod {pod_id}...")

    # Try REST API for pod logs
    r2 = httpx.get(
        f"https://rest.runpod.io/v1/pods/{pod_id}/logs",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    print(f"  Logs status: {r2.status_code}")
    if r2.status_code == 200:
        logs = r2.text[:5000]
        print(f"  Logs: {logs}")
    else:
        print(f"  Logs error: {r2.text[:500]}")

    # Also try the pod logs endpoint
    r3 = httpx.get(
        f"https://api.runpod.io/v1/pods/{pod_id}/logs",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    print(f"  Alt logs status: {r3.status_code}")
    if r3.status_code == 200:
        print(f"  Alt logs: {r3.text[:5000]}")
    else:
        print(f"  Alt logs error: {r3.text[:500]}")
