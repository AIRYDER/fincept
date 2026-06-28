"""Manage RunPod endpoint: get details, update env vars, create network volume."""
import json
import os
import sys
import requests

KEY = os.environ["RUNPOD_API_KEY"]
EP = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

if cmd == "status":
    # Get endpoint details
    r = requests.get(f"{BASE}/endpoints/{EP}", headers=HEADERS, timeout=15)
    print(f"Endpoint GET: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(json.dumps(data, indent=2))
    else:
        print(r.text[:500])

elif cmd == "patch":
    # Update endpoint env vars
    # First get current config
    r = requests.get(f"{BASE}/endpoints/{EP}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"GET failed: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    current = r.json()
    print(f"Current endpoint: {json.dumps({k: current.get(k) for k in ['id', 'name', 'templateId']}, indent=2)}")

    # Build the patch payload with updated env vars
    callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    new_env = {
        "QUANT_FOUNDRY_CALLBACK_SECRET": callback_secret,
        "QUANT_FOUNDRY_USE_REAL_TRAINER": "true",
        "QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS": "1800",  # 30 minutes
    }

    # Preserve existing env vars
    existing_env = current.get("env", {})
    if isinstance(existing_env, dict):
        new_env.update(existing_env)
    # Override with our values
    new_env["QUANT_FOUNDRY_CALLBACK_SECRET"] = callback_secret
    new_env["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"
    new_env["QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS"] = "1800"

    # Build patch payload — include all required fields
    patch_body = {
        "name": current.get("name", "quant-foundry-training"),
        "templateId": current.get("templateId"),
        "env": new_env,
    }
    # Preserve other fields
    for field in ["gpuIds", "workersMin", "workersMax", "autoscalingEnabled", "flashbootEnabled",
                  "containerDiskSizeGb", "volumeMountPath", "networkVolumeId"]:
        if field in current and current[field] is not None:
            patch_body[field] = current[field]

    print(f"\nPatching endpoint with env vars:")
    for k, v in new_env.items():
        display = v[:16] + "..." if len(v) > 20 else v
        print(f"  {k}: {display}")

    r2 = requests.patch(f"{BASE}/endpoints/{EP}", headers=HEADERS, json=patch_body, timeout=30)
    print(f"\nPATCH: {r2.status_code}")
    if r2.status_code == 200:
        result = r2.json()
        print(f"Updated env: {json.dumps(result.get('env', {}), indent=2)}")
        print("SUCCESS — endpoint updated")
    else:
        print(f"Error: {r2.text[:500]}")

elif cmd == "volumes":
    # List network volumes
    r = requests.get(f"{BASE}/network-volumes", headers=HEADERS, timeout=15)
    print(f"Network Volumes: {r.status_code}")
    print(json.dumps(r.json(), indent=2)[:2000])

elif cmd == "create-volume":
    # Create a network volume for dataset storage
    r = requests.post(f"{BASE}/network-volumes", headers=HEADERS, json={
        "name": "quant-foundry-datasets",
        "sizeGb": 10,  # 10 GB is enough for datasets
        "region": "US",  # adjust as needed
    }, timeout=30)
    print(f"Create Volume: {r.status_code}")
    print(json.dumps(r.json(), indent=2))

elif cmd == "templates":
    # List templates
    r = requests.get(f"{BASE}/templates", headers=HEADERS, timeout=15)
    print(f"Templates: {r.status_code}")
    if r.status_code == 200:
        templates = r.json()
        for t in templates if isinstance(templates, list) else [templates]:
            print(f"  id: {t.get('id')}, name: {t.get('name')}")
    else:
        print(r.text[:500])

else:
    print(f"Unknown command: {cmd}")
    print("Usage: python runpod_manage.py [status|patch|volumes|create-volume|templates]")
