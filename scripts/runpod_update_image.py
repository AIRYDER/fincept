"""Update RunPod template to point to the new container image."""
import json
import os
import subprocess
import requests

KEY = os.environ["RUNPOD_API_KEY"]
LOCAL_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
TEMPLATE_ID = "me58r5vdrp"
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

full_sha = subprocess.check_output(
    ["git", "rev-parse", "HEAD"],
    cwd="C:/Users/nolan/CascadeProjects/fincept-terminal",
).decode().strip()
NEW_IMAGE = f"ghcr.io/airyder/fincept/quant-foundry-training:{full_sha}"

r = requests.get(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, timeout=15)
tmpl = r.json()
print(f"Current image: {tmpl.get('imageName')}")

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

print(f"Updating to: {NEW_IMAGE}")

r2 = requests.patch(f"{BASE}/templates/{TEMPLATE_ID}", headers=HEADERS, json=patch_body, timeout=30)
print(f"PATCH: {r2.status_code}")
if r2.status_code == 200:
    result = r2.json()
    print(f"New image: {result.get('imageName')}")
    print("SUCCESS")
else:
    print(f"Error: {r2.text[:500]}")
