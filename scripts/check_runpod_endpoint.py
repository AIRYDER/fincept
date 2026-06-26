"""Check RunPod endpoint configuration."""
import httpx
import json
import os
import sys

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

query = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      gpuIds
      workersMin
      workersMax
      env { key value }
      template {
        id
        name
        imageName
        containerDiskInGb
        volumeInGb
        volumeMountPath
        containerRegistryAuthId
        dockerArgs
        config
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
ep = data["data"]["myself"]["endpoint"]
for e in (ep.get("env") or []):
    if "SECRET" in e.get("key", ""):
        e["value"] = "***REDACTED***"
print(json.dumps(ep, indent=2))
