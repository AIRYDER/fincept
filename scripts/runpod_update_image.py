"""Update RunPod template to point to the new container image."""
import json
import os
import requests

KEY = os.environ["RUNPOD_API_KEY"]
LOCAL_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
TEMPLATE_ID = "me58r5vdrp"
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# New image SHA from the git push
NEW_SHA = "598b2a81a7f9e16f2291478211a545b38194d37a"
NEW_IMAGE = f"ghcr.io/airyder/fincept/quant-foundry-training:{NEW_SHA}"

# Also check if :latest tag works
LATEST_IMAGE = "ghcr.io/airyder/fincept/quant-foundry-training:latest"

# 1. Get current template
r = requests.get(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, timeout=15)
tmpl = r.json()
current_image = tmpl.get("imageName", "")
print(f"Current image: {current_image}")

# 2. Update to new image
new_env = tmpl.get("env", {})
new_env["QUANT_FOUNDRY_CALLBACK_SECRET"] = LOCAL_SECRET
new_env["QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS"] = "1800"
new_env["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

patch_body = {
    "name": tmpl.get("name"),
    "imageName": NEW_IMAGE,
    "containerDiskInGb": tmpl.get("containerDiskInGb", 20),
    "env": new_env,
    "volumeMountPath": tmpl.get("volumeMountPath", "/workspace"),
}

print(f"\nUpdating template to new image:")
print(f"  Image: {NEW_IMAGE}")
print(f"  Env:")
for k, v in new_env.items():
    display = v[:20] + "..." if len(str(v)) > 25 else v
    print(f"    {k}: {display}")

r2 = requests.patch(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, json=patch_body, timeout=30)
print(f"\nPATCH: {r2.status_code}")
if r2.status_code == 200:
    result = r2.json()
    print(f"  New image: {result.get('imageName')}")
    print("  SUCCESS - template updated with new image")
else:
    print(f"  Error: {r2.text[:500]}")

# 3. Also try to trigger an endpoint update to pick up the new template
print("\n--- Triggering endpoint update ---")
EP = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
# The endpoint should automatically pick up template changes, but let's verify
r3 = requests.get(f"{BASE}/endpoints/{EP}", headers=HEADERS, timeout=15)
if r3.status_code == 200:
    ep_data = r3.json()
    print(f"  Endpoint templateId: {ep_data.get('templateId')}")
    print(f"  Endpoint version: {ep_data.get('version')}")
