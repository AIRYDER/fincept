"""Comprehensive RunPod serverless endpoint diagnostic.

Fetches endpoint health, pod details, pod logs, and recent job status
in one call.  Use this to diagnose pod exits, stuck jobs, and worker
health issues.

Usage:
    python scripts/runpod_endpoint_diagnostic.py --endpoint-id zbpy7m8s8dps7k
    python scripts/runpod_endpoint_diagnostic.py --endpoint-id zbpy7m8s8dps7k --poll 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

REST_HOST = "https://api.runpod.ai/v2"
GRAPHQL_URL = "https://api.runpod.io/graphql"


def _emit(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": _now(), "event": event, **fields}, sort_keys=True))
    sys.stdout.flush()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{GRAPHQL_URL}?api_key={api_key}",
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    return r.json()


def _rest(api_key: str, method: str, path: str) -> dict[str, Any]:
    url = f"{REST_HOST}/{path}"
    r = httpx.request(
        method,
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    try:
        return {"status_code": r.status_code, "body": r.json() if r.text else {}}
    except Exception:
        return {"status_code": r.status_code, "body": r.text[:2000]}


# GraphQL queries
QUERY_ENDPOINT = """
query($id: String!) {
  myself {
    endpoint(id: $id) {
      id
      name
      status
      workersMin
      workersMax
      gpuIds
      idleTimeout
      scalerType
      scalerValue
      networkVolumeId
      volumeMountPath
      template {
        id
        name
        imageName
        containerDiskInGb
        dockerArgs
        env {
          key
          value
        }
      }
      pods {
        id
        name
        dockerId
        machineId
      }
    }
  }
}
"""

QUERY_POD_LOGS = """
query($id: String!) {
  pod(id: $id) {
    id
    name
    containerDiskInGb
    vcpuCount
    memoryInGb
    gpuCount
    imageName
    desiredStatus
    lastStatusChange
    podStatus
    machine {
      id
      gpuDisplayName
    }
    runtime {
      ports
      uptimeSeconds
    }
    logs(last: 200) {
      lines
    }
  }
}
"""


def _fetch_endpoint(api_key: str, endpoint_id: str) -> dict[str, Any]:
    data = _graphql(api_key, QUERY_ENDPOINT, {"id": endpoint_id})
    if "errors" in data:
        return {"errors": data["errors"]}
    endpoint = data.get("data", {}).get("myself", {}).get("endpoint")
    if not endpoint:
        return {"error": "endpoint not found"}
    return endpoint


def _fetch_pod_details(api_key: str, pod_id: str) -> dict[str, Any]:
    data = _graphql(api_key, QUERY_POD_LOGS, {"id": pod_id})
    if "errors" in data:
        return {"errors": data["errors"]}
    pod = data.get("data", {}).get("pod")
    if not pod:
        return {"error": f"pod {pod_id} not found"}
    return pod


def _fetch_pod_logs_rest(api_key: str, pod_id: str) -> dict[str, Any]:
    """Try REST API for pod logs (fallback if GraphQL logs don't work)."""
    for base in ("https://rest.runpod.io/v1", "https://api.runpod.io/v1"):
        url = f"{base}/pods/{pod_id}/logs"
        r = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
        if r.status_code == 200:
            try:
                return {"source": base, "body": r.json()}
            except Exception:
                return {"source": base, "body": r.text[:3000]}
        else:
            return {"source": base, "status_code": r.status_code, "body": r.text[:500]}
    return {"error": "no REST logs endpoint returned 200"}


def _redact_env(env_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact sensitive env values."""
    sensitive = {"QUANT_FOUNDRY_CALLBACK_SECRET", "RUNPOD_API_KEY"}
    redacted = []
    for item in env_list:
        key = item.get("key", "")
        if key in sensitive:
            redacted.append({"key": key, "value": "***REDACTED***"})
        else:
            redacted.append(item)
    return redacted


def diagnose_once(api_key: str, endpoint_id: str) -> dict[str, Any]:
    """Run one full diagnostic pass and return all findings."""
    results: dict[str, Any] = {"endpoint_id": endpoint_id, "timestamp": _now()}

    # 1. REST health
    health = _rest(api_key, "GET", f"{endpoint_id}/health")
    results["health"] = health
    _emit("health", endpoint_id=endpoint_id, body=health.get("body", {}))

    # 2. GraphQL endpoint details
    endpoint = _fetch_endpoint(api_key, endpoint_id)
    results["endpoint"] = endpoint
    if "errors" in endpoint:
        _emit("endpoint_errors", errors=endpoint["errors"])
        return results

    # Redact env in output
    template = endpoint.get("template", {})
    if isinstance(template, dict) and isinstance(template.get("env"), list):
        template["env"] = _redact_env(template["env"])

    _emit(
        "endpoint_details",
        name=endpoint.get("name"),
        status=endpoint.get("status"),
        workers_min=endpoint.get("workersMin"),
        workers_max=endpoint.get("workersMax"),
        gpu_ids=endpoint.get("gpuIds"),
        image=template.get("imageName"),
        disk_gb=template.get("containerDiskInGb"),
        docker_args=template.get("dockerArgs"),
        scaler_type=endpoint.get("scalerType"),
        scaler_value=endpoint.get("scalerValue"),
        idle_timeout=endpoint.get("idleTimeout"),
        pod_count=len(endpoint.get("pods", [])),
    )

    # 3. Pod details + logs
    pods = endpoint.get("pods", [])
    pod_results = []
    for pod in pods:
        pod_id = pod.get("id", "")
        _emit("pod_fetch", pod_id=pod_id, name=pod.get("name"))
        pod_detail = _fetch_pod_details(api_key, pod_id)
        if "errors" in pod_detail:
            # Try REST logs as fallback
            rest_logs = _fetch_pod_logs_rest(api_key, pod_id)
            pod_detail["rest_logs"] = rest_logs
        else:
            # Also try REST logs for comparison
            rest_logs = _fetch_pod_logs_rest(api_key, pod_id)
            pod_detail["rest_logs"] = rest_logs
        pod_results.append({"pod_id": pod_id, "detail": pod_detail})

        # Emit pod status
        _emit(
            "pod_status",
            pod_id=pod_id,
            pod_status=pod_detail.get("podStatus"),
            desired_status=pod_detail.get("desiredStatus"),
            last_status_change=pod_detail.get("lastStatusChange"),
            uptime=pod_detail.get("runtime", {}).get("uptimeSeconds") if isinstance(pod_detail.get("runtime"), dict) else None,
            gpu=pod_detail.get("machine", {}).get("gpuDisplayName") if isinstance(pod_detail.get("machine"), dict) else None,
            memory_gb=pod_detail.get("memoryInGb"),
            vcpu=pod_detail.get("vcpuCount"),
        )

        # Emit pod logs
        logs = pod_detail.get("logs", {})
        if isinstance(logs, dict) and logs.get("lines"):
            lines = logs["lines"]
            if isinstance(lines, list):
                for line in lines[-50:]:
                    _emit("pod_log", pod_id=pod_id, line=line)
            elif isinstance(lines, str):
                _emit("pod_log", pod_id=pod_id, lines=lines[-3000:])

    results["pods"] = pod_results

    # 4. Check for stuck jobs via REST status endpoint
    # The /health endpoint already shows inQueue count
    health_body = health.get("body", {})
    if isinstance(health_body, dict):
        in_queue = health_body.get("jobsInQueue", 0)
        unhealthy = health_body.get("workersUnhealthy", 0)
        if in_queue > 0 or unhealthy > 0:
            _emit(
                "warning",
                in_queue=in_queue,
                unhealthy=unhealthy,
                msg="Jobs stuck or workers unhealthy",
            )

    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Comprehensive RunPod serverless endpoint diagnostic."
    )
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--poll",
        type=float,
        default=0,
        help="If >0, poll every N seconds until Ctrl+C.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write full diagnostic JSON to this file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_key = args.api_key or os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        print("ERROR: RUNPOD_API_KEY env var or --api-key required", file=sys.stderr)
        return 1

    if args.poll > 0:
        try:
            while True:
                results = diagnose_once(api_key, args.endpoint_id)
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(results, f, indent=2, sort_keys=True, default=str)
                time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\n[diagnostic] stopped by user")
    else:
        results = diagnose_once(api_key, args.endpoint_id)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, sort_keys=True, default=str)
            print(f"\n[diagnostic] full results written to {args.output}")
        else:
            print(json.dumps(results, indent=2, sort_keys=True, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
