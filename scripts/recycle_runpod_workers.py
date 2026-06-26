"""Force-recycle RunPod endpoint workers by toggling workersMax."""
import httpx
import json
import os
import sys
import time

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

query = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      workersMin
      workersMax
    }
  }
}
"""

update_query = """
mutation($id: String!, $workersMin: Int!, $workersMax: Int!) {
  updateEndpoint(input: {id: $id, workersMin: $workersMin, workersMax: $workersMax}) {
    id
    workersMin
    workersMax
  }
}
"""

def graphql(query, variables):
    r = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]

# 1. Fetch current state
result = graphql(query, {"id": endpoint_id})
ep = result["myself"]["endpoint"]
print(f"Endpoint: {ep['name']} ({ep['id']})")
print(f"  Current: workersMin={ep['workersMin']}, workersMax={ep['workersMax']}")

# 2. Set workersMax=0 to kill all workers
print("  Setting workersMax=0 to purge workers...")
graphql(update_query, {"id": endpoint_id, "workersMin": 0, "workersMax": 0})
time.sleep(10)

# 3. Check health
r = httpx.get(
    f"https://api.runpod.ai/v2/{endpoint_id}/health",
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=30.0,
)
print(f"  Health after purge: {json.dumps(r.json())}")

# 4. Restore workersMax=1
print("  Restoring workersMax=1...")
graphql(update_query, {"id": endpoint_id, "workersMin": 0, "workersMax": 1})

time.sleep(5)
r = httpx.get(
    f"https://api.runpod.ai/v2/{endpoint_id}/health",
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=30.0,
)
print(f"  Health after restore: {json.dumps(r.json())}")
