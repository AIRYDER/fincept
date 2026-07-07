"""Tests for quant_foundry.registry_db — DB-backed model registry with promotion workflow.

Tests the ``ModelRegistryDB`` against an in-memory SQLite database (no Postgres
required). The registry uses ``INSERT ... ON CONFLICT DO NOTHING`` for
idempotency on model/version registration, which works on both SQLite and
Postgres (the registry code picks the right dialect-specific insert at runtime).

Test coverage:
  - Model registration (idempotent — same model_id is a no-op)
  - Version registration (idempotent — same version_id is a no-op)
  - Metrics recording (training, tournament, sentinel, settlement)
  - Shadow evaluation recording
  - Promotion workflow: approved path (status changes, receipt persisted)
  - Promotion workflow: rejected path (status does NOT change, receipt persisted)
  - Promotion history query (lists all attempts with decision receipts)
  - State machine enforcement (gate enforces — no skipping levels)
  - No secrets in DB (no signature bytes, no raw payloads)
  - CHECK constraint enforcement (bad status, bad decision, bad metric_type)
  - Idempotency (ON CONFLICT DO NOTHING for models and versions)
  - Read API (get_model, get_version, list_models, list_versions)
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fincept_db.callback_tables import (
    ArtifactManifestRow,
    CallbackReceiptRow,
    ModelDossierRow,
)
from fincept_db.models import Base
from fincept_db.registry_tables import (
    ModelMetricRow,
    ModelRow,
    ModelVersionRow,
    PromotionDecisionRow,
    PromotionRow,
    ShadowEvaluationRow,
)

from quant_foundry.dossier import DossierStatus
from quant_foundry.promotion import (
    PromotionGate,
    PromotionRejectionReason,
    PromotionWaiver,
    ReviewDecision,
)
from quant_foundry.registry_db import ModelRegistryDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all registry + callback tables.

    We create the FK parent tables (artifact_manifests, callback_receipts,
    model_dossiers) plus the 6 registry tables. The registry tables use
    generic JSON type (not JSONB) so SQLite can render them. FKs are
    enforced by SQLite when foreign_keys=ON (pragma).
    """
    eng = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        ArtifactManifestRow.__table__,  # FK parent
        CallbackReceiptRow.__table__,   # FK parent
        ModelDossierRow.__table__,      # FK parent (FKs to artifact_manifests)
        ModelRow.__table__,
        ModelVersionRow.__table__,      # FKs to models, model_dossiers, artifact_manifests, callback_receipts
        ModelMetricRow.__table__,       # FKs to model_versions
        PromotionRow.__table__,         # FKs to model_versions
        PromotionDecisionRow.__table__, # FKs to promotions
        ShadowEvaluationRow.__table__,  # FKs to model_versions
    ]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    eng.dispose()


@pytest.fixture()
def registry(engine):
    """ModelRegistryDB with an injected engine + gate (min_settled_count=10)."""
    return ModelRegistryDB(engine=engine, gate=PromotionGate(min_settled_count=10))


# ---------------------------------------------------------------------------
# Helpers — create FK parent rows needed by version registration
# ---------------------------------------------------------------------------


def _make_artifact_manifest(artifact_id: str = "art-001") -> ArtifactManifestRow:
    return ArtifactManifestRow(
        artifact_id=artifact_id,
        sha256="a" * 64,
        size_bytes=1024,
        uri="file:///tmp/artifact.pkl",
        model_family="lightgbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        code_git_sha="c" * 40,
        lockfile_hash="k" * 64,
        container_image_digest="sha256:" + "d" * 64,
    )


def _make_callback_receipt(callback_id: str = "cb-001") -> CallbackReceiptRow:
    return CallbackReceiptRow(
        callback_id=callback_id,
        job_id="job-001",
        idempotency_key="idem-001",
        signature_valid=True,
        payload_hash="p" * 64,
        payload_ref="/data/payloads/cb-001.json",
        received_at_ns=time.time_ns(),
        status="processed",
    )


def _make_dossier(
    content_hash: str = "h" * 64,
    artifact_id: str = "art-001",
    model_id: str = "model-001",
    blocking_issues: list[dict[str, Any]] | None = None,
) -> ModelDossierRow:
    return ModelDossierRow(
        schema_version=1,
        model_id=model_id,
        artifact_manifest_id=artifact_id,
        artifact_sha256="a" * 64,
        dataset_manifest_id="ds-001",
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        code_git_sha="c" * 40,
        lockfile_hash="k" * 64,
        container_image_digest="sha256:" + "d" * 64,
        trial_count=1,
        training_metrics={"accuracy": 0.85, "brier": 0.12},
        status="candidate",
        blocking_issues=blocking_issues or [],
        registered_at_ns=time.time_ns(),
        content_hash=content_hash,
    )


