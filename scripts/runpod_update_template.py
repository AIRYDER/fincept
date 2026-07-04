"""Update RunPod template env vars and check network volume."""

import json
import os

import requests

KEY = os.environ["RUNPOD_API_KEY"]
LOCAL_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
TEMPLATE_ID = "me58r5vdrp"
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# 1. Get current template
r = requests.get(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, timeout=15)
tmpl = r.json()
print("Current template env:")
for k, v in tmpl.get("env", {}).items():
    display = v[:20] + "..." if len(str(v)) > 25 else v
    print(f"  {k}: {display}")

# 2. Update env vars
new_env = tmpl.get("env", {})
new_env["QUANT_FOUNDRY_CALLBACK_SECRET"] = LOCAL_SECRET
new_env["QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS"] = "1800"
new_env["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

patch_body = {
    "name": tmpl.get("name"),
    "imageName": tmpl.get("imageName"),
    "containerDiskInGb": tmpl.get("containerDiskInGb", 20),
    "env": new_env,
    "volumeMountPath": tmpl.get("volumeMountPath", "/workspace"),
}

print("\nUpdating template with new env:")
for k, v in new_env.items():
    display = v[:20] + "..." if len(str(v)) > 25 else v
    print(f"  {k}: {display}")

r2 = requests.patch(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, json=patch_body, timeout=30)
print(f"\nPATCH result: {r2.status_code}")
if r2.status_code == 200:
    result = r2.json()
    print("Updated env:")
    for k, v in result.get("env", {}).items():
        display = v[:20] + "..." if len(str(v)) > 25 else v
        print(f"  {k}: {display}")
    print("\nSUCCESS - template updated")
else:
    print(f"Error: {r2.text[:500]}")

# 3. Check network volume
print("\n--- Network Volume ---")
for vol_path in [f"{BASE}/network-volumes", "https://rest.runpod.io/v1/network-volumes"]:
    r3 = requests.get(vol_path, headers=HEADERS, timeout=15)
    print(f"GET {vol_path}: {r3.status_code}")
    if r3.status_code == 200:
        vols = r3.json()
        print(json.dumps(vols, indent=2)[:1500])
        break
    else:
        print(f"  {r3.text[:200]}")
