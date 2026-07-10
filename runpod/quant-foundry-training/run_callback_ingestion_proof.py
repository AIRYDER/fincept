#!/usr/bin/env python3
"""Live callback ingestion proof: dispatch → poll → ingest → verify DB rows.

This script proves the full product loop with a REAL RunPod dispatch and a
REAL signed callback ingestion into the database (SQLite for this proof;
Postgres when available). It is the live version of the E2E test
(`test_e2e_product_loop_dispatch_to_model_versions`), replacing the
MockRunPodClient with HttpRunPodClient against a live RunPod endpoint.

Flow:
  1. Create a RunPod endpoint (template + endpoint) with the image.
  2. Create an in-memory SQLite engine with all callback + registry tables.
  3. Construct a QuantFoundryGateway with sink_backend="db" + HttpRunPodClient.
  4. Dispatch a training job via gateway.create_job().
  5. Poll RunPod via gateway.poll_runpod_results() until the job completes.
  6. Verify the callback landed in model_dossiers, callback_receipts,
     artifact_manifests, and training_jobs.
  7. Register a model_version from the dossier.
  8. Clean up the endpoint + template.

Usage:
    python runpod/quant-foundry-training/run_callback_ingestion_proof.py \\
        --sha 34d85c10a52cf59f8bd30d4d8ab7474b2cc53f9e

Requires:
    QUANT_FOUNDRY_CALLBACK_SECRET env var (same secret used in the image).
    RUNPOD_API_KEY env var.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add repo paths to sys.path
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# RunPod lifecycle helpers (same as run_train_model.py)
sys.path.insert(0, str(_REPO_ROOT / "runpod" / "quant-foundry-training"))

from run_live_canary import (  # noqa: E402
    GPU_TYPE,
    POLL_INTERVAL_S,
    REGISTRY_AUTH_ID,
    _redact,
    create_endpoint,
    delete_endpoint,
    get_endpoint_health,
    safe_scale_to_zero,
    save_template,
    update_endpoint_workers,
)
from runpod.runpod_lifecycle import (  # noqa: E402
    make_unique_name,
    retry_delete_endpoint,
)

# Quant Foundry imports
sys.path.insert(0, str(_REPO_ROOT / "services" / "quant_foundry" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "libs" / "fincept-core" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "libs" / "fincept-db" / "src"))

from quant_foundry.budget import BudgetGuard  # noqa: E402
from quant_foundry.cost_tracker import CostTracker  # noqa: E402
from quant_foundry.gateway import QuantFoundryGateway  # noqa: E402
from quant_foundry.promotion import PromotionGate  # noqa: E402
from quant_foundry.registry_db import ModelRegistryDB  # noqa: E402
from quant_foundry.runpod_client import HttpRunPodClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy import event as sa_event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from fincept_db.callback_tables import (  # noqa: E402
    ArtifactManifestRow,
    CallbackDlqRow,
    CallbackMetricRow,
    CallbackReceiptRow,
    ModelDossierRow,
)
from fincept_db.models import Base  # noqa: E402
from fincept_db.observability import (  # noqa: E402
    CostSummaryRow,
    JobCostEventRow,
    JobMetricRow,
    TrainingJobRow,
)
from fincept_db.registry_tables import (  # noqa: E402
    ModelMetricRow,
    ModelRow,
    ModelVersionRow,
    PromotionDecisionRow,
    PromotionRow,
    ShadowEvaluationRow,
)

TRAIN_READY_TIMEOUT_S = 600
POLL_TIMEOUT_S = 600


def _make_engine():
    """In-memory SQLite engine with every table the product loop touches."""
    eng = create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        ArtifactManifestRow.__table__,
        ModelDossierRow.__table__,
        CallbackReceiptRow.__table__,
        CallbackDlqRow.__table__,
        CallbackMetricRow.__table__,
        TrainingJobRow.__table__,
        JobCostEventRow.__table__,
        JobMetricRow.__table__,
        CostSummaryRow.__table__,
        ModelRow.__table__,
        ModelVersionRow.__table__,
        ModelMetricRow.__table__,
        PromotionRow.__table__,
        PromotionDecisionRow.__table__,
        ShadowEvaluationRow.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    return eng


def _training_payload(job_id: str, sha: str) -> dict[str, Any]:
    """Build a training request payload for the RunPod dispatch.

    Only includes fields from RunPodTrainingRequest schema +
    handler-level extensions that are popped before validation.
    The schema has extra="forbid" so any extra field causes
    schema_validation_failed.
    """
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "inline://callback-proof",
        "model_family": "lightgbm",
        "search_space": {},
        "random_seed": 42,
        "hardware_class": "runpod-gpu",
        "extra_constraints": {"training_mode": "canary"},
        # Handler-level extensions (popped before schema validation):
        "inline_dataset_csv": _build_synthetic_csv(),
        "n_folds": 2,
    }


def _build_synthetic_csv(rows: int = 300, seed: int = 42) -> str:
    """Build a tiny deterministic synthetic dataset as CSV."""
    import random

    rng = random.Random(seed)
    lines = ["timestamp,f1,f2,f3,label"]
    for i in range(rows):
        ts = 1700000000 + i * 60
        f1 = rng.gauss(0, 1)
        f2 = rng.gauss(0, 1)
        f3 = rng.gauss(0, 1)
        raw = 0.5 * f1 - 0.3 * f2 + 0.2 * f3 + rng.gauss(0, 0.5)
        label = 1 if raw > 0 else 0
        lines.append(f"{ts},{f1:.6f},{f2:.6f},{f3:.6f},{label}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live callback ingestion proof (dispatch → poll → DB)"
    )
    parser.add_argument("--sha", required=True, help="Full git SHA for the image tag")
    parser.add_argument(
        "--receipt-subdir",
        default="callback-ingestion",
        help="Receipt subdirectory under reports/runpod-test-runs/<sha8>/",
    )
    args = parser.parse_args()

    sha = args.sha
    image_tag = f"ghcr.io/airyder/fincept/quant-foundry-training:{sha}"
    callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    runpod_api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not callback_secret:
        print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET not set")
        return 1
    if not runpod_api_key:
        print("ERROR: RUNPOD_API_KEY not set")
        return 1

    receipt_dir = Path(f"reports/runpod-test-runs/{sha[:8]}/{args.receipt_subdir}")
    receipt_dir.mkdir(parents=True, exist_ok=True)

    print("Live Callback Ingestion Proof")
    print(f"  SHA: {sha}")
    print(f"  Image: {image_tag}")
    print(f"  GPU: {GPU_TYPE}")
    print(f"  Receipts: {receipt_dir}")
    print()

    # --- 1. Create RunPod endpoint (template + endpoint) ---
    template_name = make_unique_name("qf-cbproof", sha, suffix="tpl")
    env_vars = [
        {"key": "PYTHONUNBUFFERED", "value": "1"},
        {"key": "PYTHONPATH", "value": "/worker"},
        {"key": "QUANT_FOUNDRY_GIT_SHA", "value": sha},
        {"key": "QUANT_FOUNDRY_CALLBACK_SECRET", "value": callback_secret},
    ]
    template_id = save_template(template_name, image_tag, env_vars, REGISTRY_AUTH_ID)
    print(f"  Template created: {template_id}")

    endpoint_name = make_unique_name("qf-cbproof", sha)
    endpoint_id = create_endpoint(endpoint_name, template_id)
    print(f"  Endpoint created: {endpoint_id}")

    (receipt_dir / "endpoint-create-redacted.json").write_text(
        json.dumps(
            _redact(
                {
                    "endpoint_id": endpoint_id,
                    "name": endpoint_name,
                    "template_id": template_id,
                    "gpu_type": GPU_TYPE,
                    "image": image_tag,
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    # --- 2. Wait for worker readiness ---
    print(f"  Waiting for ready (timeout={TRAIN_READY_TIMEOUT_S}s)...")
    health_before: dict[str, Any] = {}
    ready = False
    for i in range(TRAIN_READY_TIMEOUT_S // POLL_INTERVAL_S):
        health_before = get_endpoint_health(endpoint_id)
        workers = health_before.get("workers", {})
        ready_count = workers.get("ready", 0)
        unhealthy = workers.get("unhealthy", 0)
        if ready_count > 0 and unhealthy == 0:
            ready = True
            print(f"    [{i * POLL_INTERVAL_S}] ready={ready_count} unhealthy={unhealthy}")
            break
        if i % 4 == 0:
            print(f"    [{i * POLL_INTERVAL_S}] ready={ready_count} unhealthy={unhealthy}")
        time.sleep(POLL_INTERVAL_S)

    if not ready:
        print("ERROR: worker did not become ready in time")
        safe_scale_to_zero(endpoint_id, update_endpoint_workers)
        retry_delete_endpoint(endpoint_id, delete_endpoint)
        return 1

    (receipt_dir / "health-before.json").write_text(
        json.dumps(_redact(health_before), indent=2), encoding="utf-8"
    )

    # --- 3. Set up the gateway with DB sinks + HttpRunPodClient ---
    engine = _make_engine()
    training_client = HttpRunPodClient(
        api_key=runpod_api_key,
        endpoint_id=endpoint_id,
        cost_per_dispatch_cents=0,  # canary — no budget tracking
    )
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=callback_secret,
        base_dir=Path(receipt_dir) / "qf",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        budget_guard=BudgetGuard(
            base_dir=Path(receipt_dir) / "qf" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )

    # --- 4. Dispatch the training job via the gateway ---
    job_id = f"qf:cbproof:{sha[:8]}:{int(time.time())}"
    print(f"\n  Dispatching job: {job_id}")
    create_receipt = gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id, sha),
    )
    print(f"  Create receipt: {json.dumps(_redact(create_receipt), indent=2)}")

    if not create_receipt.get("enabled"):
        print("ERROR: gateway did not enable the job")
        safe_scale_to_zero(endpoint_id, update_endpoint_workers)
        retry_delete_endpoint(endpoint_id, delete_endpoint)
        return 1

    # Verify the dispatch created a training_jobs row.
    with Session(engine) as session:
        job_row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        if job_row is None:
            print("ERROR: training_jobs row not created on dispatch")
            return 1
        print(f"  training_jobs row: status={job_row.status}")

    # --- 5. Poll RunPod via the gateway until the job completes ---
    print(f"\n  Polling RunPod (timeout={POLL_TIMEOUT_S}s)...")
    poll_start = time.monotonic()
    final_receipt: dict[str, Any] | None = None
    while time.monotonic() - poll_start < POLL_TIMEOUT_S:
        receipts = gateway.poll_runpod_results()
        for r in receipts:
            status_val = r.get("status", "")
            result_val = r.get("result", "")
            ok = r.get("ok", False)
            print(
                f"    poll: job={r.get('job_id')} ok={ok} status={status_val} result={result_val}"
            )
            if r.get("job_id") == job_id and ok and result_val == "processed":
                final_receipt = r
                break
            if r.get("job_id") == job_id and not ok:
                final_receipt = r
                break
        if final_receipt is not None:
            break
        time.sleep(POLL_INTERVAL_S)

    if final_receipt is None:
        print("ERROR: job did not complete in time")
        safe_scale_to_zero(endpoint_id, update_endpoint_workers)
        retry_delete_endpoint(endpoint_id, delete_endpoint)
        return 1

    print(f"\n  Final receipt: {json.dumps(_redact(final_receipt), indent=2)}")

    # Save the poll receipt.
    (receipt_dir / "poll-receipt.json").write_text(
        json.dumps(_redact(final_receipt), indent=2), encoding="utf-8"
    )

    # --- 6. Verify DB rows ---
    print("\n  Verifying DB rows...")
    verification: dict[str, Any] = {}
    errors: list[str] = []

    with Session(engine) as session:
        # callback_receipts
        receipt_rows = session.scalars(
            select(CallbackReceiptRow).where(CallbackReceiptRow.job_id == job_id)
        ).all()
        verification["callback_receipts_count"] = len(receipt_rows)
        if len(receipt_rows) < 1:
            errors.append("callback_receipts row not created")
        else:
            verification["callback_receipt_id"] = receipt_rows[0].callback_id
            verification["callback_status"] = receipt_rows[0].status
            print(
                f"    callback_receipts: {len(receipt_rows)} row(s), status={receipt_rows[0].status}"
            )

        # model_dossiers
        dossier_rows = session.scalars(select(ModelDossierRow)).all()
        verification["model_dossiers_count"] = len(dossier_rows)
        if len(dossier_rows) < 1:
            errors.append("model_dossiers row not created")
        else:
            verification["dossier_model_id"] = dossier_rows[0].model_id
            verification["dossier_status"] = dossier_rows[0].status
            verification["dossier_content_hash"] = dossier_rows[0].content_hash
            print(
                f"    model_dossiers: {len(dossier_rows)} row(s), model_id={dossier_rows[0].model_id}"
            )

        # artifact_manifests
        artifact_rows = session.scalars(select(ArtifactManifestRow)).all()
        verification["artifact_manifests_count"] = len(artifact_rows)
        if len(artifact_rows) < 1:
            errors.append("artifact_manifests row not created")
        else:
            verification["artifact_id"] = artifact_rows[0].artifact_id
            verification["artifact_sha256"] = artifact_rows[0].sha256
            print(
                f"    artifact_manifests: {len(artifact_rows)} row(s), sha256={artifact_rows[0].sha256[:16]}..."
            )

        # training_jobs
        job_row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        if job_row is not None:
            verification["training_jobs_status"] = job_row.status
            verification["training_jobs_callback_receipt_id"] = job_row.callback_receipt_id
            print(
                f"    training_jobs: status={job_row.status}, callback_receipt_id={job_row.callback_receipt_id}"
            )
            if job_row.status != "completed":
                errors.append(f"training_jobs.status is {job_row.status}, expected 'completed'")
            if job_row.callback_receipt_id is None:
                errors.append("training_jobs.callback_receipt_id is None")
        else:
            errors.append("training_jobs row not found")

    # --- 7. Register a model_version from the dossier ---
    if not errors and verification.get("dossier_content_hash"):
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=0),
        )
        model_id = verification["dossier_model_id"]
        version_id = f"version:cbproof:{sha[:8]}:001"

        registry.register_model(
            model_id=model_id,
            name="Callback Proof LightGBM v1",
            model_family="lightgbm",
            description="Model registered from the live callback ingestion proof",
        )
        version_result = registry.register_version(
            model_id=model_id,
            version_id=version_id,
            dossier_content_hash=verification["dossier_content_hash"],
            artifact_id=verification["artifact_id"],
            callback_receipt_id=verification["callback_receipt_id"],
            version_number=1,
        )
        verification["model_version_id"] = version_id
        verification["model_version_status"] = version_result.get("status")
        print(f"    model_versions: version_id={version_id}, status={version_result.get('status')}")

        with Session(engine) as session:
            version_db_row = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
            ).one()
            verification["model_version_dossier_hash"] = version_db_row.dossier_content_hash
            verification["model_version_artifact_id"] = version_db_row.artifact_id

    # --- 8. Write verification result ---
    verification["errors"] = errors
    verification["passed"] = len(errors) == 0
    (receipt_dir / "verification.json").write_text(
        json.dumps(verification, indent=2, default=str), encoding="utf-8"
    )

    # --- Cleanup ---
    print("\n  Cleaning up...")
    safe_scale_to_zero(endpoint_id, update_endpoint_workers)
    retry_delete_endpoint(endpoint_id, delete_endpoint)
    from run_train_model import delete_template

    delete_template(template_name)
    print("  Endpoint + template deleted.")

    (receipt_dir / "cleanup.json").write_text(
        json.dumps({"endpoint_deleted": True, "template_deleted": True}, indent=2),
        encoding="utf-8",
    )

    # --- Final verdict ---
    print()
    if errors:
        print("FAIL: callback ingestion proof failed:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("PASS: callback ingestion proof succeeded.")
    print("  - Signed callback from live RunPod worker ingested into DB")
    print("  - model_dossiers row created")
    print("  - callback_receipts row created")
    print("  - artifact_manifests row created")
    print("  - training_jobs row: status=completed, callback_receipt_id linked")
    print("  - model_versions row registered from dossier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
