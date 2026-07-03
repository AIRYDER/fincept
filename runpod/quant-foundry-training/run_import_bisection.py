#!/usr/bin/env python3
"""Import Bisection Test F — live RunPod probe script.

For each QF_IMPORT_PROFILE, creates a fresh endpoint with the bisection
image, waits for ready=1 idle=1, dispatches a sentinel-shaped payload,
polls health/status every 5s, and stops at the first profile that causes
a dispatch failure (worker unhealthy, pod exit, job stuck IN_QUEUE).

Receipts are written to reports/runpod-test-runs/<sha>/import-bisection/.

Usage:
    python runpod/quant-foundry-training/run_import_bisection.py \
        --sha c0f15fa7be38460c6c1930ef5394caf152615199 \
        --profiles sentinel,pandas_numpy,xgboost,catboost,lightgbm,torch,signatures_schemas,runpod_training,quality_report,dataset_manifest,full_handler_import,full_handler_call

Never prints RUNPOD_API_KEY, QUANT_FOUNDRY_CALLBACK_SECRET, registry auth
ids, or callback signatures.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# --- Config ------------------------------------------------------------------

DEFAULT_PROFILES = [
    "sentinel",
    "pandas_numpy",
    "xgboost",
    "catboost",
    "lightgbm",
    "torch",
    "signatures_schemas",
    "runpod_training",
    "quality_report",
    "dataset_manifest",
    "full_handler_import",
    "full_handler_call",
]

GPU_TYPE = "ADA_24"
WORKERS_MIN = 1
WORKERS_MAX = 1
IDLE_TIMEOUT = 300
SCALER_TYPE = "QUEUE_DELAY"
SCALER_VALUE = 4
CONTAINER_DISK_GB = 20
READY_TIMEOUT_S = 180  # wait up to 3 min for worker to become ready (heavy imports)
PROBE_TIMEOUT_S = 120  # wait up to 2 min for job to complete
POLL_INTERVAL_S = 5

# Existing template from the sentinel test — reuse its registry auth
BASE_TEMPLATE_ID = "29f7bzvkbr"
REGISTRY_AUTH_ID = "cmqu7l5rz0047nzyt0o28je3d"

# Secrets to redact from receipts
REDACT_KEYS = {
    "RUNPOD_API_KEY",
    "QUANT_FOUNDRY_CALLBACK_SECRET",
    "containerRegistryAuthId",
}


# --- RunPod API helpers ------------------------------------------------------


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a GraphQL query against the RunPod API."""
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY not set")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.runpod.io/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RunPod-Bisect/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {error_body}") from e
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(data['errors'])}")
    return data["data"]


def _redact(obj: Any) -> Any:
    """Recursively redact secret keys from a dict/list."""
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if k in REDACT_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(i) for i in obj]
    return obj


def get_template(template_id: str) -> dict[str, Any]:
    """Get a template by ID."""
    data = _gql(
        'query GetTemplate($id: String!) { podTemplate(id: $id) { id name imageName env { key value } dockerArgs containerRegistryAuthId } }',
        {"id": template_id},
    )
    return data["podTemplate"]


def save_template(
    name: str,
    image_name: str,
    env_vars: list[dict[str, str]],
    registry_auth_id: str,
    docker_args: str = "",
    template_id: str | None = None,
) -> str:
    """Create or update a template. Returns the template ID.

    If template_id is provided, updates the existing template.
    Otherwise creates a new one.
    """
    input_obj = {
        "name": name,
        "imageName": image_name,
        "env": env_vars,
        "containerRegistryAuthId": registry_auth_id,
        "dockerArgs": docker_args,
        "volumeInGb": 0,
        "containerDiskInGb": CONTAINER_DISK_GB,
        "isServerless": True,
    }
    if template_id:
        input_obj["id"] = template_id
    data = _gql(
        'mutation SaveTemplate($input: SaveTemplateInput) { saveTemplate(input: $input) { id } }',
        {"input": input_obj},
    )
    return data["saveTemplate"]["id"]


