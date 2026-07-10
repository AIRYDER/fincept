"""Live production canary for the RunPod training worker.

Creates a fresh endpoint with the exact SHA image, waits for ready=1,
dispatches a callback_secret_canary job, polls health/status until terminal,
scales down, and writes a receipt bundle.

Usage:
    python runpod/quant-foundry-training/run_live_canary.py --sha <sha> [--template-id <id>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Shared lifecycle helpers (unique naming, retry cleanup, timeout config).
# The scripts/ package is added to sys.path below so this works when the
# probe tools are run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from runpod.runpod_lifecycle import (  # noqa: E402
    DEFAULT_IDLE_TIMEOUT_S,
    EndpointConfig,
    TemplateConfig,
    build_endpoint_input,
    build_job_policy,
    build_template_input,
    compute_execution_timeout,
    format_timeout_receipt,
    make_unique_name,
    retry_delete_endpoint,
    safe_scale_to_zero,
)

# --- Config ------------------------------------------------------------------

GPU_TYPE = "ADA_24"
WORKERS_MIN = 1
WORKERS_MAX = 1
IDLE_TIMEOUT = DEFAULT_IDLE_TIMEOUT_S
# executionTimeout >= 1860s (handler deadline 1800s + 60s slack) so the
# handler's signed failure envelope always fires before RunPod times the
# job out. RunPod's default is 600s — never inherit it.
EXECUTION_TIMEOUT = compute_execution_timeout()
SCALER_TYPE = "QUEUE_DELAY"
SCALER_VALUE = 4
CONTAINER_DISK_GB = 20
READY_TIMEOUT_S = 180
PROBE_TIMEOUT_S = 120
POLL_INTERVAL_S = 5
REGISTRY_AUTH_ID = "cmqu7l5rz0047nzyt0o28je3d"  # copied from known working source


# --- RunPod API helpers (same as bisection script) ---------------------------


def _gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "User-Agent": "RunPod-Canary/1.0",
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
    if isinstance(obj, dict):
        return {
            k: (
                "****"
                if any(s in k.lower() for s in ("secret", "key", "token", "password", "auth"))
                else _redact(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def save_template(name: str, image_name: str, env_vars: list[dict], registry_auth_id: str) -> str:
    config = TemplateConfig(
        name=name,
        image_name=image_name,
        env_vars=env_vars,
        registry_auth_id=registry_auth_id,
        container_disk_gb=CONTAINER_DISK_GB,
    )
    input_obj = build_template_input(config)
    data = _gql(
        "mutation SaveTemplate($input: SaveTemplateInput) { saveTemplate(input: $input) { id } }",
        {"input": input_obj},
    )
    return data["saveTemplate"]["id"]


def create_endpoint(name: str, template_id: str, network_volume_id: str | None = None) -> str:
    config = EndpointConfig(
        name=name,
        template_id=template_id,
        gpu_ids=GPU_TYPE,
        workers_min=WORKERS_MIN,
        workers_max=WORKERS_MAX,
        idle_timeout=IDLE_TIMEOUT,
        execution_timeout=EXECUTION_TIMEOUT,
        scaler_type=SCALER_TYPE,
        scaler_value=SCALER_VALUE,
        container_disk_gb=CONTAINER_DISK_GB,
        network_volume_id=network_volume_id,
    )
    input_obj = build_endpoint_input(config)
    data = _gql(
        "mutation SaveEndpoint($input: EndpointInput!) { saveEndpoint(input: $input) { id } }",
        {"input": input_obj},
    )
    return data["saveEndpoint"]["id"]


def get_endpoint_health(endpoint_id: str) -> dict[str, Any]:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/health"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "RunPod-Canary/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        return {"jobs": {}, "workers": {}}


def run_job(endpoint_id: str, input_data: dict[str, Any]) -> str:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    body = json.dumps({"input": input_data, "policy": build_job_policy()}).encode()
    url = f"https://api.runpod.ai/v2/{endpoint_id}/run"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "RunPod-Canary/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["id"]


def get_job_status(endpoint_id: str, job_id: str) -> dict[str, Any]:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "RunPod-Canary/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_endpoint_by_id(endpoint_id: str) -> dict[str, Any]:
    data = _gql(
        "query GetEndpoints { myself { endpoints { id name templateId workersMin workersMax } } }",
    )
    for ep in data["myself"]["endpoints"]:
        if ep["id"] == endpoint_id:
            return ep
    raise RuntimeError(f"Endpoint {endpoint_id} not found")


def update_endpoint_workers(endpoint_id: str, workers_min: int, workers_max: int) -> dict[str, Any]:
    ep = _get_endpoint_by_id(endpoint_id)
    input_obj = {
        "id": endpoint_id,
        "name": ep["name"],
        "templateId": ep["templateId"],
        "workersMin": workers_min,
        "workersMax": workers_max,
    }
    data = _gql(
        "mutation SaveEndpoint($input: EndpointInput!) { saveEndpoint(input: $input) { id workersMin workersMax } }",
        {"input": input_obj},
    )
    return data["saveEndpoint"]


def delete_endpoint(endpoint_id: str) -> None:
    _gql(f'mutation {{ deleteEndpoint(id: "{endpoint_id}") }}')


# --- Main canary flow --------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Live production canary")
    parser.add_argument("--sha", required=True, help="Git SHA for the image tag")
    parser.add_argument("--template-id", default=None, help="Existing template ID to reuse")
    parser.add_argument("--image-tag", default=None, help="Full image tag")
    args = parser.parse_args()

    sha = args.sha
    image_tag = args.image_tag or f"ghcr.io/airyder/fincept/quant-foundry-training:{sha}"
    callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not callback_secret:
        print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET not set")
        return 1

    receipt_dir = Path(f"reports/runpod-test-runs/{sha[:8]}/live-canary")
    receipt_dir.mkdir(parents=True, exist_ok=True)

    print("Live Production Canary")
    print(f"  SHA: {sha}")
    print(f"  Image: {image_tag}")
    print(f"  GPU: {GPU_TYPE}")
    print(f"  Receipts: {receipt_dir}")
    print()

    # Create or reuse template
    if args.template_id:
        template_id = args.template_id
        print(f"  Reusing template: {template_id}")
    else:
        template_name = make_unique_name("qf-canary", sha, suffix="tpl")
        env_vars = [
            {"key": "PYTHONUNBUFFERED", "value": "1"},
            {"key": "PYTHONPATH", "value": "/worker"},
            {"key": "QUANT_FOUNDRY_GIT_SHA", "value": sha},
            {"key": "QUANT_FOUNDRY_CALLBACK_SECRET", "value": callback_secret},
        ]
        template_id = save_template(template_name, image_tag, env_vars, REGISTRY_AUTH_ID)
        print(f"  Template created: {template_id}")

    # Write template receipt
    (receipt_dir / "template-redacted.txt").write_text(
        f"Template ID: {template_id}\nImage: {image_tag}\nGPU: {GPU_TYPE}\n",
        encoding="utf-8",
    )

    # Create endpoint
    endpoint_name = make_unique_name("qf-canary", sha)
    endpoint_id = create_endpoint(endpoint_name, template_id)
    print(f"  Endpoint created: {endpoint_id}")

    # Write endpoint receipt
    (receipt_dir / "endpoint-create-redacted.json").write_text(
        json.dumps(
            _redact(
                {
                    "endpoint_id": endpoint_id,
                    "name": endpoint_name,
                    "template_id": template_id,
                    "gpu_type": GPU_TYPE,
                    "workers_min": WORKERS_MIN,
                    "workers_max": WORKERS_MAX,
                    "idle_timeout": IDLE_TIMEOUT,
                    "execution_timeout": EXECUTION_TIMEOUT,
                    "scaler_type": SCALER_TYPE,
                    "scaler_value": SCALER_VALUE,
                    "timeout_config": format_timeout_receipt(EXECUTION_TIMEOUT, IDLE_TIMEOUT),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        # Wait for ready
        print(f"  Waiting for ready (timeout={READY_TIMEOUT_S}s)...")
        health_before = None
        for i in range(READY_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            workers = health.get("workers", {})
            ready = workers.get("ready", 0)
            unhealthy = workers.get("unhealthy", 0)
            print(
                f"    [{i * POLL_INTERVAL_S}] ready={ready} idle={workers.get('idle', 0)} "
                f"running={workers.get('running', 0)} unhealthy={unhealthy} "
                f"initializing={workers.get('initializing', 0)}"
            )
            if ready >= 1 and unhealthy == 0:
                health_before = health
                break
            if unhealthy > 0:
                print("  FAIL: worker went unhealthy before dispatch")
                (receipt_dir / "health-before.json").write_text(
                    json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8"
                )
                return 1
            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: worker not ready after {READY_TIMEOUT_S}s")
            (receipt_dir / "health-before.json").write_text(
                json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8"
            )
            return 1

        print(
            f"  Health before: ready={workers.get('ready', 0)} idle={workers.get('idle', 0)} unhealthy={workers.get('unhealthy', 0)}"
        )
        (receipt_dir / "health-before.json").write_text(
            json.dumps(_redact(health_before), indent=2, sort_keys=True), encoding="utf-8"
        )

        # Dispatch callback_secret_canary
        canary_input = {
            "task": "callback_secret_canary",
            "job_id": f"qf:canary:{sha[:8]}",
            "nonce": f"n-{int(time.time())}",
        }
        job_id = run_job(endpoint_id, canary_input)
        print(f"  Job dispatched: {job_id}")
        (receipt_dir / "run-response.json").write_text(
            json.dumps(_redact({"job_id": job_id, "input": canary_input}), indent=2),
            encoding="utf-8",
        )

        # Poll until terminal
        probe_log = []
        final_status = "UNKNOWN"
        for i in range(PROBE_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            workers = health.get("workers", {})
            status_resp = get_job_status(endpoint_id, job_id)
            job_status = status_resp.get("status", "UNKNOWN")
            final_status = job_status

            probe_log.append(
                {
                    "event": "poll",
                    "job_id": job_id,
                    "status": job_status,
                    "health": _redact(health),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )

            print(
                f"  [{i * POLL_INTERVAL_S}] status={job_status} "
                f"ready={workers.get('ready', 0)} running={workers.get('running', 0)} "
                f"unhealthy={workers.get('unhealthy', 0)} "
                f"inQueue={health.get('jobs', {}).get('inQueue', 0)} "
                f"completed={health.get('jobs', {}).get('completed', 0)}"
            )

            if workers.get("unhealthy", 0) > 0:
                print("  FAIL: worker went unhealthy")
                break

            if job_status == "COMPLETED":
                print("  PASS: job COMPLETED")
                break

            if job_status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                print(f"  FAIL: job {job_status}")
                break

            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: probe timed out (job stuck in {final_status})")

        # Write probe log and final status
        (receipt_dir / "probe.jsonl").write_text(
            "\n".join(json.dumps(e) for e in probe_log) + "\n",
            encoding="utf-8",
        )
        (receipt_dir / "status-final.json").write_text(
            json.dumps(_redact(get_job_status(endpoint_id, job_id)), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Final health
        health_after = get_endpoint_health(endpoint_id)
        print(
            f"  Health after: ready={health_after['workers'].get('ready', 0)} "
            f"unhealthy={health_after['workers'].get('unhealthy', 0)}"
        )
        (receipt_dir / "health-after.json").write_text(
            json.dumps(_redact(health_after), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Result
        if final_status == "COMPLETED":
            print("\n  CANARY PASSED")
            return 0
        else:
            print(f"\n  CANARY FAILED (final_status={final_status})")
            return 1

    finally:
        # Scale down and delete (with retry on transient failures)
        scaled_down = safe_scale_to_zero(
            endpoint_id,
            update_endpoint_workers,
            logger=print,
        )
        endpoint_deleted = retry_delete_endpoint(
            endpoint_id,
            delete_endpoint,
            logger=print,
        )
        (receipt_dir / "cleanup.json").write_text(
            json.dumps(
                {
                    "endpoint_id": endpoint_id,
                    "scaled_down": scaled_down,
                    "deleted": endpoint_deleted,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    sys.exit(main())
