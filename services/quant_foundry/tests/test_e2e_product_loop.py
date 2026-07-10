"""End-to-end product loop proof: dispatch -> callback -> model_versions row.

This is the Phase A integration proof. A single test exercises the full product
loop against an in-memory SQLite engine (no external Postgres required):

  1. Create a training job in the gateway.
  2. Dispatch it via a mock RunPod client (no live API calls).
  3. Receive a signed callback carrying a valid ``training_result`` payload
     (a ``RunPodCallbackEnvelope`` with a ``ModelDossier`` +
     ``ArtifactManifest``).
  4. Verify the callback creates a ``model_dossiers`` row (via the DB sink).
  5. Verify the callback creates a ``callback_receipts`` row.
  6. Verify a ``training_jobs`` row exists with ``status=completed`` and
     ``callback_receipt_id`` set (via ``CostTracker``).
  7. Verify a ``model_versions`` row can be registered from the dossier
     (via ``ModelRegistryDB``).

The test is self-contained: it creates its own SQLite engine with all
callback + observability + registry tables, constructs the gateway with
``sink_backend="db"`` + ``CostTracker``, and constructs a
``ModelRegistryDB`` against the same engine. It uses the existing
``MockRunPodClient`` and ``sign_callback`` helpers.
"""

from __future__ import annotations

import time
from typing import Any

from quant_foundry.budget import BudgetGuard
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.dataset_manifest import DatasetRegistry
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus
from quant_foundry.promotion import PromotionGate
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


