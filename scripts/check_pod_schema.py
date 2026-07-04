"""Check RunPod pod creation schema."""

import os

import requests

KEY = os.environ["RUNPOD_API_KEY"]
r = requests.get(
    "https://rest.runpod.io/v1/openapi.json",
    headers={"Authorization": f"Bearer {KEY}"},
    timeout=15,
)
spec = r.json()

# Find pod creation schema
paths = spec.get("paths", {})
pod_post = paths.get("/pods", {}).get("post", {})
request_body = pod_post.get("requestBody", {})
content = request_body.get("content", {})
app_json = content.get("application/json", {})
schema_ref = app_json.get("schema", {}).get("$ref", "")
print(f"Schema ref: {schema_ref}")

# Resolve the schema
components = spec.get("components", {})
schemas = components.get("schemas", {})

# Get the schema name from the ref
if schema_ref:
    schema_name = schema_ref.split("/")[-1]
    schema = schemas.get(schema_name, {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    print(f"\nSchema: {schema_name}")
    print(f"Required: {required}")
    print("\nProperties:")
    for k, v in sorted(props.items()):
        ptype = v.get("type", "?")
        desc = v.get("description", "")[:100]
        default = v.get("default", "")
        print(f"  {k}: type={ptype} default={default} desc={desc}")
