"""Clear dockerArgs on both endpoint templates."""
import httpx
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import INFERENCE_ENDPOINT_ID, TRAINING_ENDPOINT_ID  # noqa: E402

api_key = os.environ["RUNPOD_API_KEY"]

SAVE_TEMPLATE = """
mutation($input: SaveTemplateInput!) {
  saveTemplate(input: $input) {
    id
    name
    imageName
    dockerArgs
  }
}
"""

GET_TEMPLATE = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      template {
        id
        name
        imageName
        dockerArgs
        containerDiskInGb
        volumeInGb
        isServerless
        env { key value }
        volumeMountPath
        config
        category
        containerRegistryAuthId
        isPublic
        ports
        readme
        startScript
        advancedStart
      }
    }
  }
}
"""

for name, eid in [
    ("training", TRAINING_ENDPOINT_ID),
    ("inference", INFERENCE_ENDPOINT_ID),
]:
    print(f"\n=== Clearing dockerArgs for {name} ({eid}) ===")

    # 1. Fetch current template
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
    print(f"  Template: {template['id']}, dockerArgs: {template.get('dockerArgs')}")

    # 2. Build template input with dockerArgs cleared
    merged_env = template.get("env", [])
    template_input = {
        "id": template["id"],
        "name": template["name"],
        "imageName": template["imageName"],
        "containerDiskInGb": template.get("containerDiskInGb", 5),
        "volumeInGb": template.get("volumeInGb", 0),
        "isServerless": template.get("isServerless", True),
        "env": merged_env,
        "dockerArgs": "",  # Clear dockerArgs so ENTRYPOINT is used
    }

    # Preserve optional fields
    for field in ["volumeMountPath", "config", "category", "containerRegistryAuthId",
                  "isPublic", "ports", "readme", "startScript", "advancedStart"]:
        if field in template and template[field] is not None:
            template_input[field] = template[field]

    # 3. Save template
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
        print(f"  Saved: id={result['id']}, dockerArgs={result.get('dockerArgs')}")