def _signed_training_callback(job_id: str, *, secret: str) -> tuple[bytes, str, int]:
    """Build a signed ``training_complete`` callback envelope.

    Returns ``(payload_bytes, signature, ts)`` ready for
    ``gateway.receive_callback(...)``.
    """
    artifact = ArtifactManifest(
        artifact_id=_ARTIFACT_ID,
        sha256="a" * 64,
        size_bytes=2048,
        uri="file:///durable/artifact.zip",
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
# The end-to-end product loop proof
# ---------------------------------------------------------------------------


def test_e2e_product_loop_dispatch_to_model_versions(tmp_path) -> None:
    """Prove the full product loop: dispatch -> signed callback -> model_versions.

    All DB operations use a single in-memory SQLite engine shared by the
    gateway (DB sinks + CostTracker) and the model registry. No external
    Postgres and no live RunPod API calls are required.
    """
    engine = _make_engine()
    secret = "e2e-product-loop-secret"

    # --- Registry: constructed before gateway, passed as registry param -------
    # Tier 1.2: when a registry is wired into the gateway, successful
    # training_complete callbacks auto-register a model version.
    registry = ModelRegistryDB(
        engine=engine,
        gate=PromotionGate(min_settled_count=10),
    )

    # --- Gateway: DB sinks + CostTracker, mock RunPod dispatch ---------------
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        # Allow the 25c mock dispatch (default monthly cap is 0 = kill switch).
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )

    job_id = "qf:train:e2e:1"

    # 1) Create + 2) dispatch the job via the mock RunPod client.
    create_receipt = gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    assert create_receipt["enabled"] is True, "gateway must be enabled"
    assert create_receipt["job_id"] == job_id
    # The mock client must have recorded exactly one dispatch.
    assert training_client._dispatch_count == 1, "job must be dispatched via MockRunPodClient"

    # The dispatch must have created a training_jobs row (via CostTracker).
    with Session(engine) as session:
        job_row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        assert job_row is not None, "training_jobs row should be created on dispatch"
        assert job_row.status == "dispatched"
        assert job_row.model_family == "gbm"
        assert job_row.callback_receipt_id is None

    # 3) Receive a signed callback with a valid training_result payload.
    payload, signature, ts = _signed_training_callback(job_id, secret=secret)
    cb_receipt = gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=signature,
        ts=ts,
        worker_id="runpod-training-e2e",
    )
    assert cb_receipt["ok"] is True, f"callback must be accepted: {cb_receipt}"
    assert cb_receipt["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

    # 4) Verify the callback created a model_dossiers row (via DB sink).
    # 5) Verify the callback created a callback_receipts row.
    # 6) Verify the training_jobs row is status=completed + callback_receipt_id set.
    in_rec = gateway.inbox.get_by_job_id(job_id)
    assert in_rec is not None, "inbox record must exist after callback"
    callback_receipt_id = in_rec.callback_id

    with Session(engine) as session:
        # 4) model_dossiers row.
        dossier_rows = session.scalars(
            select(ModelDossierRow).where(ModelDossierRow.model_id == _MODEL_ID)
        ).all()
        assert len(dossier_rows) == 1, "exactly one model_dossiers row must be created"
        dossier_row = dossier_rows[0]
        assert dossier_row.artifact_manifest_id == _ARTIFACT_ID
        assert dossier_row.status == "candidate"
        assert dossier_row.content_hash is not None
        dossier_content_hash = dossier_row.content_hash

        # The artifact_manifests row must also exist (written by the DB sink).
        artifact_rows = session.scalars(
            select(ArtifactManifestRow).where(ArtifactManifestRow.artifact_id == _ARTIFACT_ID)
        ).all()
        assert len(artifact_rows) == 1, "artifact_manifests row must be created"
        assert artifact_rows[0].sha256 == "a" * 64

        # 5) callback_receipts row.
        receipt_rows = session.scalars(
            select(CallbackReceiptRow).where(CallbackReceiptRow.job_id == job_id)
        ).all()
        assert len(receipt_rows) >= 1, "callback_receipts row must be created"
        assert any(r.callback_id == callback_receipt_id for r in receipt_rows), (
            "the callback_receipts row must match the inbox callback_id"
        )

        # 6) training_jobs row: status=completed + callback_receipt_id linked.
        job_row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        assert job_row is not None, "training_jobs row must still exist"
        assert job_row.status == "completed", (
            f"training_jobs.status must be 'completed', got {job_row.status!r}"
        )
        assert job_row.completed_at_ns is not None, "completed_at_ns must be set"
        assert job_row.callback_receipt_id is not None, (
            "callback_receipt_id must be linked by CostTracker"
        )
        assert job_row.callback_receipt_id == callback_receipt_id, (
            "training_jobs.callback_receipt_id must equal the inbox callback_id"
        )

    # 7) Verify the model version was AUTO-registered by the gateway
    #    (Tier 1.2: no manual register_version call needed — the gateway
    #    auto-registers when a registry is wired in).
    expected_version_id = f"version:{_MODEL_ID}:{dossier_content_hash[:16]}"

    # Confirm the rows are durable in the DB.
    with Session(engine) as session:
        model_db_row = session.scalars(
            select(ModelRow).where(ModelRow.model_id == _MODEL_ID)
        ).first()
        assert model_db_row is not None, "models row must be auto-registered by the gateway"
        assert model_db_row.model_id == _MODEL_ID
        assert model_db_row.current_status == "candidate"

        version_db_row = session.scalars(
            select(ModelVersionRow).where(ModelVersionRow.version_id == expected_version_id)
        ).first()
        assert version_db_row is not None, (
            "model_versions row must be auto-registered by the gateway"
        )
        assert version_db_row.model_id == _MODEL_ID
        assert version_db_row.dossier_content_hash == dossier_content_hash
        assert version_db_row.artifact_id == _ARTIFACT_ID
        assert version_db_row.callback_receipt_id == callback_receipt_id
        assert version_db_row.status == "candidate"
        assert version_db_row.version_number == 1

    # Cross-check the registry read API sees the registered version.
    listed_versions = registry.list_versions(_MODEL_ID)
    assert len(listed_versions) == 1
    assert listed_versions[0]["version_id"] == expected_version_id

    engine.dispose()


# ---------------------------------------------------------------------------
# Tier 1.5: Dataset registry dispatch gate
# ---------------------------------------------------------------------------


def test_dataset_registry_rejects_unregistered_production_dispatch(tmp_path) -> None:
    """Production training with an unregistered dataset is rejected at dispatch.

    The gateway calls DatasetRegistry.dispatch_training(dataset_id, mode="production")
    before enqueueing a production training job. An unregistered dataset id
    is rejected with error_code="dataset_dispatch_rejected". Canary mode
    with the same dataset id is allowed (permissive).
    """
    engine = _make_engine()
    secret = "dataset-gate-secret"
    dataset_registry = DatasetRegistry()  # in-memory, empty

    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-gate",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        dataset_registry=dataset_registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-gate" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )

    dataset_id = "dataset:unregistered:production:test"
    payload = {
        "schema_version": 1,
        "job_id": "qf:gate:prod:1",
        "dataset_manifest_ref": dataset_id,
        "model_family": "gbm",
        "search_space": {"n_estimators": [64]},
        "random_seed": 7,
        "extra_constraints": {"training_mode": "production"},
    }

    # 1) Production dispatch with unregistered dataset → rejected.
    receipt = gateway.create_job(
        job_id="qf:gate:prod:1",
        job_type="training",
        idempotency_key="idem-gate-prod-1",
        request_payload=payload,
    )
    assert receipt["ok"] is False, "production dispatch with unregistered dataset must be rejected"
    assert receipt["error_code"] == "dataset_dispatch_rejected"
    assert dataset_id in receipt["detail"], "error detail must mention the dataset id"
    # The job must NOT be in the outbox (rejected before enqueue).
    ob_rec = gateway.outbox.get("qf:gate:prod:1")
    assert ob_rec is None, "rejected job must not be enqueued"

    # 2) Canary dispatch with the same unregistered dataset → allowed.
    canary_payload = dict(payload)
    canary_payload["job_id"] = "qf:gate:canary:1"
    canary_payload["extra_constraints"] = {"training_mode": "canary"}
    canary_receipt = gateway.create_job(
        job_id="qf:gate:canary:1",
        job_type="training",
        idempotency_key="idem-gate-canary-1",
        request_payload=canary_payload,
    )
    assert canary_receipt.get("ok") is not False, (
        "canary dispatch with unregistered dataset must be allowed"
    )
    assert canary_receipt["enabled"] is True
    # The canary job must be in the outbox (enqueued + dispatched).
    ob_rec = gateway.outbox.get("qf:gate:canary:1")
    assert ob_rec is not None, "canary job must be enqueued"

    engine.dispose()


