"""Link a RunPod container registry credential to the endpoint templates.

The ghcr.io images are private (published from a private repo). RunPod
needs a registry credential to pull them. The templates had
containerRegistryAuthId=null, so workers crashed on image pull.

This script links an existing container registry credential to both
templates. The credential IDs come from:
  query { myself { containerRegistryCreds { id name } } }
"""
import httpx
import json
import os
import sys

api_key = os.environ["RUNPOD_API_KEY"]

# Default to ghcr.io-fincept; override via argv[1]
auth_id = sys.argv[1] if len(sys.argv) > 1 else "cmqu7l5rz0047nzyt0o28je3d"

SAVE_TEMPLATE = """
mutation($input: SaveTemplateInput!) {
  saveTemplate(input: $input) {
    id
    name
    imageName
    containerRegistryAuthId
  }
}
"""

GET_TEMPLATE = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      template {
        id name imageName dockerArgs containerDiskInGb volumeInGb
        isServerless env { key value } volumeMountPath config category
        containerRegistryAuthId isPublic ports readme startScript advancedStart
      }
    }
  }
}
"""

for name, eid in [
    ("training", "h2blqodcicxqyy"),
    ("inference", "t31u1z426jy1ub"),
]:
    print(f"\n=== Linking registry auth for {name} ({eid}) ===")

    r = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": GET_TEMPLATE, "variables": {"id": eid}},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    data = r.json()
    if "errors" in data:
        print(f"  Fetch error: {json.dumps(data['errors'], indent=2)}")
        continue

    template = data["data"]["myself"]["endpoint"]["template"]
    print(f"  Template: {template['id']}, current auth: {template.get('containerRegistryAuthId')}")

    template_input = {
        "id": template["id"],
        "name": template["name"],
        "imageName": template["imageName"],
        "containerDiskInGb": template.get("containerDiskInGb", 5),
        "volumeInGb": template.get("volumeInGb", 0),
        "isServerless": template.get("isServerless", True),
        "env": template.get("env", []),
        "dockerArgs": template.get("dockerArgs", "") or "",
        "containerRegistryAuthId": auth_id,
    }

    for field in ["volumeMountPath", "config", "category", "isPublic",
                  "ports", "readme", "startScript", "advancedStart"]:
        if field in template and template[field] is not None:
            template_input[field] = template[field]

    r2 = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": SAVE_TEMPLATE, "variables": {"input": template_input}},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    data2 = r2.json()
    if "errors" in data2:
        print(f"  Save error: {json.dumps(data2['errors'], indent=2)}")
    else:
        result = data2["data"]["saveTemplate"]
        print(f"  Saved: id={result['id']}, auth={result.get('containerRegistryAuthId')}")
