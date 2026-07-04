"""Live GPU healthcheck probe for the RunPod training worker.

Creates a fresh endpoint with the exact SHA image, waits for ready=1,
dispatches a ``gpu_healthcheck`` job (mode=canary), polls health/status
until terminal, captures the GPU runtime metadata from the job output,
scales down, deletes the endpoint, and writes a receipt bundle.

Reuses the RunPod API helpers from ``run_live_canary.py`` (same GraphQL
endpoint, same bearer auth, same redaction, same endpoint/template shape).

Usage:
    python runpod/quant-foundry-training/run_gpu_healthcheck.py --sha <full-sha>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Reuse the validated RunPod API helpers from the canary tool.
from run_live_canary import (
    CONTAINER_DISK_GB,
    GPU_TYPE,
    IDLE_TIMEOUT,
    POLL_INTERVAL_S,
    PROBE_TIMEOUT_S,
    READY_TIMEOUT_S,
    REGISTRY_AUTH_ID,
    SCALER_TYPE,
    SCALER_VALUE,
    WORKERS_MAX,
    WORKERS_MIN,
    _redact,
    create_endpoint,
    delete_endpoint,
    get_endpoint_health,
    get_job_status,
    run_job,
    save_template,
    update_endpoint_workers,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live GPU healthcheck probe")
    parser.add_argument("--sha", required=True, help="Full git SHA for the image tag")
    parser.add_argument("--template-id", default=None, help="Existing template ID to reuse")
    parser.add_argument("--image-tag", default=None, help="Full image tag (overrides --sha)")
    parser.add_argument(
        "--receipt-subdir",
        default="gpu-healthcheck",
        help="Receipt subdirectory under reports/runpod-test-runs/<sha8>/",
    )
    args = parser.parse_args()

    sha = args.sha
    image_tag = args.image_tag or f"ghcr.io/airyder/fincept/quant-foundry-training:{sha}"
    callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not callback_secret:
        print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET not set")
        return 1

    receipt_dir = Path(f"reports/runpod-test-runs/{sha[:8]}/{args.receipt_subdir}")
    receipt_dir.mkdir(parents=True, exist_ok=True)

    print("Live GPU Healthcheck Probe")
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
        template_name = f"qf-gpuhc-{sha[:8]}-tpl"
        env_vars = [
            {"key": "PYTHONUNBUFFERED", "value": "1"},
            {"key": "PYTHONPATH", "value": "/worker"},
            {"key": "QUANT_FOUNDRY_GIT_SHA", "value": sha},
            {"key": "QUANT_FOUNDRY_CALLBACK_SECRET", "value": callback_secret},
        ]
        template_id = save_template(template_name, image_tag, env_vars, REGISTRY_AUTH_ID)
        print(f"  Template created: {template_id}")

    (receipt_dir / "template-redacted.txt").write_text(
        f"Template ID: {template_id}\nImage: {image_tag}\nGPU: {GPU_TYPE}\n",
        encoding="utf-8",
    )

    # Create endpoint
    endpoint_name = f"qf-gpuhc-{sha[:8]}"
    endpoint_id = create_endpoint(endpoint_name, template_id)
    print(f"  Endpoint created: {endpoint_id}")

    (receipt_dir / "endpoint-create-redacted.json").write_text(
        json.dumps(_redact({
            "endpoint_id": endpoint_id,
            "name": endpoint_name,
            "template_id": template_id,
            "gpu_type": GPU_TYPE,
            "workers_min": WORKERS_MIN,
            "workers_max": WORKERS_MAX,
            "idle_timeout": IDLE_TIMEOUT,
            "scaler_type": SCALER_TYPE,
            "scaler_value": SCALER_VALUE,
            "container_disk_gb": CONTAINER_DISK_GB,
        }), indent=2),
        encoding="utf-8",
    )

    job_id: str | None = None
    try:
        # Wait for ready
        print(f"  Waiting for ready (timeout={READY_TIMEOUT_S}s)...")
        health_before = None
        workers: dict[str, Any] = {}
        for i in range(READY_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            workers = health.get("workers", {})
            ready = workers.get("ready", 0)
            unhealthy = workers.get("unhealthy", 0)
            print(f"    [{i*POLL_INTERVAL_S}] ready={ready} idle={workers.get('idle',0)} "
                  f"running={workers.get('running',0)} unhealthy={unhealthy} "
                  f"initializing={workers.get('initializing',0)}")
            if ready >= 1 and unhealthy == 0:
                health_before = health
                break
            if unhealthy > 0:
                print("  FAIL: worker went unhealthy before dispatch")
                (receipt_dir / "health-before.json").write_text(
                    json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8")
                return 1
            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: worker not ready after {READY_TIMEOUT_S}s")
            (receipt_dir / "health-before.json").write_text(
                json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8")
            return 1

        print(f"  Health before: ready={workers.get('ready',0)} "
              f"idle={workers.get('idle',0)} unhealthy={workers.get('unhealthy',0)}")
        (receipt_dir / "health-before.json").write_text(
            json.dumps(_redact(health_before), indent=2, sort_keys=True), encoding="utf-8")

        # Dispatch gpu_healthcheck (canary mode — succeeds even without GPU)
        hc_input = {
            "task": "gpu_healthcheck",
            "mode": "canary",
            "job_id": f"qf:gpu-hc:{sha[:8]}:001",
        }
        job_id = run_job(endpoint_id, hc_input)
        print(f"  Job dispatched: {job_id}")
        (receipt_dir / "run-response.json").write_text(
            json.dumps(_redact({"job_id": job_id, "input": hc_input}), indent=2),
            encoding="utf-8",
        )

        # Poll until terminal
        probe_log: list[dict[str, Any]] = []
        final_status = "UNKNOWN"
        final_output: dict[str, Any] | None = None
        for i in range(PROBE_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            w = health.get("workers", {})
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

            print(f"  [{i*POLL_INTERVAL_S}] status={job_status} "
                  f"ready={w.get('ready',0)} running={w.get('running',0)} "
                  f"unhealthy={w.get('unhealthy',0)} "
                  f"inQueue={health.get('jobs',{}).get('inQueue',0)} "
                  f"completed={health.get('jobs',{}).get('completed',0)}")

            if w.get("unhealthy", 0) > 0:
                print("  FAIL: worker went unhealthy")
                final_output = status_resp
                break

            if job_status == "COMPLETED":
                print("  PASS: job COMPLETED")
                final_output = status_resp
                break

            if job_status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                print(f"  FAIL: job {job_status}")
                final_output = status_resp
                break

            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: probe timed out (job stuck in {final_status})")
            final_output = get_job_status(endpoint_id, job_id) if job_id else None

        # Write probe log and final status
        (receipt_dir / "probe.jsonl").write_text(
            "\n".join(json.dumps(e) for e in probe_log) + "\n",
            encoding="utf-8",
        )
        (receipt_dir / "status-final.json").write_text(
            json.dumps(_redact(final_output), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Extract and save the GPU healthcheck result from the job output
        gpu_result: dict[str, Any] | None = None
        if final_output:
            output_field = final_output.get("output")
            if isinstance(output_field, str):
                try:
                    parsed_output = json.loads(output_field)
                    gpu_result = parsed_output.get("gpu_healthcheck")
                except json.JSONDecodeError:
                    pass
            elif isinstance(output_field, dict):
                gpu_result = output_field.get("gpu_healthcheck")

        if gpu_result is not None:
            (receipt_dir / "gpu-healthcheck-result.json").write_text(
                json.dumps(_redact(gpu_result), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"  GPU capable: {gpu_result.get('gpu_capable')}")
            print(f"  GPU model:   {gpu_result.get('gpu_model')}")
            print(f"  GPU count:   {gpu_result.get('gpu_count')}")
            print(f"  CUDA ver:    {gpu_result.get('cuda_version')}")
            print(f"  Driver ver:  {gpu_result.get('driver_version')}")
            print(f"  GPU mem MB:  {gpu_result.get('gpu_memory_mb')}")
            print(f"  Promotion:   {gpu_result.get('promotion_eligible')}")
        else:
            print("  WARNING: no gpu_healthcheck result in job output")

        # Final health
        health_after = get_endpoint_health(endpoint_id)
        print(f"  Health after: ready={health_after.get('workers',{}).get('ready',0)} "
              f"unhealthy={health_after.get('workers',{}).get('unhealthy',0)}")
        (receipt_dir / "health-after.json").write_text(
            json.dumps(_redact(health_after), indent=2, sort_keys=True), encoding="utf-8",
        )

        # Result
        if final_status == "COMPLETED":
            print("\n  GPU HEALTHCHECK PASSED")
            return 0
        else:
            print(f"\n  GPU HEALTHCHECK FAILED (final_status={final_status})")
            return 1

    finally:
        # Cancel stuck job if not terminal
        if job_id and final_status not in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            try:
                api_key = os.environ.get("RUNPOD_API_KEY", "")
                import urllib.request
                url = f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}"
                req = urllib.request.Request(url, method="POST", headers={
                    "Authorization": f"Bearer {api_key}",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cancel_resp = json.loads(resp.read())
                (receipt_dir / "cancel.json").write_text(
                    json.dumps(_redact(cancel_resp), indent=2), encoding="utf-8")
                print(f"  Cancelled stuck job: {job_id}")
            except Exception as e:
                print(f"  WARNING: could not cancel job {job_id}: {e}")

        # Scale down and delete
        try:
            update_endpoint_workers(endpoint_id, 0, 0)
            print("  Scaled down to 0/0")
        except Exception as e:
            print(f"  WARNING: could not scale down: {e}")
        try:
            delete_endpoint(endpoint_id)
            print("  Endpoint deleted")
        except Exception as e:
            print(f"  WARNING: could not delete endpoint: {e}")
        (receipt_dir / "cleanup.json").write_text(
            json.dumps({"endpoint_id": endpoint_id, "scaled_down": True, "deleted": True}, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    sys.exit(main())
