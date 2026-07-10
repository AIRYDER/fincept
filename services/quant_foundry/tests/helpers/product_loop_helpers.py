"""Shared helpers for the product-loop / auto-promotion test suite.

This module is a non-test helper (its name does not start with ``test_``)
so it is importable by every test module under pytest's ``importlib``
import mode. Test modules re-export these helpers back into their own
namespace where needed so existing internal references keep working.
"""

from __future__ import annotations

import time
from typing import Any

from quant_foundry.budget import BudgetGuard
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import MockRunPodClient
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
)
from quant_foundry.signatures import sign_callback
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

# ---------------------------------------------------------------------------
# Engine fixture — all callback + observability + registry tables
# ---------------------------------------------------------------------------


def _make_engine():
    """In-memory SQLite engine with every table the product loop touches.

    Creates the 6 callback ingestion tables, the 4 observability tables, and
    the 6 registry tables. FK enforcement is enabled via a connect pragma so
    the cross-table FKs (training_jobs.callback_receipt_id ->
    callback_receipts.callback_id; model_versions -> models / model_dossiers /
    artifact_manifests / callback_receipts) are honored.
    """
    eng = create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        # Callback ingestion tables (FK parents for registry versions).
        ArtifactManifestRow.__table__,
        ModelDossierRow.__table__,
        CallbackReceiptRow.__table__,
        CallbackDlqRow.__table__,
        CallbackMetricRow.__table__,
        # Observability tables (CostTracker writes here).
        TrainingJobRow.__table__,
        JobCostEventRow.__table__,
        JobMetricRow.__table__,
        CostSummaryRow.__table__,
        # Registry tables (ModelRegistryDB writes here).
        ModelRow.__table__,
        ModelVersionRow.__table__,
        ModelMetricRow.__table__,
        PromotionRow.__table__,
        PromotionDecisionRow.__table__,
        ShadowEvaluationRow.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    return eng


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


_MODEL_ID = "model:qf:train:e2e:1"
_ARTIFACT_ID = "artifact:e2e:1"


def _training_payload(job_id: str) -> dict[str, Any]:
    """A training request payload (mirrors the worker contract)."""
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "dataset:training:e2e",
        "model_family": "gbm",
        "search_space": {"n_estimators": [64]},
        "random_seed": 7,
        "hardware_class": "runpod-gpu",
        "extra_constraints": {},
        "gpu_type": "RTX_4090",
        "gpu_count": 1,
        "execution_timeout_ms": 1_860_000,
        "container_image": "ghcr.io/fincept/quant-foundry-worker:latest",
    }


def _signed_callback_with_artifact(
    job_id: str,
    *,
    secret: str,
    artifact_id: str,
    sha256: str,
) -> tuple[bytes, str, int]:
    """Build a signed callback with a custom artifact_id + sha256.

    This allows creating multiple versions under the same model
    (the default _signed_training_callback uses a fixed artifact hash
    which causes deduplication).
    """
    artifact = ArtifactManifest(
        artifact_id=artifact_id,
        sha256=sha256,
        size_bytes=2048,
        uri=None,
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="feature-hash-e2e",
        label_schema_hash="label-hash-e2e",
        code_git_sha="git-sha-e2e",
        lockfile_hash="lock-hash-e2e",
        container_image_digest="sha256:container-digest-e2e",
    )
    dossier = ModelDossier(
        model_id=_MODEL_ID,
        artifact_manifest_id=artifact.artifact_id,
        dataset_manifest_id="dataset:training:e2e",
        code_git_sha="git-sha-e2e",
        lockfile_hash="lock-hash-e2e",
        container_image_digest="sha256:container-digest-e2e",
        random_seed=7,
        hardware_class="runpod-gpu",
        training_metrics={"accuracy": 0.62, "logloss": 0.49},
        pbo=0.12,
        deflated_sharpe=1.1,
        authority=Authority.SHADOW_ONLY,
        metadata={"model_family": "gbm"},
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-training-e2e",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier.model_dump(mode="json"),
            "artifact_manifest": artifact.model_dump(mode="json"),
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    signature = sign_callback(payload, secret=secret, ts=ts, job_id=job_id)
    return payload, signature, ts


# ---------------------------------------------------------------------------
# Dispatch + gateway helpers (from test_auto_promotion)
# ---------------------------------------------------------------------------


def _dispatch_and_callback(
    gateway: QuantFoundryGateway,
    engine: Any,
    secret: str,
    job_id: str,
    model_id: str = _MODEL_ID,
    artifact_id: str = _ARTIFACT_ID,
    sha256: str = "a" * 64,
) -> str:
    """Dispatch a training job, receive the callback, return version_id.

    Pass a unique ``artifact_id`` + ``sha256`` to create distinct
    versions under the same model.
    """
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    payload, signature, ts = _signed_callback_with_artifact(
        job_id,
        secret=secret,
        artifact_id=artifact_id,
        sha256=sha256,
    )
    gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=signature,
        ts=ts,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        version_row = session.scalars(
            select(ModelVersionRow).where(
                ModelVersionRow.model_id == model_id,
                ModelVersionRow.artifact_id == artifact_id,
            )
        ).first()
        assert version_row is not None, f"no version row for artifact {artifact_id}"
        return version_row.version_id


def _make_gateway(
    engine: Any,
    secret: str,
    registry: ModelRegistryDB,
    tmp_path: Any,
) -> QuantFoundryGateway:
    """Create a gateway with DB sinks + CostTracker."""
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    return QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-auto",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-auto" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )


# ---------------------------------------------------------------------------
# Settlement helpers (from test_settlement_provider)
# ---------------------------------------------------------------------------


def _make_settlement_record(
    *,
    prediction_id: str,
    model_id: str = _MODEL_ID,
    realized_return_net: float | None = 0.001,
    brier: float | None = 0.21,
    status: SettlementStatus = SettlementStatus.SETTLED,
    ts_event: int | None = None,
) -> SettlementRecord:
    """Create a synthetic SettlementRecord for testing."""
    ts = ts_event or time.time_ns()
    is_settled = status == SettlementStatus.SETTLED
    net = realized_return_net if is_settled else None
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="AAPL",
        ts_event=ts,
        horizon_ns=86_400_000_000_000,  # 1 day
        status=status,
        settled_at_ns=ts + 86_400_000_000_000 if is_settled else None,
        realized_return_gross=(net + 0.0001) if net is not None else None,
        realized_return_net=net,
        abnormal_return=(net * 0.9) if net is not None else None,
        brier=brier if is_settled else None,
        calibration_bucket="bucket_0.5_0.6" if is_settled else None,
        cost_model_version="cm-v1",
        decision_window_start=ts,
        decision_window_end=ts + 86_400_000_000_000,
    )


class _FakeSettlementLedger:
    """In-memory settlement ledger for testing.

    Implements the read_all() method that SettledComparisonInputProvider
    needs. Does not write to disk.
    """

    def __init__(self, records: list[SettlementRecord] | None = None) -> None:
        self._records = records or []

    def read_all(self) -> list[SettlementRecord]:
        return list(self._records)

    def add(self, record: SettlementRecord) -> None:
        self._records.append(record)
