"""Test script: dispatch a real training job to RunPod and poll for results (longer window)."""
from __future__ import annotations

import json
import os
import time

from quant_foundry.runpod_client import HttpRunPodClient, DispatchStatus


def main() -> None:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
    base_url = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")

    if not api_key or not endpoint_id:
        print("ERROR: RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID must be set.")
        return

    client = HttpRunPodClient(
        api_key=api_key,
        endpoint_id=endpoint_id,
        base_url=base_url,
        timeout_seconds=60.0,
    )

    # Check health first
    print("=== RunPod endpoint health ===")
    try:
        health = client.check_health()
        print(json.dumps(health, indent=2))
    except Exception as e:
        print(f"Health check failed: {e}")
        return

    # Dispatch a test training job
    print("\n=== Dispatching test training job ===")
    result = client.dispatch(
        job_id="qf:train:runpod:test:2",
        request_payload={
            "job_id": "qf:train:runpod:test:2",
            "dataset_manifest_ref": "ds-test-1",
            "model_family": "gbm",
            "search_space": {"n_estimators": [100]},
            "random_seed": 42,
            "hardware_class": "rtx-4090",
        },
        budget_cents=50,
    )

    print(f"  status: {result.status}")
    print(f"  runpod_job_id: {result.runpod_job_id}")
    print(f"  error_code: {result.error_code}")
    print(f"  error_summary: {result.error_summary}")

    if result.status != DispatchStatus.DISPATCHED:
        print("\nDispatch failed. Check error above.")
        return

    runpod_job_id = result.runpod_job_id
    print(f"\n  -> Job dispatched! RunPod job ID: {runpod_job_id}")

    # Poll for status - 30 attempts x 15s = 7.5 min max
    print("\n=== Polling job status (30 attempts, 15s interval) ===")
    for attempt in range(30):
        time.sleep(15)
        try:
            status = client.check_status(runpod_job_id)
            state = status.get("status", "UNKNOWN")
            print(f"  [{attempt+1}/30] status: {state}", end="")
            if state == "COMPLETED":
                print("\n\n=== Job completed! ===")
                output = status.get("output", {})
                print(json.dumps(output, indent=2)[:3000])
                return
            if state == "FAILED":
                print("\n\n=== Job FAILED ===")
                error = status.get("error", "")
                print(f"  error: {error}")
                return
            # Check worker health
            health = client.check_health()
            workers = health.get("workers", {})
            print(f"  workers: init={workers.get('initializing',0)} ready={workers.get('ready',0)} running={workers.get('running',0)} unhealthy={workers.get('unhealthy',0)} throttled={workers.get('throttled',0)}")
        except Exception as e:
            print(f"  [{attempt+1}/30] poll error: {e}")

    print("\nJob did not complete within polling window. Check RunPod console.")


if __name__ == "__main__":
    main()
