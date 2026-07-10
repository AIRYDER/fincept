#!/usr/bin/env python3
"""Deploy new container images to RunPod serverless endpoints.

Updates the bound template's imageName (and optionally env vars) for each
RunPod serverless endpoint via the GraphQL API. When a bound template is
updated, RunPod automatically creates a "Release" and rolls out the new
image to workers.

Usage:
    # Deploy both endpoints with real ML enabled
    python scripts/deploy_runpod_endpoints.py

    # Dry run — show what would change without sending
    python scripts/deploy_runpod_endpoints.py --dry-run

Requires env vars:
    RUNPOD_API_KEY                  — RunPod API key (Bearer token)
    RUNPOD_ENDPOINT_ID              — Training endpoint ID
    RUNPOD_INFERENCE_ENDPOINT_ID    — Inference endpoint ID

Optional env vars:
    RUNPOD_TRAINING_IMAGE    — override training image (default: ghcr.io/airyder/fincept/quant-foundry-training:latest)
    RUNPOD_INFERENCE_IMAGE   — override inference image (default: ghcr.io/airyder/fincept/quant-foundry-inference:latest)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import (
    INFERENCE_ENDPOINT_ID,
    TRAINING_ENDPOINT_ID,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TRAINING_IMAGE = "ghcr.io/airyder/fincept/quant-foundry-training:latest"
DEFAULT_INFERENCE_IMAGE = "ghcr.io/airyder/fincept/quant-foundry-inference:latest"

GRAPHQL_URL = "https://api.runpod.io/graphql"

# Env vars to set on each endpoint template.
# PYTHONPATH is updated to match the container's baked-in code layout
# (training: /worker, inference: /app) instead of the network volume path.
TRAINING_ENV = {
    "QUANT_FOUNDRY_USE_REAL_TRAINER": "true",
    "PYTHONPATH": "/worker",
}
INFERENCE_ENV = {
    "QUANT_FOUNDRY_USE_REAL_INFERENCE": "true",
    "PYTHONPATH": "/app",
}

# Env var keys whose values should be redacted in output.
_REDACT_KEYS = {"QUANT_FOUNDRY_CALLBACK_SECRET"}

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

FETCH_ENDPOINT_TEMPLATE = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      template {
        id
        name
        imageName
        containerDiskInGb
        volumeInGb
        volumeMountPath
        isServerless
        dockerArgs
        env { key value }
        config
        category
        containerRegistryAuthId
        isPublic
        ports
        readme
        startScript
        startSsh
        startJupyter
        advancedStart
      }
    }
  }
}
"""