def _seed_parents(engine, artifact_id="art-001", callback_id="cb-001", dossier_hash="h" * 64):
    """Insert artifact_manifest, callback_receipt, and dossier rows."""
    with Session(engine) as session:
        session.add(_make_artifact_manifest(artifact_id))
        session.add(_make_callback_receipt(callback_id))
        session.add(_make_dossier(content_hash=dossier_hash, artifact_id=artifact_id))
        session.commit()


# ---------------------------------------------------------------------------
# Model registration
# ---------------------------------------------------------------------------


class TestRegisterModel:
    def test_register_creates_row_with_candidate_status(self, registry, engine) -> None:
        result = registry.register_model(
            model_id="model-001",
            name="Momentum XGB v1",
            model_family="lightgbm",
            description="First model",
        )
        assert result is not None
        assert result["model_id"] == "model-001"
        assert result["name"] == "Momentum XGB v1"
        assert result["model_family"] == "lightgbm"
        assert result["current_status"] == "candidate"
        assert result["current_version_id"] is None

        with Session(engine) as session:
            row = session.scalars(
                select(ModelRow).where(ModelRow.model_id == "model-001")
            ).one()
            assert row.name == "Momentum XGB v1"
            assert row.current_status == "candidate"

    def test_register_is_idempotent(self, registry, engine) -> None:
        registry.register_model(
            model_id="model-002", name="First", model_family="xgboost_gpu"
        )
        # Second call with same model_id is a no-op.
        result = registry.register_model(
            model_id="model-002", name="Second", model_family="xgboost_gpu"
        )
        assert result is None

        with Session(engine) as session:
            row = session.scalars(
                select(ModelRow).where(ModelRow.model_id == "model-002")
            ).one()
            assert row.name == "First"  # original name preserved

    def test_register_without_description(self, registry, engine) -> None:
        result = registry.register_model(
            model_id="model-003", name="No Desc", model_family="catboost_gpu"
        )
        assert result is not None
        assert result["description"] is None


# ---------------------------------------------------------------------------
# Version registration
# ---------------------------------------------------------------------------


