"""Live RunPod end-to-end proof: training + inference loop.

Runs against REAL RunPod endpoints using env vars.
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

    # --- Training job ---
    print("\n=== Dispatching Training Job ===")
    train_job_id = f"qf:train:live:{uuid.uuid4().hex[:8]}"
    train_payload = {
        "schema_version": 1,
        "job_id": train_job_id,
        "dataset_manifest_ref": "ds-live-test-1",
        "model_family": "gbm",
        "search_space": {"n_estimators": [100, 200]},
        "random_seed": 42,
        "hardware_class": "live-gpu",
    }
    result = gateway.create_job(
        job_id=train_job_id,
        job_type="training",
        idempotency_key=f"idem-{train_job_id}",
        request_payload=train_payload,
    )
    print(json.dumps(result, indent=2, default=str))

    # --- Inference job ---
    print("\n=== Dispatching Inference Job ===")
    infer_job_id = f"qf:infer:live:{uuid.uuid4().hex[:8]}"
    infer_payload = {
        "job_id": infer_job_id,
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
        job_id=infer_job_id,
        job_type="inference",
        idempotency_key=f"idem-{infer_job_id}",
        request_payload=infer_payload,
    )
    print(json.dumps(result, indent=2, default=str))

    # Poll for completion
    print("\n=== Polling for Completion ===")
    train_done = False
    infer_done = False
    for i in range(60):  # 5 min max
        time.sleep(5)
        receipts = gateway.poll_runpod_results()
        if receipts:
            for r in receipts:
                print(f"  poll {i+1}: {json.dumps(r, indent=2, default=str)}")
        else:
            print(f"  poll {i+1}: no receipts")

        train_rec = gateway.outbox.get(train_job_id)
        infer_rec = gateway.outbox.get(infer_job_id)
        train_status = train_rec.status if train_rec else "unknown"
        infer_status = infer_rec.status if infer_rec else "unknown"
        print(f"  training={train_status}, inference={infer_status}")

        if train_rec and train_rec.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            train_done = True
        if infer_rec and infer_rec.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            infer_done = True
        if train_done and infer_done:
            break

    # Final results
    print("\n=== Shadow Health ===")
    print(json.dumps(gateway.shadow_health(), indent=2, default=str))

    print("\n=== Dossiers ===")
    dossiers = gateway.list_dossiers()
    print(f"  dossier count: {len(dossiers)}")
    for d in dossiers:
        print(f"  {json.dumps(d, default=str)[:300]}")

    print("\n=== Jobs ===")
    for j in gateway.list_jobs():
        print(f"  {j['job_id']}: {j['status']} ({j['job_type']})")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
