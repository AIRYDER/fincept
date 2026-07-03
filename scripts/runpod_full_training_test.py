"""End-to-end full training test on the RunPod training endpoint.

Steps:
1. Cancel any stuck jobs on the target endpoint
2. Free worker quota (zero out unnecessary endpoints)
3. Wait for the worker to become ready
4. Submit a full train_model job with an inline CSV dataset
5. Poll until the job completes, printing the result

Usage:
    uv run python scripts/runpod_full_training_test.py [--endpoint-id ID] [--image SHA]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from typing import Any

import httpx

API_BASE = "https://api.runpod.io"
REST_BASE = "https://api.runpod.ai"

FETCH_QUERY = """
query($id: String!) { myself { endpoint(id: $id) {
    id name workersMin workersMax gpuIds idleTimeout scalerType scalerValue
    template { id name imageName containerDiskInGb dockerArgs env { key value }
               containerRegistryAuthId }
} } }
"""

SAVE_QUERY = (
    "mutation SaveEndpoint($input: EndpointInput!) {"
    " saveEndpoint(input: $input) {"
    " id name workersMin workersMax"
    " template { id name imageName env { key value } }"
    " } }"
)


def gql(api_key: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60.0,
    )
    return r.json()


def rest(api_key: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    r = httpx.request(
        method,
        f"{REST_BASE}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
        **kwargs,
    )
    return r.json()


def get_health(api_key: str, eid: str) -> dict[str, Any]:
    return rest(api_key, "GET", f"/v2/{eid}/health")


def cancel_job(api_key: str, eid: str, job_id: str) -> dict[str, Any]:
    return rest(api_key, "POST", f"/v2/{eid}/cancel/{job_id}")


def list_jobs(api_key: str, eid: str) -> list[dict[str, Any]]:
    """List jobs on an endpoint. RunPod may return paginated or non-JSON format."""
    try:
        r = httpx.get(
            f"{REST_BASE}/v2/{eid}/jobs",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("jobs", data.get("data", []))
        return []
    except Exception:
        return []


def set_workers(
    api_key: str,
    eid: str,
    workers_min: int,
    workers_max: int,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Update endpoint worker count (and optionally image/env)."""
    data = gql(api_key, FETCH_QUERY, {"id": eid})
    ep = data["data"]["myself"]["endpoint"]
    tmpl = ep["template"]

    env_list = tmpl.get("env", [])
    if extra_env:
        env_list = [e for e in env_list if e["key"] not in extra_env]
        for k, v in extra_env.items():
            env_list.append({"key": k, "value": v})

    endpoint_input: dict[str, Any] = {
        "id": eid,
        "name": ep["name"],
        "workersMin": workers_min,
        "workersMax": workers_max,
        "gpuIds": ep.get("gpuIds", "ADA_24"),
        "template": {
            "name": tmpl["name"],
            "imageName": image or tmpl["imageName"],
            "containerDiskInGb": tmpl.get("containerDiskInGb", 40),
            "dockerArgs": tmpl.get("dockerArgs", ""),
            "env": env_list,
        },
    }
    if tmpl.get("containerRegistryAuthId"):
        endpoint_input["template"]["containerRegistryAuthId"] = tmpl["containerRegistryAuthId"]
    if ep.get("idleTimeout") is not None:
        endpoint_input["idleTimeout"] = ep["idleTimeout"]
    if ep.get("scalerType"):
        endpoint_input["scalerType"] = ep["scalerType"]
    if ep.get("scalerValue") is not None:
        endpoint_input["scalerValue"] = ep["scalerValue"]

    result = gql(api_key, SAVE_QUERY, {"input": endpoint_input})
    return result


def wait_ready(api_key: str, eid: str, max_polls: int = 40, interval: int = 30) -> bool:
    """Wait for worker to become ready. Returns True if ready."""
    for i in range(max_polls):
        h = get_health(api_key, eid)
        w = h.get("workers", {})
        j = h.get("jobs", {})
        ts = time.strftime("%H:%M:%S")
        print(
            f"  [{ts}] poll {i + 1}/{max_polls}: "
            f"ready={w.get('ready', 0)} idle={w.get('idle', 0)} "
            f"init={w.get('initializing', 0)} unhealthy={w.get('unhealthy', 0)} | "
            f"in_queue={j.get('inQueue', 0)}"
        )
        if w.get("ready", 0) > 0 or w.get("idle", 0) > 0:
            return True
        if w.get("unhealthy", 0) > 0 and i > 2:
            print("  *** ENDPOINT WENT UNHEALTHY ***")
            return False
        time.sleep(interval)
    print("  *** TIMEOUT waiting for ready ***")
    return False


def purge_queue(api_key: str, eid: str) -> int:
    """Cancel all IN_QUEUE jobs. Returns count cancelled."""
    jobs = list_jobs(api_key, eid)
    cancelled = 0
    for job in jobs:
        if job.get("status") == "IN_QUEUE":
            jid = job.get("id")
            if jid:
                cancel_job(api_key, eid, jid)
                print(f"  Cancelled job {jid}")
                cancelled += 1
                time.sleep(1)
    return cancelled


