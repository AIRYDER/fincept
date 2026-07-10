"""S3: Full Product Loop Proof — dispatch → GPU train → callback → DB → model_versions.

This script proves the complete product loop end-to-end:

  1. Create a SQLite engine with all callback + observability + registry tables.
  2. Construct a QuantFoundryGateway with HttpRunPodClient + DB sinks + CostTracker.
  3. Create a training job in the gateway (dispatches to RunPod via HttpRunPodClient).
  4. Poll RunPod for completion.
  5. Extract the signed callback from the RunPod response.
  6. Feed the callback to gateway.receive_callback() (within the 300s skew window).
  7. Verify model_dossiers, callback_receipts, artifact_manifests rows are created.
  8. Register a model_versions row via ModelRegistryDB.
  9. Verify the model_versions row is durable in the DB.

No external Postgres required — uses SQLite (same pattern as test_e2e_product_loop.py).
No API service required — calls the gateway directly (same code path as the API route).
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import tempfile
import time

# --- Path setup ---
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = str(_REPO_ROOT / "services" / "quant_foundry" / "src")
_DB_SRC = str(_REPO_ROOT / "libs" / "fincept-db" / "src")
_CORE_SRC = str(_REPO_ROOT / "libs" / "fincept-core" / "src")
for p in [_QF_SRC, _DB_SRC, _CORE_SRC]:
    if p not in sys.path:
        sys.path.insert(0, p)

# --- Env setup ---
os.environ.setdefault(
    "QUANT_FOUNDRY_CALLBACK_SECRET", os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
)
os.environ.setdefault("RUNPOD_API_KEY", os.environ.get("RUNPOD_API_KEY", ""))

CALLBACK_SECRET = os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"]
RUNPOD_API_KEY = os.environ["RUNPOD_API_KEY"]
ENDPOINT_ID = "rjxyaov775q7nd"

if not CALLBACK_SECRET:
    print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET is not set")
    sys.exit(1)
if not RUNPOD_API_KEY:
    print("ERROR: RUNPOD_API_KEY is not set")
    sys.exit(1)

# --- Imports ---
from quant_foundry.budget import BudgetGuard
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.promotion import PromotionGate
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import HttpRunPodClient
from quant_foundry.signatures import verify_callback
from sqlalchemy import create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from fincept_db.callback_tables import (
    ArtifactManifestRow,
    CallbackDlqRow,
    CallbackMetricRow,
    CallbackReceiptRow,
    ModelDossierRow,
)
from fincept_db.models import Base
from fincept_db.observability import (
    CostSummaryRow,
    JobCostEventRow,
    JobMetricRow,
    TrainingJobRow,
)
from fincept_db.registry_tables import (
    ModelMetricRow,
    ModelRow,
    ModelVersionRow,
    PromotionDecisionRow,
    PromotionRow,
    ShadowEvaluationRow,
)

# --- Constants ---
REPORT_DIR = _REPO_ROOT / "reports" / "s3-product-loop-proof"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 70)
print("S3: FULL PRODUCT LOOP PROOF")
print("dispatch → GPU train → callback → DB → model_versions")
print("=" * 70)
print()

# --- Step 1: Create SQLite engine with all tables ---
print("=== STEP 1: CREATE SQLITE ENGINE WITH ALL TABLES ===")


def _make_engine():
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


engine = _make_engine()
print(f"  SQLite engine created with {15} tables")
print()

# --- Step 2: Construct gateway with HttpRunPodClient + DB sinks ---
print("=== STEP 2: CONSTRUCT GATEWAY ===")

tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="s3-product-loop-"))
training_client = HttpRunPodClient(
    api_key=RUNPOD_API_KEY,
    endpoint_id=ENDPOINT_ID,
    cost_per_dispatch_cents=0,
)

gateway = QuantFoundryGateway(
    enabled=True,
    mode="runpod",
    shadow_only=True,
    callback_secret=CALLBACK_SECRET,
    base_dir=tmp_dir / "qf",
    runpod_clients={"training": training_client},
    cost_tracker=CostTracker(engine=engine),
    sink_backend="db",
    db_engine=engine,
    budget_guard=BudgetGuard(
        base_dir=tmp_dir / "qf" / "budget",
        monthly_budget_cents=1_000_000,
    ),
)
print(f"  Gateway constructed with HttpRunPodClient (endpoint={ENDPOINT_ID})")
print("  DB sinks: enabled (SQLite)")
print("  CostTracker: enabled")
print()

# --- Step 3: Prepare dataset and create job ---
print("=== STEP 3: CREATE TRAINING JOB ===")

random.seed(42)
rows = ["feature_1,feature_2,feature_3,label\n"]
for i in range(100):
    f1 = random.gauss(0, 1)
    f2 = random.gauss(0, 1)
    f3 = random.gauss(0, 1)
    label = 1 if (f1 + f2 + f3 + random.gauss(0, 0.5)) > 0 else 0
    rows.append(f"{f1:.6f},{f2:.6f},{f3:.6f},{label}\n")
inline_csv = "".join(rows)

job_id = f"s3-product-loop-{int(time.time())}"
request_payload = {
    "schema_version": 1,
    "job_id": job_id,
    "dataset_manifest_ref": "inline://placeholder",
    "model_family": "xgboost_gpu",
    "random_seed": 42,
    "search_space": {
        "max_depth": [6],
        "learning_rate": [0.1],
        "n_estimators": [50],
    },
    "extra_constraints": {
        "bar_seconds": "86400",
        "horizon_bars": "5",
        "purge_bars": "5",
        "training_mode": "canary",
        "column_roles_json": json.dumps(
            {
                "feature_columns": ["feature_1", "feature_2", "feature_3"],
                "label_columns": ["label"],
            }
        ),
        "task_spec_json": json.dumps(
            {
                "task_type": "binary",
                "label_column": "label",
                "horizon": 5,
                "calibration_policy": "none",
            }
        ),
    },
    "inline_dataset_csv": inline_csv,
}

print(f"  job_id:       {job_id}")
print("  model_family: xgboost_gpu")
print()

create_receipt = gateway.create_job(
    job_id=job_id,
    job_type="training",
    idempotency_key=f"idem-{job_id}",
    request_payload=request_payload,
)

if not create_receipt.get("enabled"):
    print(f"  FAILED: gateway not enabled — {create_receipt}")
    sys.exit(1)

if create_receipt.get("status") not in ("dispatched", "DISPATCHED", "Dispatched"):
    print(f"  Dispatch status: {create_receipt.get('status')}")
    # Check if it's a transient failure
    if create_receipt.get("error_code"):
        print(f"  error_code: {create_receipt.get('error_code')}")
        print(f"  error_summary: {create_receipt.get('error_summary', '')[:200]}")
        sys.exit(1)

runpod_job_id = create_receipt.get("runpod_job_id", "")
print(f"  Dispatched to RunPod: {runpod_job_id}")

# Verify training_jobs row was created
with Session(engine) as session:
    job_row = session.scalars(select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)).first()
    if job_row:
        print(f"  training_jobs row: status={job_row.status}, model_family={job_row.model_family}")
    else:
        print("  WARNING: no training_jobs row found")
print()

# --- Step 4: Poll RunPod for completion ---
print("=== STEP 4: POLL RUNPOD FOR COMPLETION ===")

start = time.time()
timeout = 300  # 5 minutes max
last_status = None
output = None

while time.time() - start < timeout:
    try:
        status_resp = training_client.check_status(runpod_job_id)
        status = status_resp.get("status", "UNKNOWN")
        if status != last_status:
            elapsed = time.time() - start
            print(f"  [{elapsed:.0f}s] status: {status}", flush=True)
            last_status = status

        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            output = status_resp.get("output", {})
            elapsed = time.time() - start
            print(f"\n  {status} in {elapsed:.1f}s", flush=True)
            break
    except Exception as e:
        elapsed = time.time() - start
        print(f"  [{elapsed:.0f}s] error: {e}", flush=True)
    time.sleep(3)
else:
    print("  TIMEOUT — job did not complete in 300s")
    sys.exit(1)

if not output or not isinstance(output, dict):
    print("  No output received")
    sys.exit(1)

# Check for training failure
if output.get("status") == "failed" or output.get("error_code"):
    print(f"  Training FAILED: {output.get('error_code')}: {output.get('error_summary', '')[:200]}")
    sys.exit(1)

print()

# --- Step 5: Extract signed callback ---
print("=== STEP 5: EXTRACT SIGNED CALLBACK ===")

callback_payload_str = output.get("callback_payload", "")
callback_signature = output.get("callback_signature", "")
callback_ts = int(output.get("callback_ts", 0))
worker_id = "runpod-worker-s3"

if not callback_payload_str or not callback_signature:
    print("  ERROR: missing callback fields in output")
    print(f"  Available keys: {sorted(output.keys())}")
    sys.exit(1)

print(f"  callback_payload: {len(callback_payload_str)} chars")
print(f"  callback_signature: {callback_signature[:32]}...")
print(f"  callback_ts: {callback_ts}")
print()

# --- Step 6: Feed callback to gateway ---
print("=== STEP 6: FEED CALLBACK TO GATEWAY ===")

payload_bytes = callback_payload_str.encode("utf-8")

# Verify HMAC first (for diagnostics)
sig_valid = verify_callback(
    payload_bytes,
    callback_signature,
    secret=CALLBACK_SECRET,
    ts=callback_ts,
    job_id=job_id,
)
print(f"  HMAC pre-check: {'VALID' if sig_valid else 'INVALID'}")
if not sig_valid:
    # Check if it's a skew issue
    now = int(time.time())
    skew = abs(now - callback_ts)
    print(f"  Skew: {skew}s (max 300s)")
    if skew > 300:
        print("  ERROR: skew exceeded — callback took too long to process")
        sys.exit(1)
    else:
        print("  ERROR: signature invalid for non-skew reason")
        sys.exit(1)

# Feed to gateway
cb_receipt = gateway.receive_callback(
    job_id=job_id,
    payload=payload_bytes,
    signature=callback_signature,
    ts=callback_ts,
    worker_id=worker_id,
)

print(f"  Gateway result: ok={cb_receipt.get('ok')}, result={cb_receipt.get('result')}")
if not cb_receipt.get("ok"):
    print(f"  ERROR: {cb_receipt.get('error_code')}: {cb_receipt.get('detail', '')[:200]}")
    sys.exit(1)

# Verify outbox status
ob_rec = gateway.outbox.get(job_id)
if ob_rec:
    print(f"  Outbox status: {ob_rec.status}")
print()

# --- Step 7: Verify DB rows ---
print("=== STEP 7: VERIFY DB ROWS ===")

# Parse callback to get artifact_id and model_id
envelope = json.loads(callback_payload_str)
payload = envelope.get("payload", envelope)
artifact_manifest = payload.get("artifact_manifest", {})
dossier = payload.get("dossier", {})
artifact_id = artifact_manifest.get("artifact_id", "")
model_id = dossier.get("model_id", "")
dossier_content_hash = None

with Session(engine) as session:
    # 7a: callback_receipts
    receipt_rows = session.scalars(
        select(CallbackReceiptRow).where(CallbackReceiptRow.job_id == job_id)
    ).all()
    print(f"  callback_receipts: {len(receipt_rows)} row(s)")
    if receipt_rows:
        r = receipt_rows[0]
        print(f"    callback_id: {r.callback_id}")
        print(f"    job_id: {r.job_id}")
        print(f"    signature_valid: {r.signature_valid}")

    # 7b: model_dossiers
    dossier_rows = (
        session.scalars(select(ModelDossierRow).where(ModelDossierRow.model_id == model_id)).all()
        if model_id
        else []
    )
    print(f"  model_dossiers: {len(dossier_rows)} row(s)")
    if dossier_rows:
        d = dossier_rows[0]
        print(f"    model_id: {d.model_id}")
        print(f"    artifact_manifest_id: {d.artifact_manifest_id}")
        print(f"    status: {d.status}")
        print(f"    content_hash: {d.content_hash}")
        dossier_content_hash = d.content_hash

    # 7c: artifact_manifests
    artifact_rows = (
        session.scalars(
            select(ArtifactManifestRow).where(ArtifactManifestRow.artifact_id == artifact_id)
        ).all()
        if artifact_id
        else []
    )
    print(f"  artifact_manifests: {len(artifact_rows)} row(s)")
    if artifact_rows:
        a = artifact_rows[0]
        print(f"    artifact_id: {a.artifact_id}")
        print(f"    sha256: {a.sha256}")
        print(f"    model_family: {a.model_family}")
        print(f"    size_bytes: {a.size_bytes}")

    # 7d: training_jobs (updated by CostTracker)
    job_row = session.scalars(select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)).first()
    if job_row:
        print(
            f"  training_jobs: status={job_row.status}, callback_receipt_id={job_row.callback_receipt_id}"
        )

print()

# --- Step 8: Register model_versions row ---
print("=== STEP 8: REGISTER MODEL VERSION ===")

if not model_id or not dossier_content_hash or not artifact_id:
    print("  ERROR: missing required fields for register_version")
    print(f"    model_id: {model_id}")
    print(f"    dossier_content_hash: {dossier_content_hash}")
    print(f"    artifact_id: {artifact_id}")
    sys.exit(1)

# Get callback_receipt_id
with Session(engine) as session:
    receipt_rows = session.scalars(
        select(CallbackReceiptRow).where(CallbackReceiptRow.job_id == job_id)
    ).all()
    callback_receipt_id = receipt_rows[0].callback_id if receipt_rows else None

registry = ModelRegistryDB(
    engine=engine,
    gate=PromotionGate(min_settled_count=10),
)

# Register the model
model_result = registry.register_model(
    model_id=model_id,
    name="S3 Product Loop xgboost_gpu v1",
    model_family="xgboost_gpu",
    description="Model registered from the S3 full product loop proof",
)
print(f"  register_model: {model_result is not None}")
if model_result:
    print(f"    model_id: {model_result['model_id']}")
    print(f"    current_status: {model_result['current_status']}")

# Register the version
version_id = f"version:s3:{int(time.time())}"
version_result = registry.register_version(
    model_id=model_id,
    version_id=version_id,
    dossier_content_hash=dossier_content_hash,
    artifact_id=artifact_id,
    callback_receipt_id=callback_receipt_id,
    version_number=1,
)
print(f"  register_version: {version_result is not None}")
if version_result:
    print(f"    version_id: {version_result['version_id']}")
    print(f"    model_id: {version_result['model_id']}")
    print(f"    status: {version_result['status']}")
    print(f"    version_number: {version_result['version_number']}")
print()

# --- Step 9: Verify model_versions row is durable ---
print("=== STEP 9: VERIFY model_versions ROW IS DURABLE ===")

with Session(engine) as session:
    version_db_row = session.scalars(
        select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
    ).first()
    if version_db_row:
        print("  model_versions row FOUND:")
        print(f"    version_id: {version_db_row.version_id}")
        print(f"    model_id: {version_db_row.model_id}")
        print(f"    dossier_content_hash: {version_db_row.dossier_content_hash}")
        print(f"    artifact_id: {version_db_row.artifact_id}")
        print(f"    callback_receipt_id: {version_db_row.callback_receipt_id}")
        print(f"    status: {version_db_row.status}")
        print(f"    version_number: {version_db_row.version_number}")
    else:
        print("  ERROR: model_versions row NOT FOUND")
        sys.exit(1)

    # Cross-check: list_versions
    listed = registry.list_versions(model_id)
    print(f"  list_versions: {len(listed)} version(s)")
    if listed:
        print(f"    [0] version_id={listed[0]['version_id']}, status={listed[0]['status']}")

print()

# --- Summary receipt ---
print("=" * 70)
print("SUMMARY RECEIPT")
print("=" * 70)

receipt = {
    "test": "S3 Full Product Loop Proof",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "job_id": job_id,
    "runpod_job_id": runpod_job_id,
    "model_family": "xgboost_gpu",
    "model_id": model_id,
    "artifact_id": artifact_id,
    "version_id": version_id,
    "dossier_content_hash": dossier_content_hash,
    "callback_receipt_id": callback_receipt_id,
    "hmac_valid": sig_valid,
    "callback_result": cb_receipt.get("result"),
    "db_rows": {
        "callback_receipts": len(receipt_rows),
        "model_dossiers": len(dossier_rows),
        "artifact_manifests": len(artifact_rows),
        "model_versions": 1 if version_db_row else 0,
    },
    "artifact": {
        "sha256": artifact_manifest.get("sha256"),
        "size_bytes": artifact_manifest.get("size_bytes"),
        "model_family": artifact_manifest.get("model_family"),
        "determinism_status": artifact_manifest.get("determinism_status"),
    },
    "verdict": "FULL_PRODUCT_LOOP_PROVEN" if version_db_row else "FAILED",
}

receipt_path = REPORT_DIR / "product_loop_receipt.json"
receipt_path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
print(json.dumps(receipt, indent=2, default=str))
print(f"\n  Receipt saved to: {receipt_path}")

engine.dispose()
print("\n  DONE.")
