"""Check RunPod endpoint pods/workers for errors."""
import httpx
import json
import os

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = os.environ["RUNPOD_ENDPOINT_ID"]

# Use introspection to find Pod fields
query = """
query {
  __type(name: "Pod") {
    fields {
      name
      type { name kind }
    }
  }
}
"""
r = httpx.post(
    f"https://api.runpod.io/graphql?api_key={api_key}",
    json={"query": query},
    headers={"Content-Type": "application/json"},
    timeout=30.0,
)
data = r.json()
if "errors" in data:
    print(f"Errors: {json.dumps(data['errors'], indent=2)}")
else:
    fields = data["data"]["__type"]["fields"]
    print("Pod fields:")
    for f in fields:
        print(f"  {f['name']}: {f['type']['name'] or f['type']['kind']}")

# Now query pods with correct fields
query2 = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      pods {
        id
        name
        desiredStatus
        machineId
        dockerId
        runtime {
          status
          ports
          gpus
          containerDiskPath
          volumes
        }
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
    print(f"\nEndpoint pods errors: {json.dumps(data2['errors'], indent=2)}")
else:
    ep = data2["data"]["myself"]["endpoint"]
    print(f"\nEndpoint: {ep['name']}")
    for pod in ep.get("pods", []):
        print(f"  Pod: {json.dumps(pod, indent=4)}")