def build_training_payload(job_id: str) -> dict[str, Any]:
    """Build a full train_model payload with inline CSV dataset."""
    random.seed(42)
    csv_rows = ["feature_1,feature_2,feature_3,label"]
    for _ in range(200):
        f1 = random.gauss(0, 1)
        f2 = random.gauss(0, 1)
        f3 = random.gauss(0, 1)
        label = 1 if (f1 + f2 + random.gauss(0, 0.3)) > 0 else 0
        csv_rows.append(f"{f1:.6f},{f2:.6f},{f3:.6f},{label}")
    inline_csv = "\n".join(csv_rows)

    return {
        "input": {
            "job_id": job_id,
            "schema_version": 1,
            "dataset_manifest_ref": "inline",
            "model_family": "lightgbm",
            "search_space": {
                "num_leaves": [15, 31, 63],
                "learning_rate": [0.05, 0.1, 0.2],
                "n_estimators": [50, 100],
            },
            "random_seed": 42,
            "extra_constraints": {"training_mode": "research"},
            "inline_dataset_csv": inline_csv,
            "n_folds": 3,
            "output_prefix": "/runpod-volume/output",
        }
    }


def is_training_success(result: dict[str, Any]) -> bool:
    if result.get("status") != "COMPLETED":
        return False
    output = result.get("output")
    if not isinstance(output, dict):
        return False
    if output.get("signed_failure") is True or output.get("error_code"):
        return False
    output_status = str(output.get("status", "")).lower()
    return output_status not in {"failed", "error"}


def submit_and_poll(api_key: str, eid: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Submit a job and poll until completion."""
    print(f"\nSubmitting job {payload['input']['job_id']}...")
    r = httpx.post(
        f"{REST_BASE}/v2/{eid}/run",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=60.0,
    )
    data = r.json()
    job_id = data.get("id")
    if not job_id:
        print(f"ERROR: No job ID returned: {json.dumps(data, indent=2)}")
        return None
    print(f"Job submitted: {job_id}")

    print(f"\nPolling job {job_id}...")
    for i in range(120):
        status = rest(api_key, "GET", f"/v2/{eid}/status/{job_id}")
        state = status.get("status", "unknown")
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] poll {i + 1}/120: status={state}")
        if state in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            return status
        time.sleep(15)
    print("  *** TIMEOUT waiting for job ***")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint-id",
        default="gt9r90hxsip48l",
        help="Endpoint ID (default: train-lazy gt9r90hxsip48l)",
    )
    parser.add_argument(
        "--image",
        help="Override image (full ref). Default: keep current image.",
    )
    parser.add_argument(
        "--set-env",
        action="append",
        help="Set env var KEY=VALUE (repeatable)",
    )
    parser.add_argument("--purge-queue", action="store_true", help="Cancel all IN_QUEUE jobs first")
    parser.add_argument(
        "--skip-submit", action="store_true", help="Only wait for ready, don't submit"
    )
    args = parser.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("ERROR: RUNPOD_API_KEY env var not set", file=sys.stderr)
        return 1

    eid = args.endpoint_id
    print(f"\n=== Full Training Test on endpoint {eid} ===")

    # Step 1: Purge stuck jobs
    if args.purge_queue:
        print("\n--- Purging stuck jobs ---")
        n = purge_queue(api_key, eid)
        print(f"Cancelled {n} jobs")

    # Step 2: Set workers to 0 then back to 1 (force fresh worker) + optional image/env update
    extra_env: dict[str, str] = {}
    if args.set_env:
        for kv in args.set_env:
            if "=" in kv:
                k, v = kv.split("=", 1)
                extra_env[k] = v

    print("\n--- Recycling worker (set workersMax=0) ---")
    r0 = set_workers(api_key, eid, 0, 0, image=args.image, extra_env=extra_env or None)
    if r0.get("errors"):
        print(f"ERROR: {json.dumps(r0['errors'], indent=2)}")
        return 1
    print("  workersMax=0 OK")
    time.sleep(10)

    print("\n--- Setting workersMax=1 ---")
    r1 = set_workers(api_key, eid, 0, 1, image=args.image, extra_env=extra_env or None)
    if r1.get("errors"):
        print(f"ERROR: {json.dumps(r1['errors'], indent=2)}")
        return 1
    ep = r1["data"]["saveEndpoint"]
    print(f"  workersMax=1 OK, image={ep['template']['imageName']}")

    # Step 3: Wait for worker ready
    print("\n--- Waiting for worker ready ---")
    if not wait_ready(api_key, eid):
        print("\nWorker did not become ready. Fetching details...")
        return 1

    if args.skip_submit:
        print("\nWorker is ready. (--skip-submit: not submitting a job)")
        return 0

    # Step 4: Submit full training job
    print("\n--- Submitting full training job ---")
    payload = build_training_payload(f"full-train-{int(time.time())}")
    result = submit_and_poll(api_key, eid, payload)

    # Step 5: Print result
    print("\n=== Result ===")
    if result:
        print(json.dumps(result, indent=2, default=str))
        if is_training_success(result):
            print("\n*** TRAINING JOB COMPLETED SUCCESSFULLY ***")
            return 0
        else:
            print(f"\n*** JOB ENDED WITH STATUS: {result.get('status')} ***")
            return 1
    else:
        print("\n*** JOB TIMED OUT ***")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