SAVE_TEMPLATE = """
mutation($input: SaveTemplateInput!) {
  saveTemplate(input: $input) {
    id
    name
    imageName
    isServerless
    containerDiskInGb
    env { key value }
  }
}
"""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _graphql_request(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Send a GraphQL request to the RunPod API."""
    import httpx

    payload = {"query": query, "variables": variables}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # RunPod GraphQL accepts the API key as a query param OR Bearer header.
    url = f"{GRAPHQL_URL}?api_key={api_key}"
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL request failed: HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
    return data["data"]


# ---------------------------------------------------------------------------
# Deploy logic
# ---------------------------------------------------------------------------


def fetch_endpoint_template(api_key: str, endpoint_id: str) -> dict[str, Any]:
    """Fetch the endpoint and its bound template."""
    result = _graphql_request(api_key, FETCH_ENDPOINT_TEMPLATE, {"id": endpoint_id})
    endpoint = result["myself"]["endpoint"]
    if not endpoint:
        raise RuntimeError(f"Endpoint {endpoint_id} not found")
    return endpoint


def _merge_env(existing: list[dict[str, str]], updates: dict[str, str]) -> list[dict[str, str]]:
    """Merge env updates into existing env list (preserving non-updated keys)."""
    env_map = {e["key"]: e["value"] for e in existing}
    env_map.update(updates)
    return [{"key": k, "value": v} for k, v in env_map.items()]


def _redact_env(env_list: list[dict[str, str]]) -> list[dict[str, str]]:
    """Redact sensitive env values for display."""
    return [
        {"key": e["key"], "value": "***REDACTED***" if e["key"] in _REDACT_KEYS else e["value"]}
        for e in env_list
    ]


def update_template(
    api_key: str,
    template: dict[str, Any],
    new_image: str,
    env_updates: dict[str, str],
    docker_cmd: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update a template's imageName and env vars via saveTemplate mutation."""
    # Build the TemplateInput. saveTemplate accepts the same fields as the
    # template object, plus the `id` for updates.
    merged_env = _merge_env(template.get("env", []), env_updates)

    template_input: dict[str, Any] = {
        "id": template["id"],
        "name": template["name"],
        "imageName": new_image,
        "containerDiskInGb": template.get("containerDiskInGb", 5),
        "volumeInGb": template.get("volumeInGb", 0),
        "isServerless": template.get("isServerless", True),
        "env": merged_env,
        # Set dockerArgs to the explicit command. An empty string ("")
        # was causing RunPod to override the Dockerfile's ENTRYPOINT with
        # an empty command, preventing the handler from starting.
        # The JSON format matches RunPod's expected dockerArgs schema.
        "dockerArgs": json.dumps({"cmd": ["python", "-u", docker_cmd]}),
    }

    # Preserve optional fields if they exist.
    for field in [
        "volumeMountPath",
        "config",
        "category",
        "containerRegistryAuthId",
        "isPublic",
        "ports",
        "readme",
        "startScript",
        "startSsh",
        "startJupyter",
        "advancedStart",
    ]:
        if field in template and template[field] is not None:
            template_input[field] = template[field]

    if dry_run:
        print("  [dry-run] would saveTemplate with:")
        print(f"    id: {template_input['id']}")
        print(f"    name: {template_input['name']}")
        print(f"    imageName: {template_input['imageName']}")
        print(f"    env: {json.dumps(_redact_env(merged_env), indent=6)}")
        return {
            "id": template["id"],
            "name": template["name"],
            "imageName": new_image,
            "dry_run": True,
        }

    result = _graphql_request(api_key, SAVE_TEMPLATE, {"input": template_input})
    return result["saveTemplate"]


def deploy_endpoint(
    api_key: str,
    endpoint_id: str,
    new_image: str,
    env_updates: dict[str, str],
    docker_cmd: str,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Fetch endpoint template, update image + env, print summary."""
    print(f"\n{'=' * 60}")
    print(f"Endpoint: {endpoint_id}")
    print(f"{'=' * 60}")

    # 1. Fetch current state.
    print("  Fetching endpoint template...")
    endpoint = fetch_endpoint_template(api_key, endpoint_id)
    template = endpoint["template"]
    print(f"  Endpoint name: {endpoint.get('name', 'unknown')}")
    print(f"  Template ID: {template['id']}")
    print(f"  Template name: {template['name']}")
    print(f"  Current image: {template['imageName']}")
    current_env = {e["key"]: e["value"] for e in template.get("env", [])}
    print(f"  Current env keys: {sorted(current_env.keys())}")

    # 2. Check if update is needed.
    if (
        not force
        and template["imageName"] == new_image
        and all(current_env.get(k) == v for k, v in env_updates.items())
    ):
        print("  Already up-to-date — no changes needed.")
        return

    print(f"  New image: {new_image}")
    print(f"  Env updates: {json.dumps(env_updates, indent=4)}")

    # 3. Update the template.
    print("  Updating template...")
    result = update_template(api_key, template, new_image, env_updates, docker_cmd, dry_run=dry_run)
    print(f"  Result: id={result.get('id')}, image={result.get('imageName')}")
    if not dry_run:
        print("  [OK] Template updated. RunPod will roll out the new image to workers.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy RunPod endpoint templates")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without sending")
    parser.add_argument(
        "--force", action="store_true", help="Force template update even if image tag is unchanged"
    )
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        print("ERROR: RUNPOD_API_KEY env var is required", file=sys.stderr)
        return 1

    training_endpoint = os.environ.get("RUNPOD_ENDPOINT_ID", TRAINING_ENDPOINT_ID)
    inference_endpoint = os.environ.get("RUNPOD_INFERENCE_ENDPOINT_ID", INFERENCE_ENDPOINT_ID)
    training_image = os.environ.get("RUNPOD_TRAINING_IMAGE", DEFAULT_TRAINING_IMAGE)
    inference_image = os.environ.get("RUNPOD_INFERENCE_IMAGE", DEFAULT_INFERENCE_IMAGE)

    print("RunPod Endpoint Deployment")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}{' (FORCED)' if args.force else ''}")
    print(f"Training endpoint: {training_endpoint} -> {training_image}")
    print(f"Inference endpoint: {inference_endpoint} -> {inference_image}")

    try:
        deploy_endpoint(
            api_key,
            training_endpoint,
            training_image,
            TRAINING_ENV,
            docker_cmd="/worker/handler.py",
            dry_run=args.dry_run,
            force=args.force,
        )
        deploy_endpoint(
            api_key,
            inference_endpoint,
            inference_image,
            INFERENCE_ENV,
            docker_cmd="/app/handler.py",
            dry_run=args.dry_run,
            force=args.force,
        )
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\n{'=' * 60}")
    print("Deployment complete.")
    if not args.dry_run:
        print("Workers will pull the new image on the next job.")
        print("Monitor: https://api.runpod.ai/v2/<endpoint_id>/health")
    return 0


if __name__ == "__main__":
    sys.exit(main())
