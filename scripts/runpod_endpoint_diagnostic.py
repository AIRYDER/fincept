"""Comprehensive RunPod endpoint diagnostic.

Fetches endpoint health, GraphQL endpoint details (template, env, scaler, pods),
pod details (status, uptime, memory, GPU), and pod logs (GraphQL + REST fallback).

Usage:
    uv run python scripts/runpod_endpoint_diagnostic.py [--endpoint-id ID] [--poll N] [--logs]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

API_BASE = "https://api.runpod.io"
REST_BASE = "https://api.runpod.ai"


def _gql(api_key: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60.0,
    )
    return r.json()


def _rest(api_key: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    r = httpx.request(
        method,
        f"{REST_BASE}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
        **kwargs,
    )
    return r.json()


def list_endpoints(api_key: str) -> list[dict[str, Any]]:
    query = """
    query { myself { endpoints { id name workersMin workersMax
        template { imageName containerDiskInGb } } } }
    """
    data = _gql(api_key, query)
    if data.get("errors"):
        print(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
        return []
    return data["data"]["myself"]["endpoints"]


def get_health(api_key: str, endpoint_id: str) -> dict[str, Any]:
    return _rest(api_key, "GET", f"/v2/{endpoint_id}/health")


def get_endpoint_details(api_key: str, endpoint_id: str) -> dict[str, Any]:
    query = """
    query($id: String!) { myself { endpoint(id: $id) {
        id name workersMin workersMax gpuIds idleTimeout scalerType scalerValue
        template { id name imageName containerDiskInGb dockerArgs env { key value }
                   containerRegistryAuthId }
        pods { id name uptimeSeconds memoryInGb }
    } } }
    """
    data = _gql(api_key, query, {"id": endpoint_id})
    if data.get("errors"):
        return {"errors": data["errors"]}
    return data["data"]["myself"]["endpoint"]


def get_pod_logs(api_key: str, pod_id: str) -> str:
    # Try GraphQL first
    query = """
    query($podId: String!) { myself { pod(id: $podId) { logs { ... on PodLogEvent { data } } } } }
    """
    data = _gql(api_key, query, {"podId": pod_id})
    if not data.get("errors"):
        logs = data.get("data", {}).get("myself", {}).get("pod", {}).get("logs", [])
        if logs:
            return "\n".join(entry.get("data", "") for entry in logs if isinstance(entry, dict))
    # REST fallback
    try:
        r = _rest(api_key, "GET", f"/v2/pods/{pod_id}/logs")
        return json.dumps(r, indent=2)
    except Exception as exc:
        return f"(log fetch failed: {exc})"


def print_health(endpoint_id: str, health: dict[str, Any]) -> None:
    w = health.get("workers", {})
    j = health.get("jobs", {})
    print(
        f"  {endpoint_id}: "
        f"ready={w.get('ready', 0)} idle={w.get('idle', 0)} "
        f"init={w.get('initializing', 0)} unhealthy={w.get('unhealthy', 0)} | "
        f"jobs: in_queue={j.get('inQueue', 0)} completed={j.get('completed', 0)} "
        f"failed={j.get('failed', 0)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint-id", help="Specific endpoint ID to inspect")
    parser.add_argument("--poll", type=int, default=0, help="Poll every N seconds (0 = once)")
    parser.add_argument("--logs", action="store_true", help="Fetch pod logs")
    parser.add_argument("--list", action="store_true", help="List all endpoints and exit")
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("ERROR: RUNPOD_API_KEY env var not set", file=sys.stderr)
        return 1

    if args.list or not args.endpoint_id:
        endpoints = list_endpoints(api_key)
        print(f"\n=== Endpoints ({len(endpoints)}) ===")
        for ep in endpoints:
            tmpl = ep.get("template", {})
            print(
                f"  {ep['id']}  {ep['name']:<30}  "
                f"workers={ep.get('workersMin', 0)}-{ep.get('workersMax', 0)}  "
                f"image={tmpl.get('imageName', '?')[:60]}"
            )
            h = get_health(api_key, ep["id"])
            print_health(ep["id"], h)
        if args.list:
            return 0
        if not args.endpoint_id:
            print("\n(use --endpoint-id ID to inspect a specific endpoint)")
            return 0

    eid = args.endpoint_id
    print(f"\n=== Endpoint {eid} ===")

    if args.poll > 0:
        print(f"Polling every {args.poll}s (Ctrl-C to stop)\n", flush=True)
        try:
            while True:
                h = get_health(api_key, eid)
                ts = time.strftime("%H:%M:%S")
                print_health(f"[{ts}] {eid}", h)
                sys.stdout.flush()
                time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    # Single-shot detailed inspection
    details = get_endpoint_details(api_key, eid)
    if "errors" in details:
        print(f"GraphQL errors: {json.dumps(details['errors'], indent=2)}")
        return 1

    print(f"  Name: {details.get('name')}")
    print(f"  Workers: {details.get('workersMin', 0)}-{details.get('workersMax', 0)}")
    print(f"  GPU: {details.get('gpuIds')}")
    print(f"  Idle timeout: {details.get('idleTimeout')}")
    tmpl = details.get("template", {})
    print(f"  Image: {tmpl.get('imageName')}")
    print(f"  Disk: {tmpl.get('containerDiskInGb')}GB")
    print(f"  Docker args: {tmpl.get('dockerArgs', '(none)')}")
    env = tmpl.get("env", [])
    if env:
        print(f"  Env ({len(env)} vars):")
        for e in env:
            key = e.get("key", "?")
            val = e.get("value", "")
            # Redact secrets
            if any(s in key.upper() for s in ("SECRET", "KEY", "TOKEN", "PASSWORD")):
                val = f"({len(val)} chars, redacted)"
            print(f"    {key}={val}")

    pods = details.get("pods", [])
    print(f"\n  Pods ({len(pods)}):")
    for pod in pods:
        print(
            f"    {pod.get('id')}  "
            f"uptime={pod.get('uptimeSeconds', 0)}s  "
            f"mem={pod.get('memoryInGb')}GB"
        )
        if args.logs:
            logs = get_pod_logs(api_key, pod["id"])
            print(f"    --- logs ({len(logs)} chars) ---")
            print(logs[-2000:] if len(logs) > 2000 else logs)

    print("\n  Health:")
    h = get_health(api_key, eid)
    print_health(eid, h)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
