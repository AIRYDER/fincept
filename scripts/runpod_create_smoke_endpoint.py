#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from typing import Any

GRAPHQL_HOST = "api.runpod.io"
REST_HOST = "api.runpod.ai"
REST_BASE_PATH = "/v2"

FETCH_ENDPOINT = (
    "query($id: String!) {"
    " myself {"
    " endpoint(id: $id) {"
    " id name gpuIds"
    " template { id name imageName containerDiskInGb containerRegistryAuthId }"
    " }"
    " }"
    "}"
)

SAVE_ENDPOINT = (
    "mutation SaveEndpoint($input: EndpointInput!) {"
    " saveEndpoint(input: $input) {"
    " id name workersMin workersMax"
    " template { id name imageName containerRegistryAuthId }"
    " }"
    "}"
)


class RunPodSmokeCreateError(RuntimeError):
    pass


def _display_path(path: str) -> str:
    return (
        path.split("?api_key=", maxsplit=1)[0] + "?api_key=<redacted>"
        if "?api_key=" in path
        else path
    )


def _request_json(
    *,
    method: str,
    host: str,
    path: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RunPodSmokeCreateError(f"{method} {host}{_display_path(path)} failed: {exc}") from exc
    finally:
        conn.close()

    if resp.status >= 400:
        raise RunPodSmokeCreateError(
            f"{method} {host}{_display_path(path)} returned HTTP {resp.status}: {raw[:500]}"
        )
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunPodSmokeCreateError(
            f"{method} {host}{_display_path(path)} returned non-JSON: {raw[:500]}"
        ) from exc
    if not isinstance(data, dict):
        raise RunPodSmokeCreateError(
            f"{method} {host}{_display_path(path)} returned JSON {type(data).__name__}"
        )
    if "errors" in data:
        raise RunPodSmokeCreateError(
            f"RunPod GraphQL errors: {json.dumps(data['errors'], indent=2)}"
        )
    return data


def _graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    data = _request_json(
        method="POST",
        host=GRAPHQL_HOST,
        path="/graphql",
        api_key=api_key,
        payload={"query": query, "variables": variables},
        timeout=60.0,
    )
    graph_data = data.get("data")
    if not isinstance(graph_data, dict):
        raise RunPodSmokeCreateError(f"RunPod GraphQL returned no data: {data}")
    return graph_data


def _health(api_key: str, endpoint_id: str) -> dict[str, Any]:
    return _request_json(
        method="GET",
        host=REST_HOST,
        path=f"{REST_BASE_PATH}/{endpoint_id}/health",
        api_key=api_key,
        timeout=30.0,
    )


def _fetch_endpoint(api_key: str, endpoint_id: str) -> dict[str, Any]:
    data = _graphql(api_key, FETCH_ENDPOINT, {"id": endpoint_id})
    endpoint = data.get("myself", {}).get("endpoint")
    if not isinstance(endpoint, dict):
        raise RunPodSmokeCreateError(f"Endpoint not found: {endpoint_id}")
    return endpoint


def _image_sha(image_tag: str) -> str:
    tag = image_tag.rsplit(":", maxsplit=1)[-1]
    return tag if tag and tag != image_tag else ""


def _endpoint_input(args: argparse.Namespace, registry_auth_id: str | None) -> dict[str, Any]:
    template_env = [
        {"key": "PYTHONUNBUFFERED", "value": "1"},
        {"key": "PYTHONPATH", "value": "/worker"},
        {"key": "RUNPOD_SMOKE_IMAGE_TAG", "value": args.image_tag},
    ]
    sha = _image_sha(args.image_tag)
    if sha:
        template_env.append({"key": "QUANT_FOUNDRY_GIT_SHA", "value": sha})

    template: dict[str, Any] = {
        "name": args.template_name,
        "imageName": args.image_tag,
        "containerDiskInGb": args.container_disk_gb,
        "dockerArgs": args.docker_args,
        "env": template_env,
    }
    if registry_auth_id:
        template["containerRegistryAuthId"] = registry_auth_id

    endpoint: dict[str, Any] = {
        "name": args.name,
        "workersMin": args.workers_min,
        "workersMax": args.workers_max,
        "gpuIds": args.gpu_ids,
        "template": template,
    }
    if args.idle_timeout is not None:
        endpoint["idleTimeout"] = args.idle_timeout
    if args.scaler_type:
        endpoint["scalerType"] = args.scaler_type
    if args.scaler_value is not None:
        endpoint["scalerValue"] = args.scaler_value
    if args.network_volume_id:
        endpoint["networkVolumeId"] = args.network_volume_id
    if args.volume_mount_path:
        endpoint["volumeMountPath"] = args.volume_mount_path
    return endpoint


def _redacted_endpoint_input(endpoint_input: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(endpoint_input))
    template = redacted.get("template")
    if isinstance(template, dict) and template.get("containerRegistryAuthId"):
        template["containerRegistryAuthId"] = "***REDACTED***"
    return redacted


def _wait_for_health(api_key: str, endpoint_id: str, timeout: float, interval: float) -> bool:
    deadline = time.monotonic() + timeout
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        body = _health(api_key, endpoint_id)
        workers = body.get("workers", {})
        jobs = body.get("jobs", {})
        if not isinstance(workers, dict):
            workers = {}
        if not isinstance(jobs, dict):
            jobs = {}
        ready = int(workers.get("ready", 0) or 0)
        idle = int(workers.get("idle", 0) or 0)
        unhealthy = int(workers.get("unhealthy", 0) or 0)
        initializing = int(workers.get("initializing", 0) or 0)
        running = int(workers.get("running", 0) or 0)
        in_queue = int(jobs.get("inQueue", 0) or 0)
        print(
            "health "
            f"poll={poll} ready={ready} idle={idle} running={running} "
            f"initializing={initializing} unhealthy={unhealthy} inQueue={in_queue}",
            flush=True,
        )
        if (ready > 0 or idle > 0) and unhealthy == 0:
            return True
        time.sleep(interval)
    return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a fresh RunPod smoke endpoint from an exact image tag."
    )
    parser.add_argument("--image-tag", required=True)
    parser.add_argument(
        "--name",
        default="fincept-qf-smoke",
        help="RunPod endpoint name to create.",
    )
    parser.add_argument(
        "--template-name",
        default="fincept-qf-smoke-template",
        help="RunPod template name to create with the endpoint.",
    )
    parser.add_argument(
        "--copy-registry-auth-from-endpoint-id",
        default=os.environ.get("RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID", ""),
        help="Existing endpoint whose template registry auth should be reused.",
    )
    parser.add_argument("--container-registry-auth-id", default="")
    parser.add_argument("--gpu-ids", default=os.environ.get("RUNPOD_SMOKE_GPU_IDS", "ADA_24"))
    parser.add_argument("--workers-min", type=int, default=0)
    parser.add_argument("--workers-max", type=int, default=1)
    parser.add_argument("--container-disk-gb", type=int, default=20)
    parser.add_argument("--docker-args", default="")
    parser.add_argument("--idle-timeout", type=int, default=300)
    parser.add_argument("--scaler-type", default="QUEUE_DELAY")
    parser.add_argument("--scaler-value", type=int, default=4)
    parser.add_argument("--network-volume-id", default="")
    parser.add_argument("--volume-mount-path", default="")
    parser.add_argument("--wait-health", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=300.0)
    parser.add_argument("--wait-interval", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key and not args.dry_run:
        print("ERROR: RUNPOD_API_KEY env var is required", file=sys.stderr)
        return 1

    registry_auth_id = args.container_registry_auth_id or None
    if args.copy_registry_auth_from_endpoint_id:
        if not api_key:
            print(
                "ERROR: RUNPOD_API_KEY is required to copy registry auth",
                file=sys.stderr,
            )
            return 1
        source = _fetch_endpoint(api_key, args.copy_registry_auth_from_endpoint_id)
        template = source.get("template", {})
        if not isinstance(template, dict):
            raise RunPodSmokeCreateError("Source endpoint did not include a template")
        copied = template.get("containerRegistryAuthId")
        if not copied:
            raise RunPodSmokeCreateError(
                "Source endpoint template does not have containerRegistryAuthId"
            )
        registry_auth_id = str(copied)
        print(
            "Using registry auth from "
            f"{args.copy_registry_auth_from_endpoint_id} ({source.get('name', 'unknown')})"
        )

    endpoint_input = _endpoint_input(args, registry_auth_id)
    print("RunPod smoke endpoint create")
    print(f"mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"image: {args.image_tag}")
    print(f"endpoint name: {args.name}")
    print(f"gpuIds: {args.gpu_ids}")

    if args.dry_run:
        print(json.dumps(_redacted_endpoint_input(endpoint_input), indent=2))
        return 0

    data = _graphql(api_key, SAVE_ENDPOINT, {"input": endpoint_input})
    created = data.get("saveEndpoint")
    if not isinstance(created, dict):
        raise RunPodSmokeCreateError(f"saveEndpoint returned no endpoint: {data}")
    endpoint_id = created.get("id")
    if not endpoint_id:
        raise RunPodSmokeCreateError(f"saveEndpoint returned no id: {created}")

    print(f"created endpoint id: {endpoint_id}")
    print(f"created endpoint name: {created.get('name')}")
    print(f"created image: {created.get('template', {}).get('imageName')}")
    print()
    print("Probe command:")
    print(
        "python scripts/runpod_smoke_probe.py "
        f"--endpoint-id {endpoint_id} "
        f"--image-tag {args.image_tag} "
        "--interval 5 --timeout 180"
    )

    if args.wait_health:
        print()
        healthy = _wait_for_health(
            api_key,
            str(endpoint_id),
            timeout=args.wait_timeout,
            interval=args.wait_interval,
        )
        if not healthy:
            print("ERROR: endpoint did not become healthy before timeout", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunPodSmokeCreateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