# ---------------------------------------------------------------------------
# Tier 2a: Full product loop — dispatch → callback → model_versions →
#          metrics → promotion gate → promotion receipt → shadow evaluation
# ---------------------------------------------------------------------------


def test_e2e_full_product_loop_through_promotion(tmp_path) -> None:
    """Prove the FULL product loop: dispatch → callback → model_versions →
    tournament metrics → sentinel metrics → promotion gate → promotion receipt
    → shadow evaluation row.

    Extends ``test_e2e_product_loop_dispatch_to_model_versions`` with:
      8. Record tournament metrics (settled_count >= gate minimum).
      9. Record sentinel metrics (passed=True).
     10. Run the promotion gate via ``registry.promote()``.
     11. Verify the promotion receipt is APPROVED.
     12. Verify ``promotions`` + ``promotion_decisions`` rows are persisted.
     13. Verify ``model_versions.status`` is updated to the target level.
     14. Verify ``models.current_status`` is updated.
     15. Record a shadow evaluation row.
     16. Verify the ``shadow_evaluations`` row is persisted.

    All DB operations use a single in-memory SQLite engine. No external
    Postgres and no live RunPod API calls are required.
    """
    from quant_foundry.dossier import DossierStatus
    from quant_foundry.promotion import (
        PromotionReceipt,
        ReviewDecision,
    )

    engine = _make_engine()
    secret = "e2e-full-loop-secret"

    # --- Registry + Gateway (same setup as Phase A test) -------------------
    registry = ModelRegistryDB(
        engine=engine,
        gate=PromotionGate(min_settled_count=10),
    )
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-full",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-full" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )

    job_id = "qf:train:full:1"

    # 1-2) Create + dispatch the job.
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )

    # 3) Receive signed callback.
    payload, signature, ts = _signed_training_callback(job_id, secret=secret)
    cb_receipt = gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=signature,
        ts=ts,
        worker_id="runpod-training-full",
    )
    assert cb_receipt["ok"] is True

    # 4-7) model_versions auto-registered (same as Phase A).
    in_rec = gateway.inbox.get_by_job_id(job_id)
    assert in_rec is not None
    callback_receipt_id = in_rec.callback_id  # noqa: F841

    with Session(engine) as session:
        dossier_row = session.scalars(
            select(ModelDossierRow).where(ModelDossierRow.model_id == _MODEL_ID)
        ).first()
        assert dossier_row is not None
        dossier_content_hash = dossier_row.content_hash  # noqa: F841

        version_row = session.scalars(
            select(ModelVersionRow).where(ModelVersionRow.model_id == _MODEL_ID)
        ).first()
        assert version_row is not None
        version_id = version_row.version_id
        assert version_row.status == "candidate"

    # 8) Record tournament metrics — settled_count=50 (>= gate minimum of 10).
    tournament_metrics = {
        "model_id": _MODEL_ID,
        "total_score": 0.72,
        "score_components": [],
        "p_value": 0.03,
        "deflated_sharpe": 1.4,
        "raw_sharpe": 1.8,
        "blocking_issues": [],
        "recommendation": "promote",
        "status": "eligible",
        "trial_count": 1,
        "cost_model_version": "cm-v1",
        "settled_count": 50,
    }
    registry.record_metrics(
        version_id=version_id,
        metric_type="tournament",
        metrics_dict=tournament_metrics,
    )

    # 9) Record sentinel metrics — passed=True, no issues.
    sentinel_metrics = {
        "model_id": _MODEL_ID,
        "issues": [],
        "passed": True,
        "checks_run": ["leakage", "overfit", "stability"],
        "ts_ns": time.time_ns(),
        "pbo": 0.12,
        "pbo_flagged": False,
    }
    registry.record_metrics(
        version_id=version_id,
        metric_type="sentinel",
        metrics_dict=sentinel_metrics,
    )

    # 9b) C7 evidence chain metrics.
    registry.record_metrics(
        version_id=version_id,
        metric_type="selfcheck",
        metrics_dict={
            "passed": True,
            "n_rows_scored": 10,
            "bundle_sha256": "a" * 64,
        },
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="pit_evidence",
        metrics_dict={"verified": True, "evidence_sha256": "e" * 64},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="feature_set",
        metrics_dict={"feature_set_version": "fs-v1"},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="backend",
        metrics_dict={"production_eligible": True},
    )

    # 10) Run the promotion gate: candidate → research_approved.
    promotion_receipt = registry.promote(
        version_id=version_id,
        target_status=DossierStatus.RESEARCH_APPROVED,
        review_note="E2E full loop proof — promotion to research_approved",
        decided_by="e2e-proof-script",
    )

    # 11) Verify the promotion receipt is APPROVED.
    assert isinstance(promotion_receipt, PromotionReceipt)
    assert promotion_receipt.decision == ReviewDecision.APPROVED, (
        f"promotion should be approved, got {promotion_receipt.decision} "
        f"reason={promotion_receipt.rejection_reason}"
    )
    assert promotion_receipt.rejection_reason is None
    assert promotion_receipt.review_note == ("E2E full loop proof — promotion to research_approved")

    # 12) Verify promotions + promotion_decisions rows are persisted.
    with Session(engine) as session:
        promo_rows = session.scalars(
            select(PromotionRow).where(PromotionRow.version_id == version_id)
        ).all()
        assert len(promo_rows) == 1, "exactly one promotions row"
        promo = promo_rows[0]
        assert promo.decision == "approved"
        assert promo.from_status == "candidate"
        assert promo.to_status == "research_approved"
        assert promo.decided_at_ns is not None

        decision_rows = session.scalars(
            select(PromotionDecisionRow).where(
                PromotionDecisionRow.promotion_id == promo.promotion_id
            )
        ).all()
        assert len(decision_rows) == 1, "exactly one promotion_decisions row"
        decision = decision_rows[0]
        assert decision.decision == "approved"
        assert decision.rejection_reason is None
        assert decision.decided_by == "e2e-proof-script"
        assert decision.review_note == ("E2E full loop proof — promotion to research_approved")

    # 13) Verify model_versions.status is updated to research_approved.
    with Session(engine) as session:
        updated_version = session.scalars(
            select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
        ).first()
        assert updated_version.status == "research_approved", (
            f"version status should be 'research_approved', got {updated_version.status!r}"
        )
        assert updated_version.promoted_at_ns is not None

    # 14) Verify models.current_status is updated.
    with Session(engine) as session:
        updated_model = session.scalars(
            select(ModelRow).where(ModelRow.model_id == _MODEL_ID)
        ).first()
        assert updated_model.current_status == "research_approved"
        assert updated_model.current_version_id == version_id

    # 15) Record a shadow evaluation row.
    shadow_eval_metrics = {
        "champion_version_id": None,
        "challenger_version_id": version_id,
        "net_edge_delta_bps": 12.5,
        "bootstrap_p_value": 0.04,
        "dsr_delta": 0.3,
        "brier_delta": -0.02,
        "decision": "promote",
    }
    evaluation_id = registry.record_shadow_evaluation(
        version_id=version_id,
        settled_count=50,
        evaluation_metrics=shadow_eval_metrics,
    )

    # 16) Verify the shadow_evaluations row is persisted.
    with Session(engine) as session:
        eval_rows = session.scalars(
            select(ShadowEvaluationRow).where(ShadowEvaluationRow.evaluation_id == evaluation_id)
        ).all()
        assert len(eval_rows) == 1, "exactly one shadow_evaluations row"
        eval_row = eval_rows[0]
        assert eval_row.version_id == version_id
        assert eval_row.settled_count == 50
        assert eval_row.evaluation_metrics["decision"] == "promote"

    # 17) Verify model_metrics rows: tournament + sentinel + C7 chain = 6 total.
    with Session(engine) as session:
        metric_rows = session.scalars(
            select(ModelMetricRow).where(ModelMetricRow.version_id == version_id)
        ).all()
        assert len(metric_rows) == 6, "should have 6 metric rows (tournament + sentinel + 4 C7)"
        metric_types = {r.metric_type for r in metric_rows}
        assert metric_types == {
            "tournament",
            "sentinel",
            "selfcheck",
            "pit_evidence",
            "feature_set",
            "backend",
        }

    # 18) Summary: every table in the product loop has at least one row.
    with Session(engine) as session:
        assert session.scalars(select(TrainingJobRow)).first() is not None
        assert session.scalars(select(CallbackReceiptRow)).first() is not None
        assert session.scalars(select(ModelDossierRow)).first() is not None
        assert session.scalars(select(ArtifactManifestRow)).first() is not None
        assert session.scalars(select(ModelRow)).first() is not None
        assert session.scalars(select(ModelVersionRow)).first() is not None
        assert session.scalars(select(ModelMetricRow)).first() is not None
        assert session.scalars(select(PromotionRow)).first() is not None
        assert session.scalars(select(PromotionDecisionRow)).first() is not None
        assert session.scalars(select(ShadowEvaluationRow)).first() is not None

    engine.dispose()


