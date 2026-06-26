"""Get RunPod serverless worker pod logs."""
import httpx
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import TRAINING_ENDPOINT_ID  # noqa: E402

api_key = os.environ["RUNPOD_API_KEY"]
eid = sys.argv[1] if len(sys.argv) > 1 else TRAINING_ENDPOINT_ID

# Get pods for the endpoint
GET_PODS = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
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
    json={"query": GET_PODS, "variables": {"id": eid}},
    headers={"Content-Type": "application/json"},
    timeout=30.0,
)
data = r.json()
if "errors" in data:
    print(f"Error: {json.dumps(data['errors'], indent=2)}")
    sys.exit(1)

pods = data["data"]["myself"]["endpoint"]["pods"]
print(f"Pods: {json.dumps(pods, indent=2)}")

# Try to get logs for each pod
for pod in pods:
    pod_id = pod["id"]
    print(f"\n=== Logs for pod {pod_id} ===")

    # Try podLogs query
    LOGS_QUERY = """
    query($id: String!) {
      pod(id: $id) {
        logs(last: 100) {
          lines
        }
      }
    }
    """
    r2 = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": LOGS_QUERY, "variables": {"id": pod_id}},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    data2 = r2.json()
    if "errors" in data2:
        print(f"  podLogs error: {json.dumps(data2['errors'], indent=2)}")

        # Try REST API
        r3 = httpx.get(
            f"https://rest.runpod.io/v1/pods/{pod_id}/logs",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        print(f"  REST logs: {r3.status_code}")
        if r3.status_code == 200:
            print(f"  {r3.text[:3000]}")
        else:
            print(f"  {r3.text[:500]}")
    else:
        logs = data2["data"]["pod"]["logs"]
        print(f"  {json.dumps(logs, indent=2)[:3000]}")