def create_endpoint(
    name: str,
    template_id: str,
    gpu_type: str = GPU_TYPE,
    workers_min: int = WORKERS_MIN,
    workers_max: int = WORKERS_MAX,
    idle_timeout: int = IDLE_TIMEOUT,
    scaler_type: str = SCALER_TYPE,
    scaler_value: int = SCALER_VALUE,
) -> str:
    """Create a serverless endpoint from a template. Returns the endpoint ID."""
    input_obj = {
        "name": name,
        "templateId": template_id,
        "gpuIds": gpu_type,
        "workersMin": workers_min,
        "workersMax": workers_max,
        "idleTimeout": idle_timeout,
        "scalerType": scaler_type,
        "scalerValue": scaler_value,
    }
    data = _gql(
        'mutation SaveEndpoint($input: EndpointInput!) { saveEndpoint(input: $input) { id } }',
        {"input": input_obj},
    )
    return data["saveEndpoint"]["id"]


def _get_endpoint_by_id(endpoint_id: str) -> dict[str, Any]:
    """Fetch a single endpoint by ID from the myself.endpoints list."""
    data = _gql(
        'query GetEndpoints { myself { endpoints { id name templateId workersMin workersMax } } }',
    )
    endpoints = data["myself"]["endpoints"]
    for ep in endpoints:
        if ep["id"] == endpoint_id:
            return ep
    raise RuntimeError(f"Endpoint {endpoint_id} not found")


def get_endpoint_health(endpoint_id: str) -> dict[str, Any]:
    """Get endpoint health (worker counts, job counts) via REST API.

    The health endpoint is at api.runpod.ai (NOT api.runpod.io).
    """
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/health"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "RunPod-Bisect/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        # Return empty health if endpoint not ready
        return {"jobs": {}, "workers": {}}


def run_job(endpoint_id: str, input_data: dict[str, Any]) -> str:
    """Submit a job to an endpoint. Returns the job ID."""
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    body = json.dumps({"input": input_data}).encode()
    url = f"https://api.runpod.ai/v2/{endpoint_id}/run"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RunPod-Bisect/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["id"]


def get_job_status(endpoint_id: str, job_id: str) -> dict[str, Any]:
    """Get job status."""
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "RunPod-Bisect/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data


def cancel_job(endpoint_id: str, job_id: str) -> dict[str, Any]:
    """Cancel a job."""
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    body = json.dumps({"input": {"id": job_id}}).encode()
    url = f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RunPod-Bisect/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data


def update_endpoint_workers(
    endpoint_id: str, workers_min: int, workers_max: int, name: str = ""
) -> dict[str, Any]:
    """Update endpoint worker counts (scale up/down).

    The saveEndpoint mutation requires name and templateId when updating,
    so we fetch the current endpoint first if name is not provided.
    """
    if not name:
        ep = _get_endpoint_by_id(endpoint_id)
        name = ep.get("name", "")
        template_id = ep.get("templateId", "")
    else:
        template_id = ""

    input_obj: dict[str, Any] = {
        "id": endpoint_id,
        "name": name,
        "workersMin": workers_min,
        "workersMax": workers_max,
    }
    if template_id:
        input_obj["templateId"] = template_id
    data = _gql(
        'mutation SaveEndpoint($input: EndpointInput!) { saveEndpoint(input: $input) { id workersMin workersMax } }',
        {"input": input_obj},
    )
    return data["saveEndpoint"]


def delete_endpoint(endpoint_id: str) -> dict[str, Any]:
    """Delete an endpoint."""
    data = _gql(
        'mutation DeleteEndpoint($id: String!) { deleteEndpoint(id: $id) }',
        {"id": endpoint_id},
    )
    return data["deleteEndpoint"]


# --- Bisection logic ---------------------------------------------------------


