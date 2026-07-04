"""Check full endpoint config via REST API."""

import json
import os
import sys

import httpx

api_key = os.environ["RUNPOD_API_KEY"]
endpoint_id = sys.argv[1] if len(sys.argv) > 1 else os.environ["RUNPOD_ENDPOINT_ID"]

r = httpx.get(
    f"https://rest.runpod.io/v1/endpoints/{endpoint_id}",
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=30.0,
)
d = r.json()
# Redact secrets
template = d.get("template", {})
for e in template.get("env", []):
    if "SECRET" in e.get("key", ""):
        e["value"] = "***REDACTED***"
print(json.dumps(d, indent=2))
