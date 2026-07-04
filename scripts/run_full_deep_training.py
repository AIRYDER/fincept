"""Full deep RunPod training run — end-to-end through the gateway pipeline.

This script exercises the COMPLETE training pipeline locally, without needing
real RunPod credentials or infrastructure:

1. Construct the QuantFoundryGateway (local_mock mode, enabled)
2. Enqueue a training job in the outbox
3. Run the REAL RunPod training handler locally with RealLightGBMTrainer
   (same code path as the RunPod container — HMAC-signed callback, worker
   status files, heartbeat thread, deadline enforcement)
4. Feed the signed callback back to the gateway via receive_callback()
5. Verify the callback was processed (outbox → COMPLETED, dossier registered)
6. Print a full summary

This is the deepest training run possible without real RunPod GPU infrastructure.
The only difference from production is that the handler runs in-process instead
of on a remote RunPod serverless worker.

Usage:
    uv run python scripts/run_full_deep_training.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

# Bootstrap paths
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

# Shared runpod utilities (worker_status)
_SHARED = _REPO_ROOT / "runpod" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from quant_foundry.gateway import QuantFoundryGateway  # noqa: E402
from quant_foundry.real_trainer import RealLightGBMTrainer  # noqa: E402
from quant_foundry.runpod_training import RunPodTrainingHandler  # noqa: E402
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402


def main() -> int:
    # --- 1. Set up environment for the gateway ---
    callback_secret = "local-deep-training-test-secret"
    base_dir = _REPO_ROOT / "data" / "deep_training_run"
    status_dir = base_dir / "worker_status"

    # Clean previous run
    import shutil

    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)

    os.environ["QUANT_FOUNDRY_ENABLED"] = "true"
    os.environ["QUANT_FOUNDRY_MODE"] = "local_mock"
    os.environ["QUANT_FOUNDRY_SHADOW_ONLY"] = "true"
    os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = callback_secret
    os.environ["QUANT_FOUNDRY_BASE_DIR"] = str(base_dir)
    os.environ["QUANT_FOUNDRY_WORKER_STATUS_DIR"] = str(status_dir)
    os.environ["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

    # --- 2. Construct the gateway ---
    gateway = QuantFoundryGateway.from_env(base_dir=base_dir)
    print("=" * 70)
    print("FULL DEEP TRAINING RUN")
    print("=" * 70)
    print(f"  Gateway mode:    {gateway.mode}")
    print(f"  Gateway enabled: {gateway.enabled}")
    print(f"  Base dir:        {base_dir}")
    print(f"  Status dir:      {status_dir}")
    print(f"  Callback secret: {callback_secret[:8]}...")
    print()

    # --- 3. Prepare the dataset ---
    dataset_parquet = (
        _REPO_ROOT
        / "data"
        / "datasets"
        / "backtest_synthetic"
        / "synthetic_s5_d500_h5d_seed42.parquet"
    )
    if not dataset_parquet.exists():
        print(f"ERROR: dataset not found: {dataset_parquet}")
        return 1

    dataset_ref = f"file://{dataset_parquet.as_posix()}"
    job_id = "deep-training-run-001"
    idempotency_key = f"idemp-{job_id}-{int(time.time())}"

    # --- 4. Build the training request ---
    request_payload = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": dataset_ref,
        "model_family": "lightgbm",
        "random_seed": 42,
        "extra_constraints": {"bar_seconds": "86400"},
    }
    req = RunPodTrainingRequest.model_validate(request_payload)

    print("Training Request:")
    print(f"  job_id:           {req.job_id}")
    print(f"  model_family:     {req.model_family}")
    print(f"  dataset_ref:      {req.dataset_manifest_ref[:80]}...")
    print(f"  random_seed:      {req.random_seed}")
    print(f"  idempotency_key:  {idempotency_key}")
    print()

    # --- 5. Enqueue the job in the outbox ---
    # We bypass create_job() (which auto-dispatches in local_mock mode with
    # the mock trainer) and instead enqueue directly, then run the REAL
    # handler ourselves, then feed the callback back.
    from quant_foundry.outbox import JobStatus

    gateway.outbox.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        priority=0,
        budget_cents=0,
    )
    print(f"[outbox] Job enqueued: {job_id}")
    rec = gateway.outbox.get(job_id)
    print(f"[outbox] Status: {rec.status.value}")
    print()

    # --- 6. Run the REAL RunPod training handler ---
    print("-" * 70)
    print("RUNNING REAL LIGHTGBM TRAINING (via RunPodTrainingHandler)")
    print("-" * 70)

    trainer = RealLightGBMTrainer(n_folds=3, annualization_factor=252)
    handler = RunPodTrainingHandler(
        callback_secret=callback_secret,
        trainer=trainer,
        deadline_seconds=600,  # 10-minute deadline
        worker_id="local-deep-worker-1",
    )

    # Write worker status: started
    try:
        from worker_status import write_status

        write_status(job_id, "started")
        print(f"[worker_status] wrote 'started' for {job_id}")
    except ImportError:
        print("[worker_status] module not available, skipping")

    start_ns = time.time_ns()
    result = handler.handle(req)
    elapsed_s = (time.time_ns() - start_ns) / 1_000_000_000

    # Write worker status: completed
    try:
        from worker_status import write_status

        write_status(job_id, "completed", artifact_id=result.artifact_id)
        print(f"[worker_status] wrote 'completed' for {job_id}")
    except ImportError:
        pass

    print(f"\n[handler] Training completed in {elapsed_s:.1f}s")
    print(f"[handler] artifact_id: {result.artifact_id}")
    print(f"[handler] dossier_id:  {result.dossier_id}")
    print(f"[handler] callback_ts: {result.callback_ts}")
    print()

    # --- 7. Parse the callback envelope ---
    envelope = json.loads(result.callback_payload)
    payload_dict = envelope
    print("-" * 70)
    print("CALLBACK ENVELOPE")
    print("-" * 70)
    print(f"  job_id:      {envelope['job_id']}")
    print(f"  worker_id:   {envelope['worker_id']}")
    print(f"  result_type: {envelope['result_type']}")

    dossier_data = envelope["payload"]["dossier"]
    artifact_data = envelope["payload"]["artifact_manifest"]

    print("\n  Artifact:")
    print(f"    artifact_id:    {artifact_data['artifact_id']}")
    print(f"    sha256:         {artifact_data['sha256'][:16]}...")
    print(f"    size_bytes:     {artifact_data['size_bytes']:,}")
    print(f"    model_family:   {artifact_data['model_family']}")

    print("\n  Dossier:")
    print(f"    model_id:       {dossier_data['model_id']}")
    print(f"    authority:      {dossier_data['authority']}")
    metrics = dossier_data["training_metrics"]
    print(f"    accuracy:       {metrics.get('accuracy', 'n/a'):.6f}")
    print(f"    logloss:        {metrics.get('logloss', 'n/a'):.6f}")
    print(f"    brier_score:    {dossier_data['metadata'].get('brier_score', 'n/a')}")
    print(f"    win_rate:       {dossier_data['metadata'].get('win_rate', 'n/a')}")
    print(f"    sharpe:         {dossier_data['metadata'].get('sharpe_ratio', 'n/a')}")
    print(f"    max_drawdown:   {dossier_data['metadata'].get('max_drawdown', 'n/a')}")
    print(f"    pbo:            {dossier_data['pbo']}")
    print(f"    deflated_sharpe:{dossier_data['deflated_sharpe']}")
    print()

    # --- 8. Feed the callback to the gateway ---
    print("-" * 70)
    print("PROCESSING CALLBACK THROUGH GATEWAY")
    print("-" * 70)

    callback_payload_bytes = result.callback_payload

    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=callback_payload_bytes,
        signature=result.callback_signature,
        ts=result.callback_ts,
        worker_id="local-deep-worker-1",
    )

    print(f"  ok:             {receipt.get('ok')}")
    print(f"  job_id:         {receipt.get('job_id')}")
    print(f"  inbox_status:   {receipt.get('inbox_status')}")
    print(f"  outbox_status:  {receipt.get('outbox_status')}")
    print()

    # --- 9. Verify the outbox status ---
    print("-" * 70)
    print("VERIFICATION")
    print("-" * 70)

    final_rec = gateway.outbox.get(job_id)
    print(f"  Outbox status:  {final_rec.status.value}")
    print("  Expected:       completed")

    if final_rec.status != JobStatus.COMPLETED:
        print("\n  ERROR: job did not reach COMPLETED state!")
        print(f"  error_code:    {final_rec.error_code}")
        print(f"  error_summary: {final_rec.error_summary}")
        return 1

    # --- 10. Check dossier registry ---
    try:
        registry = gateway.dossier_registry()
        dossiers = registry.list()
        print(f"  Dossier registry: {len(dossiers)} dossier(s) registered")
        if dossiers:
            d = dossiers[-1]
            print(f"    model_id:    {d.model_id}")
            print(f"    status:      {d.status.value}")
            print(f"    authority:   {d.authority}")
    except Exception as exc:
        print(f"  Dossier registry check: {exc}")

    # --- 11. Check worker status files ---
    status_files = list(status_dir.glob("*.json"))
    print(f"  Worker status files: {len(status_files)}")
    for sf in status_files:
        print(f"    {sf.name}")

    # --- 12. Save results ---
    results_dir = base_dir / "results"
    results_dir.mkdir(exist_ok=True)

    (results_dir / "callback_envelope.json").write_text(
        json.dumps(envelope, indent=2), encoding="utf-8"
    )
    (results_dir / "artifact_manifest.json").write_text(
        json.dumps(artifact_data, indent=2), encoding="utf-8"
    )
    (results_dir / "dossier.json").write_text(json.dumps(dossier_data, indent=2), encoding="utf-8")
    (results_dir / "gateway_receipt.json").write_text(
        json.dumps(receipt, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n  Results saved to: {results_dir}")

    print(f"\n{'=' * 70}")
    print(f"FULL DEEP TRAINING RUN COMPLETE ({elapsed_s:.1f}s training)")
    print(f"{'=' * 70}")
    print(f"\n  Job:          {job_id}")
    print(f"  Artifact:     {result.artifact_id}")
    print(f"  Dossier:      {result.dossier_id}")
    print(f"  Outbox:       {final_rec.status.value}")
    print(f"  Authority:    {dossier_data['authority']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
