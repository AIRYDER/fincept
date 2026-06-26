"""Update endpoint template to use a specific SHA tag instead of :latest."""
import httpx
import json
import os
import sys

api_key = os.environ["RUNPOD_API_KEY"]
sha = sys.argv[1] if len(sys.argv) > 1 else "8a74c133380ecebfa7b685f7a7a22e6cba23f644"

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
        id name imageName dockerArgs containerDiskInGb volumeInGb
        isServerless env { key value } volumeMountPath config category
        containerRegistryAuthId isPublic ports readme startScript advancedStart
      }
    }
  }
}
"""

for name, eid, suffix in [
    ("training", "h2blqodcicxqyy", "quant-foundry-training"),
    ("inference", "t31u1z426jy1ub", "quant-foundry-inference"),
]:
    print(f"\n=== Updating {name} ({eid}) ===")

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
    new_image = f"ghcr.io/airyder/fincept/{suffix}:{sha}"
    print(f"  Old image: {template['imageName']}")
    print(f"  New image: {new_image}")

    template_input = {
        "id": template["id"],
        "name": template["name"],
        "imageName": new_image,
        "containerDiskInGb": template.get("containerDiskInGb", 5),
        "volumeInGb": template.get("volumeInGb", 0),
        "isServerless": template.get("isServerless", True),
        "env": template.get("env", []),
        "dockerArgs": "",
    }

    for field in ["volumeMountPath", "config", "category", "containerRegistryAuthId",
                  "isPublic", "ports", "readme", "startScript", "advancedStart"]:
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
        print(f"  Saved: id={result['id']}, image={result['imageName']}")
