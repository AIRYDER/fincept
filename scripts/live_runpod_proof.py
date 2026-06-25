"""Live RunPod end-to-end proof for the Quant Foundry gateway.

Runs against the REAL RunPod endpoints using env vars.
Tests: dispatch → poll → callback ingest → durable store visible.
"""
from __future__ import annotations

import json
import time
import uuid

from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus


def main() -> None:
    gateway = QuantFoundryGateway.from_env()
    health = gateway.health()
    print("=== Gateway Health ===")
    print(json.dumps(health, indent=2, default=str))

    if not health.get("enabled"):
        print("ERROR: Quant Foundry is not enabled")
        return

    runpod_health = gateway.runpod_health()
    print("\n=== RunPod Endpoint Health ===")
    print(json.dumps(runpod_health, indent=2, default=str))

    # --- Inference job (endpoint has a ready worker right now) ---
    print("\n=== Dispatching Inference Job ===")
    inference_job_id = f"qf:infer:live:{uuid.uuid4().hex[:8]}"
    inference_payload = {
        "job_id": inference_job_id,
        "artifact_ref": "file:///mock-model.pkl",
        "symbols": ["AAPL"],
        "horizons_ns": [3_600_000_000_000],
        "feature_rows": [
            {
                "symbol": "AAPL",
                "event_ts": 1_000_000_000,
                "decision_time": 1_000_000_000,
                "features": [
                    {"name": "rsi_14", "value": 55.0, "observed_at": 999_000_000},
                    {"name": "volume_zscore", "value": 0.3, "observed_at": 999_000_000},
                ],
                "label_horizon_ns": 86_400_000_000_000,
            }
        ],
        "model_id": "live-test-model-1",
    }

    result = gateway.create_job(
        job_id=inference_job_id,
        job_type="inference",
        idempotency_key=f"idem-{inference_job_id}",
        request_payload=inference_payload,
    )
    print(json.dumps(result, indent=2, default=str))

    if result.get("error_code"):
        print("ERROR: Inference job dispatch failed")
        return

    # Poll for completion
    print("\n=== Polling for Inference Completion ===")
    for i in range(40):
        time.sleep(5)
        receipts = gateway.poll_runpod_results()
        if receipts:
            for r in receipts:
                print(f"  poll {i+1}: {json.dumps(r, indent=2, default=str)}")
            # Check if our job completed
            rec = gateway.outbox.get(inference_job_id)
            if rec and rec.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                print(f"\n  Job final status: {rec.status}")
                break
        else:
            rec = gateway.outbox.get(inference_job_id)
            status = rec.status if rec else "unknown"
            print(f"  poll {i+1}: no receipts, job status={status}")
            if rec and rec.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
    else:
        print("  TIMEOUT: job did not complete in 200s")

    # Check shadow health
    print("\n=== Shadow Health ===")
    shadow_health = gateway.shadow_health()
    print(json.dumps(shadow_health, indent=2, default=str))

    # Check dossiers
    print("\n=== Dossiers ===")
    dossiers = gateway.list_dossiers()
    print(f"  dossier count: {len(dossiers)}")
    if dossiers:
        print(f"  first: {json.dumps(dossiers[0], indent=2, default=str)[:500]}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
