"""Delete and recreate RunPod endpoints to fix scheduler state corruption.

The RunPod scheduler's worker<->endpoint mapping gets corrupted after
repeated REST PATCH calls (version > ~10). The fix is to delete and
recreate the endpoints, which resets the version field and scheduler state.

See: https://happyin.space/devops/runpod-serverless-stuck-queue-idle-workers/
"""
import httpx
import json
import os
import time
import sys

# Template IDs (from previous queries)
TRAINING_TEMPLATE_ID = "me58r5vdrp"
INFERENCE_TEMPLATE_ID = "wnasp3v5jn"

# Endpoint names
TRAINING_NAME = "fincept-qf-training"
INFERENCE_NAME = "fincept-qf-inference"

# GPU type
GPU_TYPE = "NVIDIA GeForce RTX 4090"


def graphql(api_key, query, variables=None):
    r = httpx.post(
        f"https://api.runpod.io/graphql?api_key={api_key}",
        json={"query": query, "variables": variables or {}},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def main() -> None:
    """Delete and recreate both RunPod endpoints."""
    api_key = os.environ["RUNPOD_API_KEY"]

    # Step 1: Delete both endpoints
    for name, eid in [
        ("training", os.environ["RUNPOD_ENDPOINT_ID"]),
        ("inference", os.environ["RUNPOD_INFERENCE_ENDPOINT_ID"]),
    ]:
        print(f"\n=== Deleting {name} endpoint ({eid}) ===")
        # First set workersMax=0 to purge workers
        r = httpx.post(
            f"https://rest.runpod.io/v1/endpoints/{eid}/update",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"workersMax": 0},
            timeout=30.0,
        )
        print(f"  Purge workers: {r.status_code}")
        time.sleep(5)

        # Delete via REST API
        r2 = httpx.delete(
            f"https://rest.runpod.io/v1/endpoints/{eid}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        print(f"  Delete: {r2.status_code} {r2.text[:200]}")

    # Step 2: Wait for deletion to complete
    print("\n=== Waiting 15s for deletion to complete ===")
    time.sleep(15)

    # Step 3: Recreate endpoints using GraphQL
    CREATE_QUERY = """
mutation($input: EndpointInput!) {
  createEndpoint(input: $input) {
    id
    name
    templateId
  }
}
"""

    for name, template_id, display_name in [
        ("training", TRAINING_TEMPLATE_ID, TRAINING_NAME),
        ("inference", INFERENCE_TEMPLATE_ID, INFERENCE_NAME),
    ]:
        print(f"\n=== Recreating {name} endpoint ===")
        endpoint_input = {
            "name": display_name,
            "templateId": template_id,
            "gpuIds": GPU_TYPE,
            "workersMin": 0,
            "workersMax": 1,
            "scalerType": "QUEUE_DELAY",
            "scalerValue": 4,
            "idleTimeout": 300,
            "flashBoot": True,
            "networkVolumeId": "rrsd005i3g",
            "volumeMountPath": "/workspace",
            "containerDiskInGb": 20,
        }
        try:
            result = graphql(api_key, CREATE_QUERY, {"input": endpoint_input})
            ep = result["createEndpoint"]
            print(f"  Created: id={ep['id']}, name={ep['name']}")
        except Exception as exc:
            print(f"  GraphQL ERROR: {exc}")

            # Try REST API as fallback with correct field names
            print(f"  Trying REST API...")
            rest_input = {
                "name": display_name,
                "templateId": template_id,
                "gpuTypeIds": [GPU_TYPE],
                "workersMin": 0,
                "workersMax": 1,
                "scalerType": "QUEUE_DELAY",
                "scalerValue": 4,
                "idleTimeout": 300,
                "flashboot": True,
                "networkVolumeId": "rrsd005i3g",
            }
            r = httpx.post(
                "https://rest.runpod.io/v1/endpoints",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=rest_input,
                timeout=30.0,
            )
            print(f"  REST create: {r.status_code}")
            if r.status_code == 200:
                d = r.json()
                print(f"  Created: id={d.get('id')}, name={d.get('name')}")
            else:
                print(f"  Error: {r.text[:500]}")


if __name__ == "__main__":
    main()
