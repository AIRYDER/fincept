"""Check full template config for stale fields."""
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
      template {
        id
        name
        imageName
        dockerArgs
        startScript
        advancedStart
        config
        isServerless
        volumeMountPath
        containerRegistryAuthId
        env { key value }
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
else:
    template = data["data"]["myself"]["endpoint"]["template"]
    for e in template.get("env", []):
        if "SECRET" in e.get("key", ""):
            e["value"] = "***REDACTED***"
    print(json.dumps(template, indent=2))