def wait_for_ready(endpoint_id: str, timeout_s: int = READY_TIMEOUT_S) -> dict[str, Any]:
    """Wait for worker to reach ready=1 idle=1. Returns final health."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        health = get_endpoint_health(endpoint_id)
        workers = health.get("workers", {})
        if workers.get("ready", 0) >= 1 and workers.get("idle", 0) >= 1:
            return health
        if workers.get("unhealthy", 0) > 0:
            return health  # unhealthy — caller will detect
        time.sleep(POLL_INTERVAL_S)
    return get_endpoint_health(endpoint_id)


def probe_profile(
    profile: str,
    sha: str,
    image_tag: str,
    receipt_dir: Path,
    template_id: str,
    callback_secret: str,
) -> dict[str, Any]:
    """Run a single import bisection profile test.

    Returns a dict with:
      - profile: the profile name
      - result: "pass" | "fail"
      - failure_reason: str | None
      - endpoint_id: str
      - job_id: str | None
      - final_status: str | None
    """
    short_sha = sha[:8]
    endpoint_name = f"qf-bisect-{profile}-{short_sha}"
    job_id_label = f"qf:import-bisect:{profile}:{short_sha}"

    print(f"\n{'='*60}")
    print(f"  PROFILE: {profile}")
    print(f"  Endpoint: {endpoint_name}")
    print(f"{'='*60}")

    # Create endpoint
    try:
        endpoint_id = create_endpoint(
            name=endpoint_name,
            template_id=template_id,
        )
        print(f"  Endpoint created: {endpoint_id}")
    except Exception as e:
        print(f"  ERROR creating endpoint: {e}")
        return {
            "profile": profile,
            "result": "fail",
            "failure_reason": f"endpoint_create_error: {e}",
            "endpoint_id": None,
            "job_id": None,
            "final_status": None,
        }

    probe_log: list[dict[str, Any]] = []

    try:
        # Wait for ready
        print(f"  Waiting for ready (timeout={READY_TIMEOUT_S}s)...")
        health_before = wait_for_ready(endpoint_id, READY_TIMEOUT_S)
        workers = health_before.get("workers", {})
        print(f"  Health before: ready={workers.get('ready',0)} idle={workers.get('idle',0)} unhealthy={workers.get('unhealthy',0)}")

        # Write health-before receipt
        (receipt_dir / f"health-before-{profile}.json").write_text(
            json.dumps(_redact(health_before), indent=2, sort_keys=True)
        )

        # Check if worker is unhealthy before dispatch
        if workers.get("unhealthy", 0) > 0 or workers.get("ready", 0) == 0:
            print("  FAIL: worker not healthy before dispatch")
            probe_log.append({
                "event": "worker_unhealthy_before_dispatch",
                "health": _redact(health_before),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            (receipt_dir / f"probe-{profile}.jsonl").write_text(
                "\n".join(json.dumps(l) for l in probe_log) + "\n"
            )
            return {
                "profile": profile,
                "result": "fail",
                "failure_reason": "worker_unhealthy_before_dispatch",
                "endpoint_id": endpoint_id,
                "job_id": None,
                "final_status": None,
            }

        # Dispatch job
        input_data = {
            "task": "import_bisect",
            "job_id": job_id_label,
            "profile": profile,
        }
        try:
            job_id = run_job(endpoint_id, input_data)
            print(f"  Job dispatched: {job_id}")
        except Exception as e:
            print(f"  ERROR dispatching job: {e}")
            return {
                "profile": profile,
                "result": "fail",
                "failure_reason": f"job_dispatch_error: {e}",
                "endpoint_id": endpoint_id,
                "job_id": None,
                "final_status": None,
            }

        probe_log.append({
            "event": "job_dispatched",
            "job_id": job_id,
            "input": _redact(input_data),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

        # Poll health and status
        deadline = time.time() + PROBE_TIMEOUT_S
        final_status = None
        failure_reason = None

        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_S)

            # Check health
            health = get_endpoint_health(endpoint_id)
            workers = health.get("workers", {})
            jobs = health.get("jobs", {})

            # Check job status
            status_resp = get_job_status(endpoint_id, job_id)
            job_status = status_resp.get("status", "UNKNOWN")
            final_status = job_status

            probe_log.append({
                "event": "poll",
                "job_id": job_id,
                "status": job_status,
                "health": _redact(health),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })

            print(f"  [{int(time.time()) % 100}] status={job_status} "
                  f"ready={workers.get('ready',0)} idle={workers.get('idle',0)} "
                  f"running={workers.get('running',0)} "
                  f"unhealthy={workers.get('unhealthy',0)} "
                  f"inQueue={jobs.get('inQueue',0)} completed={jobs.get('completed',0)}")

            # Check for failure conditions
            if workers.get("unhealthy", 0) > 0:
                failure_reason = "worker_unhealthy"
                print("  FAIL: worker went unhealthy")
                break

            if job_status in ("COMPLETED",):
                print("  PASS: job COMPLETED")
                break

            if job_status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                failure_reason = f"job_{job_status.lower()}"
                print(f"  FAIL: job {job_status}")
                break

            if (
                job_status == "IN_QUEUE"
                and workers.get("ready", 0) == 0
                and workers.get("running", 0) == 0
                and workers.get("unhealthy", 0) == 0
            ):
                # Do not treat ready=0 alone as worker death. The worker may
                # have picked up the job and be running=1 (actively
                # processing). Only declare death when there is no active
                # worker at all (ready=0, running=0, unhealthy=0) — meaning
                # the worker pod exited while the job is still queued.
                failure_reason = "worker_died_while_job_in_queue"
                print("  FAIL: worker died while job in queue (no active worker)")
                break

        else:
            # Timeout reached
            failure_reason = "probe_timeout"
            print(f"  FAIL: probe timed out (job stuck in {final_status})")

        # Cancel stuck job if not completed
        if final_status not in ("COMPLETED",):
            try:
                cancel_job(endpoint_id, job_id)
                print(f"  Job cancelled: {job_id}")
            except Exception as e:
                print(f"  WARNING: could not cancel job: {e}")

        # Final health check
        health_after = get_endpoint_health(endpoint_id)
        print(f"  Health after: ready={health_after['workers'].get('ready',0)} "
              f"unhealthy={health_after['workers'].get('unhealthy',0)}")

        # Write receipts
        (receipt_dir / f"probe-{profile}.jsonl").write_text(
            "\n".join(json.dumps(l) for l in probe_log) + "\n"
        )
        (receipt_dir / f"health-after-{profile}.json").write_text(
            json.dumps(_redact(health_after), indent=2, sort_keys=True)
        )
        (receipt_dir / f"status-final-{profile}.json").write_text(
            json.dumps({"job_id": job_id, "final_status": final_status}, indent=2)
        )

        result = "pass" if failure_reason is None else "fail"
        return {
            "profile": profile,
            "result": result,
            "failure_reason": failure_reason,
            "endpoint_id": endpoint_id,
            "job_id": job_id,
            "final_status": final_status,
        }

    finally:
        # Always scale down
        try:
            update_endpoint_workers(endpoint_id, 0, 0, name=endpoint_name)
            print("  Scaled down to 0/0")
        except Exception as e:
            print(f"  WARNING: could not scale down: {e}")

        # Write cleanup receipt
        try:
            final_ep = get_endpoint_health(endpoint_id)
            (receipt_dir / f"cleanup-{profile}.json").write_text(
                json.dumps({
                    "endpoint_id": endpoint_id,
                    "workersMin": 0,
                    "workersMax": 0,
                    "health_after_scale_down": _redact(final_ep),
                }, indent=2, sort_keys=True)
            )
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Bisection Test F")
    parser.add_argument("--sha", required=True, help="Git SHA (full)")
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILES),
        help="Comma-separated list of profiles to test",
    )
    parser.add_argument(
        "--image-tag",
        default=None,
        help="Full image tag (defaults to ghcr.io/airyder/fincept/quant-foundry-training:<sha>)",
    )
    parser.add_argument(
        "--template-id",
        default=None,
        help="Existing template ID to reuse (skips template creation)",
    )
    parser.add_argument(
        "--continue-on-fail",
        action="store_true",
        help="Continue testing all profiles even after a failure (default: stop at first failure)",
    )
    args = parser.parse_args()

    sha = args.sha
    image_tag = args.image_tag or f"ghcr.io/airyder/fincept/quant-foundry-training:{sha}"
    profiles = args.profiles.split(",")

    receipt_dir = Path(f"reports/runpod-test-runs/{sha[:8]}/import-bisection")
    receipt_dir.mkdir(parents=True, exist_ok=True)

    print("Import Bisection Test F")
    print(f"  SHA: {sha}")
    print(f"  Image: {image_tag}")
    print(f"  Profiles: {profiles}")
    print(f"  Receipts: {receipt_dir}")

    # Get the base template to copy env vars from
    base_template = get_template(BASE_TEMPLATE_ID)
    callback_secret = ""
    for env in base_template.get("env", []):
        if env["key"] == "QUANT_FOUNDRY_CALLBACK_SECRET":
            callback_secret = env["value"]
            break

    # Build env var list for the new template (preserve all existing vars)
    base_env = base_template.get("env", [])

    # Create a new template for the bisection image
    bisect_template_name = f"qf-bisect-{sha[:8]}-tpl"
    # Replace the image tag and git sha in env vars, add QF_IMPORT_PROFILE placeholder
    bisect_env = []
    for env in base_env:
        if env["key"] == "QUANT_FOUNDRY_GIT_SHA":
            bisect_env.append({"key": env["key"], "value": sha})
        elif env["key"] == "RUNPOD_SMOKE_IMAGE_TAG":
            bisect_env.append({"key": env["key"], "value": image_tag})
        else:
            bisect_env.append({"key": env["key"], "value": env["value"]})
    # Add QF_IMPORT_PROFILE (will be updated per-profile)
    bisect_env.append({"key": "QF_IMPORT_PROFILE", "value": "sentinel"})

    print(f"\nCreating template: {bisect_template_name}")
    print(f"  Image: {image_tag}")
    print(f"  Env keys: {[e['key'] for e in bisect_env]}")

    if args.template_id:
        template_id = args.template_id
        print(f"  Reusing existing base template: {template_id}")
    else:
        try:
            template_id = save_template(
                name=bisect_template_name,
                image_name=image_tag,
                env_vars=bisect_env,
                registry_auth_id=REGISTRY_AUTH_ID,
            )
            print(f"  Template created: {template_id}")
        except Exception:
            # If template name exists, try with a suffix
            try:
                bisect_template_name = f"qf-bisect-{sha[:8]}-base-tpl"
                template_id = save_template(
                    name=bisect_template_name,
                    image_name=image_tag,
                    env_vars=bisect_env,
                    registry_auth_id=REGISTRY_AUTH_ID,
                )
                print(f"  Template created (alt name): {template_id}")
            except Exception as e2:
                print(f"ERROR creating template: {e2}")
                return 1

    # Write template creation receipt (redacted)
    (receipt_dir / "template-create-redacted.txt").write_text(
        f"Template ID: {template_id}\n"
        f"Name: {bisect_template_name}\n"
        f"Image: {image_tag}\n"
        f"Registry Auth: ***REDACTED***\n"
        f"Env keys: {', '.join(e['key'] for e in bisect_env)}\n"
    )

    # Run each profile sequentially
    results = []
    first_fail = None
    last_pass = None

    for profile in profiles:
        # Build env var list for this specific profile
        profile_env = []
        for env in bisect_env:
            if env["key"] == "QF_IMPORT_PROFILE":
                profile_env.append({"key": "QF_IMPORT_PROFILE", "value": profile})
            else:
                profile_env.append({"key": env["key"], "value": env["value"]})

        # Create a dedicated template for this profile to avoid race conditions
        profile_template_name = f"qf-bisect-{profile}-{sha[:8]}-tpl"
        try:
            profile_template_id = save_template(
                name=profile_template_name,
                image_name=image_tag,
                env_vars=profile_env,
                registry_auth_id=REGISTRY_AUTH_ID,
            )
            print(f"\n  Template created for {profile}: {profile_template_id}")
        except Exception as e:
            print(f"ERROR creating template for profile {profile}: {e}")
            results.append({
                "profile": profile,
                "result": "fail",
                "failure_reason": f"template_create_error: {e}",
            })
            if not args.continue_on_fail:
                break
            continue

        # Run the probe
        result = probe_profile(
            profile=profile,
            sha=sha,
            image_tag=image_tag,
            receipt_dir=receipt_dir,
            template_id=profile_template_id,
            callback_secret=callback_secret,
        )
        results.append(result)

        if result["result"] == "pass":
            last_pass = profile
        else:
            first_fail = profile or first_fail
            print(f"\n{'='*60}")
            print(f"  FAILURE: {profile}")
            print(f"  Reason: {result['failure_reason']}")
            print(f"  Last passing: {last_pass}")
            print(f"{'='*60}")
            if not args.continue_on_fail:
                break  # Stop at first failure

    # Write summary
    summary = {
        "sha": sha,
        "image_tag": image_tag,
        "template_id": template_id,
        "profiles_tested": [r["profile"] for r in results],
        "first_failing_profile": first_fail,
        "last_passing_profile": last_pass,
        "results": results,
    }
    (receipt_dir / "summary.json").write_text(
        json.dumps(_redact(summary), indent=2, sort_keys=True)
    )

    # Write interpretation
    interp_lines = [
        "# Import Bisection Test F — Interpretation",
        "",
        f"**Image SHA:** {sha}",
        f"**Image tag:** {image_tag}",
        f"**Template ID:** {template_id}",
        "",
        "## Results",
        "",
        "| Profile | Result | Failure Reason | Endpoint | Job ID | Final Status |",
        "|---------|--------|----------------|----------|--------|--------------|",
    ]
    for r in results:
        interp_lines.append(
            f"| {r['profile']} | {r['result']} | {r.get('failure_reason', '-')} | "
            f"{r.get('endpoint_id', '-')} | {r.get('job_id', '-')} | "
            f"{r.get('final_status', '-')} |"
        )
    interp_lines.extend([
        "",
        "## Summary",
        "",
        f"- **First failing profile:** {first_fail or 'none (all passed)'}",
        f"- **Last passing profile:** {last_pass or 'none'}",
        f"- **Profiles tested:** {len(results)}",
        "",
        "## Next Steps",
        "",
    ])
    if first_fail:
        interp_lines.extend([
            f"The first failing profile is **{first_fail}**.",
            f"The last passing profile is **{last_pass}**.",
            "",
            f"This means the import group `{first_fail}` poisons the worker at dispatch time.",
            "The fix should isolate or lazy-load the imports in this group.",
        ])
    else:
        interp_lines.append("All profiles passed — the issue may be in the handler logic, not imports.")

    (receipt_dir / "interpretation.md").write_text("\n".join(interp_lines) + "\n")

    print(f"\n{'='*60}")
    print("  BISECTION COMPLETE")
    print(f"  First failing: {first_fail or 'none'}")
    print(f"  Last passing: {last_pass or 'none'}")
    print(f"  Receipts: {receipt_dir}")
    print(f"{'='*60}")

    return 0 if first_fail is None else 2  # 0=all pass, 2=found failure


if __name__ == "__main__":
    sys.exit(main())