def test_e2e_promotion_gate_rejects_insufficient_evidence(tmp_path) -> None:
    """Prove the promotion gate fails closed when evidence is insufficient.

    Dispatch → callback → model_versions → promote WITHOUT tournament or
    sentinel metrics. The gate must reject with INSUFFICIENT_EVIDENCE
    (settled_count=0 < min_settled_count=10). The rejection receipt must
    be persisted, and the model version status must NOT change.
    """
    from quant_foundry.dossier import DossierStatus
    from quant_foundry.promotion import (
        PromotionRejectionReason,
        ReviewDecision,
    )

    engine = _make_engine()
    secret = "e2e-reject-secret"

    registry = ModelRegistryDB(
        engine=engine,
        gate=PromotionGate(min_settled_count=10),
    )
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-reject",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-reject" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )

    job_id = "qf:train:reject:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    payload, signature, ts = _signed_training_callback(job_id, secret=secret)
    cb_receipt = gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=signature,
        ts=ts,
        worker_id="runpod-training-reject",
    )
    assert cb_receipt["ok"] is True

    # Get the auto-registered version_id.
    with Session(engine) as session:
        version_row = session.scalars(
            select(ModelVersionRow).where(ModelVersionRow.model_id == _MODEL_ID)
        ).first()
        assert version_row is not None
        version_id = version_row.version_id
        assert version_row.status == "candidate"

    # Record C7 evidence chain metrics but NOT tournament metrics
    # (settled_count=0 < min_settled_count=10 → INSUFFICIENT_EVIDENCE).
    registry.record_metrics(
        version_id=version_id,
        metric_type="selfcheck",
        metrics_dict={"passed": True, "n_rows_scored": 10, "bundle_sha256": "a" * 64},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="pit_evidence",
        metrics_dict={"verified": True, "evidence_sha256": "e" * 64},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="feature_set",
        metrics_dict={"feature_set_version": "fs-v1"},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="backend",
        metrics_dict={"production_eligible": True},
    )

    # Attempt promotion WITHOUT tournament metrics (settled_count=0).
    rejection_receipt = registry.promote(
        version_id=version_id,
        target_status=DossierStatus.RESEARCH_APPROVED,
        review_note="Should be rejected — no evidence",
        decided_by="e2e-reject-test",
    )

    # The gate must reject with INSUFFICIENT_EVIDENCE.
    assert rejection_receipt.decision == ReviewDecision.REJECTED
    assert rejection_receipt.rejection_reason == (PromotionRejectionReason.INSUFFICIENT_EVIDENCE)

    # The rejection must be persisted in promotions + promotion_decisions.
    with Session(engine) as session:
        promo_rows = session.scalars(
            select(PromotionRow).where(PromotionRow.version_id == version_id)
        ).all()
        assert len(promo_rows) == 1
        assert promo_rows[0].decision == "rejected"

        decision_rows = session.scalars(
            select(PromotionDecisionRow).where(
                PromotionDecisionRow.promotion_id == promo_rows[0].promotion_id
            )
        ).all()
        assert len(decision_rows) == 1
        assert decision_rows[0].decision == "rejected"
        assert decision_rows[0].rejection_reason == "insufficient_evidence"

    # The version status must NOT have changed (still candidate).
    with Session(engine) as session:
        unchanged_version = session.scalars(
            select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
        ).first()
        assert unchanged_version.status == "candidate"
        assert unchanged_version.promoted_at_ns is None

    engine.dispose()
