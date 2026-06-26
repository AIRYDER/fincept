"""Check RunPod pod runtime details."""
import httpx
import json
import os
import sys

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

# Query pods with all available fields
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
        desiredStatus
        image
        lastExitCode
        startedAt
        createdAt
        updatedAt
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
    # Try without fields that might not exist
    print(f"Errors: {json.dumps(data['errors'], indent=2)}")
    # Fallback: try minimal fields
    query2 = """
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
    r2 = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": query2, "variables": {"id": endpoint_id}},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    data2 = r2.json()
    if "errors" in data2:
        print(f"Fallback errors: {json.dumps(data2['errors'], indent=2)}")
    else:
        ep = data2["data"]["myself"]["endpoint"]
        print(f"Endpoint: {ep['name']}")
        for pod in ep.get("pods", []):
            print(f"  Pod: {json.dumps(pod, indent=4)}")
else:
    ep = data["data"]["myself"]["endpoint"]
    print(f"Endpoint: {ep['name']}")
    for pod in ep.get("pods", []):
        print(f"  Pod: {json.dumps(pod, indent=4)}")