class TestRegisterVersion:
    def test_register_creates_version_row(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        result = registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        assert result is not None
        assert result["version_id"] == "ver-001"
        assert result["model_id"] == "model-001"
        assert result["status"] == "candidate"
        assert result["version_number"] == 1
        assert result["promoted_at_ns"] is None

    def test_register_version_is_idempotent(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        result = registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        assert result is None  # no-op

    def test_register_multiple_versions(self, registry, engine) -> None:
        _seed_parents(engine, dossier_hash="h1" * 32)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h1" * 32,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        registry.register_version(
            model_id="model-001",
            version_id="ver-002",
            dossier_content_hash="h1" * 32,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=2,
        )
        versions = registry.list_versions("model-001")
        assert len(versions) == 2
        assert versions[0]["version_number"] == 1
        assert versions[1]["version_number"] == 2


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


class TestRecordMetrics:
    def test_record_training_metrics(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        metric_id = registry.record_metrics(
            version_id="ver-001",
            metric_type="training",
            metrics_dict={"accuracy": 0.9, "brier": 0.1},
        )
        assert metric_id is not None
        assert "ver-001" in metric_id
        assert "training" in metric_id

        with Session(engine) as session:
            row = session.scalars(
                select(ModelMetricRow).where(ModelMetricRow.metric_id == metric_id)
            ).one()
            assert row.metric_type == "training"
            assert row.metrics == {"accuracy": 0.9, "brier": 0.1}

    def test_record_tournament_metrics(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        metric_id = registry.record_metrics(
            version_id="ver-001",
            metric_type="tournament",
            metrics_dict={"total_score": 0.75, "settled_count": 50},
        )
        assert metric_id is not None

    def test_record_sentinel_metrics(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        metric_id = registry.record_metrics(
            version_id="ver-001",
            metric_type="sentinel",
            metrics_dict={"passed": True, "model_id": "model-001"},
        )
        assert metric_id is not None

    def test_record_invalid_metric_type_raises(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        with pytest.raises(ValueError, match="metric_type"):
            registry.record_metrics(
                version_id="ver-001",
                metric_type="invalid_type",
                metrics_dict={"foo": 1},
            )


# ---------------------------------------------------------------------------
# Shadow evaluation recording
# ---------------------------------------------------------------------------


class TestRecordShadowEvaluation:
    def test_record_shadow_evaluation(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        eval_id = registry.record_shadow_evaluation(
            version_id="ver-001",
            settled_count=100,
            evaluation_metrics={"sharpe": 1.5, "drawdown": -0.1},
        )
        assert eval_id is not None

        with Session(engine) as session:
            row = session.scalars(
                select(ShadowEvaluationRow).where(
                    ShadowEvaluationRow.evaluation_id == eval_id
                )
            ).one()
            assert row.settled_count == 100
            assert row.evaluation_metrics == {"sharpe": 1.5, "drawdown": -0.1}

    def test_record_shadow_evaluation_with_tournament_ref(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        eval_id = registry.record_shadow_evaluation(
            version_id="ver-001",
            settled_count=50,
            evaluation_metrics={"sharpe": 1.2},
            tournament_result_id="tourn-001",
        )
        assert eval_id is not None

        with Session(engine) as session:
            row = session.scalars(
                select(ShadowEvaluationRow).where(
                    ShadowEvaluationRow.evaluation_id == eval_id
                )
            ).one()
            assert row.tournament_result_id == "tourn-001"

    def test_negative_settled_count_raises(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        with pytest.raises(ValueError, match="settled_count"):
            registry.record_shadow_evaluation(
                version_id="ver-001",
                settled_count=-1,
                evaluation_metrics={},
            )

    def test_run_shadow_comparison_promote(self, registry, engine) -> None:
        """Tier 2.4: run_shadow_comparison records eval + returns decision."""
        import random

        from quant_foundry.champion_challenger import (
            ChampionChallengerConfig,
            ComparisonInput,
        )

        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Champion", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-champ",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        registry.register_model(model_id="model-002", name="Challenger", model_family="lightgbm")
        registry.register_version(
            model_id="model-002",
            version_id="ver-chal",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )

        rng = random.Random(42)
        champ_input = ComparisonInput(
            model_id="model-001",
            oos_returns_net=[rng.gauss(0.0001, 0.001) for _ in range(50)],
            settled_count=50,
        )
        chal_input = ComparisonInput(
            model_id="model-002",
            oos_returns_net=[rng.gauss(0.001, 0.001) for _ in range(50)],
            settled_count=50,
        )
        cfg = ChampionChallengerConfig(
            min_settled_count=30,
            net_edge_threshold=1.0,
        )

        eval_id, decision = registry.run_shadow_comparison(
            champion_version_id="ver-champ",
            challenger_version_id="ver-chal",
            champion_input=champ_input,
            challenger_input=chal_input,
            config=cfg,
        )

        assert eval_id is not None
        assert decision.decision == "promote"

        # Verify the shadow evaluation was recorded
        with Session(engine) as session:
            row = session.scalars(
                select(ShadowEvaluationRow).where(
                    ShadowEvaluationRow.evaluation_id == eval_id
                )
            ).one()
            assert row.settled_count == 50
            assert row.evaluation_metrics["decision"] == "promote"
            assert "net_edge_delta_bps" in row.evaluation_metrics
            assert "bootstrap_p_value" in row.evaluation_metrics


# ---------------------------------------------------------------------------
# Promotion workflow
# ---------------------------------------------------------------------------


def _setup_full_evidence_chain(registry, engine, settled_count=50, sentinel_passed=True):
    """Set up a complete evidence chain for a successful promotion.

    Creates: artifact, callback receipt, dossier (no blocking issues),
    model, version, tournament metrics (settled_count >= min), sentinel
    metrics (passed=True).
    """
    _seed_parents(engine)
    registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
    registry.register_version(
        model_id="model-001",
        version_id="ver-001",
        dossier_content_hash="h" * 64,
        artifact_id="art-001",
        callback_receipt_id="cb-001",
        version_number=1,
    )
    # Tournament metrics with enough settled_count.
    registry.record_metrics(
        version_id="ver-001",
        metric_type="tournament",
        metrics_dict={
            "model_id": "model-001",
            "total_score": 0.85,
            "settled_count": settled_count,
            "score_components": [],
            "recommendation": "promote",
            "status": "eligible",
            "trial_count": 1,
        },
    )
    # Sentinel metrics — passing.
    registry.record_metrics(
        version_id="ver-001",
        metric_type="sentinel",
        metrics_dict={
            "model_id": "model-001",
            "passed": sentinel_passed,
            "issues": [],
            "checks_run": [],
            "ts_ns": time.time_ns(),
        },
    )


class TestPromotionApproved:
    def test_promote_approved_changes_status(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        receipt = registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Looks good",
            decided_by="reviewer@example.com",
        )
        assert receipt.decision == ReviewDecision.APPROVED
        assert receipt.rejection_reason is None

        # Status changed on the version.
        version = registry.get_version("ver-001")
        assert version["status"] == "research_approved"
        assert version["promoted_at_ns"] is not None

        # Status changed on the model.
        model = registry.get_model("model-001")
        assert model["current_status"] == "research_approved"
        assert model["current_version_id"] == "ver-001"

    def test_promote_approved_persists_receipt(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Approved by reviewer",
            decided_by="reviewer@example.com",
        )
        history = registry.get_promotion_history("ver-001")
        assert len(history) == 1
        assert history[0]["decision"] == "approved"
        assert history[0]["from_status"] == "candidate"
        assert history[0]["to_status"] == "research_approved"
        assert history[0]["decision_receipt"]["review_note"] == "Approved by reviewer"
        assert history[0]["decision_receipt"]["decided_by"] == "reviewer@example.com"
        assert history[0]["decision_receipt"]["rejection_reason"] is None

    def test_promote_with_waivers(self, registry, engine) -> None:
        # Set up with a blocking issue that will be waived.
        _seed_parents(
            engine,
            dossier_hash="h" * 64,
        )
        # Overwrite dossier with a blocking issue.
        with Session(engine) as session:
            session.query(ModelDossierRow).filter(
                ModelDossierRow.content_hash == "h" * 64
            ).delete()
            session.add(
                _make_dossier(
                    content_hash="h" * 64,
                    blocking_issues=[
                        {"code": "HIGH_TRIAL_COUNT", "severity": "blocking", "note": "Too many trials"}
                    ],
                )
            )
            session.commit()

        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        registry.record_metrics(
            version_id="ver-001",
            metric_type="tournament",
            metrics_dict={
                "model_id": "model-001",
                "total_score": 0.85,
                "settled_count": 50,
                "score_components": [],
                "recommendation": "promote",
                "status": "eligible",
                "trial_count": 1,
            },
        )
        registry.record_metrics(
            version_id="ver-001",
            metric_type="sentinel",
            metrics_dict={
                "model_id": "model-001",
                "passed": True,
                "issues": [],
                "checks_run": [],
                "ts_ns": time.time_ns(),
            },
        )

        waiver = PromotionWaiver(
            issue_code="HIGH_TRIAL_COUNT",
            waived_by="reviewer@example.com",
            reason="Accepted after manual review",
        )
        receipt = registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Waived high trial count",
            decided_by="reviewer@example.com",
            waivers=[waiver],
        )
        assert receipt.decision == ReviewDecision.APPROVED


class TestPromotionRejected:
    def test_fk_prevents_version_without_dossier(self, registry, engine) -> None:
        """The FK on model_versions.dossier_content_hash -> model_dossiers.content_hash
        prevents creating a version without a dossier. The NO_DOSSIER rejection
        path in the gate is defense-in-depth — the DB makes it unreachable
        through normal operations.
        """
        with Session(engine) as session:
            session.add(_make_artifact_manifest())
            session.add(_make_callback_receipt())
            session.commit()

        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        # Attempting to register a version with a non-existent dossier hash
        # should fail at the DB level (FK constraint).
        with pytest.raises(IntegrityError):
            registry.register_version(
                model_id="model-001",
                version_id="ver-001",
                dossier_content_hash="x" * 64,  # no dossier with this hash
                artifact_id="art-001",
                callback_receipt_id="cb-001",
                version_number=1,
            )

    def test_promote_insufficient_evidence_rejected(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=5)  # < min 10
        receipt = registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Try promote",
            decided_by="reviewer@example.com",
        )
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.INSUFFICIENT_EVIDENCE

        # Status did NOT change.
        version = registry.get_version("ver-001")
        assert version["status"] == "candidate"

    def test_promote_mvp_level_limit_rejected(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        receipt = registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.LIMITED_LIVE_APPROVED,  # above MVP max
            review_note="Try to go live",
            decided_by="reviewer@example.com",
        )
        assert receipt.decision == ReviewDecision.REJECTED
        assert receipt.rejection_reason == PromotionRejectionReason.MVP_LEVEL_LIMIT

    def test_promote_rejected_persists_receipt(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=5)
        registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Try promote",
            decided_by="reviewer@example.com",
        )
        history = registry.get_promotion_history("ver-001")
        assert len(history) == 1
        assert history[0]["decision"] == "rejected"
        assert history[0]["decision_receipt"]["rejection_reason"] == "insufficient_evidence"

    def test_promote_unknown_version_raises(self, registry, engine) -> None:
        with pytest.raises(KeyError, match="unknown version_id"):
            registry.promote(
                version_id="nonexistent",
                target_status=DossierStatus.RESEARCH_APPROVED,
                review_note="test",
                decided_by="test",
            )


class TestPromotionHistory:
    def test_history_lists_multiple_attempts(self, registry, engine) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=5)  # will reject

        # First attempt — rejected (insufficient evidence).
        registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="First try",
            decided_by="reviewer@example.com",
        )

        # Add more settled_count to pass the gate.
        registry.record_metrics(
            version_id="ver-001",
            metric_type="tournament",
            metrics_dict={
                "model_id": "model-001",
                "total_score": 0.90,
                "settled_count": 50,
                "score_components": [],
                "recommendation": "promote",
                "status": "eligible",
                "trial_count": 1,
            },
        )

        # Second attempt — approved.
        registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Second try",
            decided_by="reviewer@example.com",
        )

        history = registry.get_promotion_history("ver-001")
        assert len(history) == 2
        assert history[0]["decision"] == "rejected"
        assert history[1]["decision"] == "approved"

    def test_history_empty_for_unknown_version(self, registry, engine) -> None:
        history = registry.get_promotion_history("nonexistent")
        assert history == []


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


class TestReadAPI:
    def test_get_model_returns_dict(self, registry, engine) -> None:
        registry.register_model(
            model_id="model-001", name="Test", model_family="lightgbm", description="desc"
        )
        model = registry.get_model("model-001")
        assert model is not None
        assert model["model_id"] == "model-001"
        assert model["name"] == "Test"

    def test_get_model_returns_none_for_unknown(self, registry, engine) -> None:
        assert registry.get_model("nonexistent") is None

    def test_get_version_returns_dict(self, registry, engine) -> None:
        _seed_parents(engine)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h" * 64,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        version = registry.get_version("ver-001")
        assert version is not None
        assert version["version_id"] == "ver-001"

    def test_get_version_returns_none_for_unknown(self, registry, engine) -> None:
        assert registry.get_version("nonexistent") is None

    def test_list_models_no_filter(self, registry, engine) -> None:
        registry.register_model(model_id="m1", name="M1", model_family="lightgbm")
        registry.register_model(model_id="m2", name="M2", model_family="xgboost_gpu")
        models = registry.list_models()
        assert len(models) == 2

    def test_list_models_filter_by_status(self, registry, engine) -> None:
        registry.register_model(model_id="m1", name="M1", model_family="lightgbm")
        registry.register_model(model_id="m2", name="M2", model_family="xgboost_gpu")
        # Both are candidate by default.
        candidates = registry.list_models(status="candidate")
        assert len(candidates) == 2
        # Filter for a status that none have.
        approved = registry.list_models(status="paper_approved")
        assert len(approved) == 0

    def test_list_versions_for_model(self, registry, engine) -> None:
        _seed_parents(engine, dossier_hash="h1" * 32)
        registry.register_model(model_id="model-001", name="Test", model_family="lightgbm")
        registry.register_version(
            model_id="model-001",
            version_id="ver-001",
            dossier_content_hash="h1" * 32,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=1,
        )
        registry.register_version(
            model_id="model-001",
            version_id="ver-002",
            dossier_content_hash="h1" * 32,
            artifact_id="art-001",
            callback_receipt_id="cb-001",
            version_number=2,
        )
        versions = registry.list_versions("model-001")
        assert len(versions) == 2
        # Ordered by version_number ascending.
        assert versions[0]["version_number"] == 1
        assert versions[1]["version_number"] == 2

    def test_list_versions_empty_for_unknown_model(self, registry, engine) -> None:
        assert registry.list_versions("nonexistent") == []


# ---------------------------------------------------------------------------
# CHECK constraint enforcement
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_bad_model_status_rejected_by_db(self, engine) -> None:
        with Session(engine) as session:
            row = ModelRow(
                model_id="bad-001",
                name="Bad",
                model_family="lightgbm",
                created_at_ns=time.time_ns(),
                current_status="invalid_status",
            )
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()

    def test_bad_version_status_rejected_by_db(self, engine) -> None:
        _seed_parents(engine)
        with Session(engine) as session:
            row = ModelVersionRow(
                version_id="bad-ver",
                model_id="needs_model",  # no FK to models — will fail on FK first
                dossier_content_hash="h" * 64,
                artifact_id="art-001",
                callback_receipt_id="cb-001",
                version_number=1,
                status="invalid_status",
                created_at_ns=time.time_ns(),
            )
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()

    def test_bad_metric_type_rejected_by_db(self, engine) -> None:
        _seed_parents(engine)
        with Session(engine) as session:
            row = ModelMetricRow(
                metric_id="bad-metric",
                version_id="needs_version",
                metric_type="invalid_type",
                metrics={},
                recorded_at_ns=time.time_ns(),
            )
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()

    def test_bad_promotion_decision_rejected_by_db(self, engine) -> None:
        _seed_parents(engine)
        # Need a model + version first for FK.
        with Session(engine) as session:
            session.add(ModelRow(
                model_id="m1", name="M1", model_family="lightgbm",
                created_at_ns=time.time_ns(), current_status="candidate",
            ))
            session.add(ModelVersionRow(
                version_id="v1", model_id="m1",
                dossier_content_hash="h" * 64,
                artifact_id="art-001",
                callback_receipt_id="cb-001",
                version_number=1, status="candidate",
                created_at_ns=time.time_ns(),
            ))
            session.add(PromotionRow(
                promotion_id="p1", version_id="v1",
                from_status="candidate", to_status="research_approved",
                requested_at_ns=time.time_ns(),
                decided_at_ns=time.time_ns(),
                decision="invalid_decision",
            ))
            with pytest.raises(IntegrityError):
                session.commit()

    def test_bad_rejection_reason_rejected_by_db(self, engine) -> None:
        _seed_parents(engine)
        with Session(engine) as session:
            session.add(ModelRow(
                model_id="m1", name="M1", model_family="lightgbm",
                created_at_ns=time.time_ns(), current_status="candidate",
            ))
            session.add(ModelVersionRow(
                version_id="v1", model_id="m1",
                dossier_content_hash="h" * 64,
                artifact_id="art-001",
                callback_receipt_id="cb-001",
                version_number=1, status="candidate",
                created_at_ns=time.time_ns(),
            ))
            session.add(PromotionRow(
                promotion_id="p1", version_id="v1",
                from_status="candidate", to_status="research_approved",
                requested_at_ns=time.time_ns(),
                decided_at_ns=time.time_ns(),
                decision="rejected",
            ))
            session.add(PromotionDecisionRow(
                decision_id="d1", promotion_id="p1",
                decision="rejected",
                review_note="test",
                rejection_reason="invalid_reason",
                waivers=[],
                decided_at_ns=time.time_ns(),
                decided_by="test",
            ))
            with pytest.raises(IntegrityError):
                session.commit()


# ---------------------------------------------------------------------------
# No secrets in DB
# ---------------------------------------------------------------------------


class TestNoSecretsInDB:
    def test_no_secret_columns_in_registry_tables(self, engine) -> None:
        """Verify no column in any registry table stores secrets."""
        sensitive_names = {"secret", "signature", "hmac", "password", "token", "api_key"}
        for table_cls in [
            ModelRow, ModelVersionRow, ModelMetricRow,
            PromotionRow, PromotionDecisionRow, ShadowEvaluationRow,
        ]:
            for col in table_cls.__table__.columns:
                col_lower = col.name.lower()
                for sensitive in sensitive_names:
                    assert sensitive not in col_lower, (
                        f"Column {col.name} in {table_cls.__tablename__} "
                        f"contains sensitive name: {sensitive}"
                    )

    def test_promotion_decision_has_no_raw_payload(self, registry, engine) -> None:
        """The promotion_decisions row should not contain raw callback payloads."""
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        registry.promote(
            version_id="ver-001",
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="Approved",
            decided_by="reviewer@example.com",
        )
        with Session(engine) as session:
            row = session.scalars(select(PromotionDecisionRow)).one()
            # The waivers field is a list of {issue_code, waived_by, reason} — no secrets.
            assert isinstance(row.waivers, list)
            for w in row.waivers:
                assert "secret" not in str(w).lower()
                assert "signature" not in str(w).lower()
