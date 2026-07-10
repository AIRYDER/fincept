"""Integration tests for the model registry API routes (TASK-mr7bianu).

Proves the three acceptance criteria:
  1. GET /quant-foundry/registry/models returns a list.
  2. POST /quant-foundry/registry/promote with valid evidence returns a
     promotion receipt.
  3. 503 when the registry is not configured.

The tests construct a minimal FastAPI app with only the quant_foundry
router (no Redis/lifespan) and a ModelRegistryDB backed by an in-memory
SQLite engine with all registry + callback tables created.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quant_foundry.promotion import PromotionGate
from quant_foundry.registry_db import ModelRegistryDB
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all registry + callback tables.

    Uses ``StaticPool`` + ``check_same_thread=False`` so the single
    in-memory connection is shared across threads (the TestClient runs
    the ASGI app in a separate thread from the test function).
    """
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        ArtifactManifestRow.__table__,
        CallbackReceiptRow.__table__,
        ModelDossierRow.__table__,
        ModelRow.__table__,
        ModelVersionRow.__table__,
        ModelMetricRow.__table__,
        PromotionRow.__table__,
        PromotionDecisionRow.__table__,
        ShadowEvaluationRow.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    eng.dispose()


@pytest.fixture()
def registry(engine):
    """ModelRegistryDB with an injected SQLite engine + gate."""
    return ModelRegistryDB(engine=engine, gate=PromotionGate(min_settled_count=10))


@pytest.fixture()
def auth_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """A valid JWT signed with the test secret."""
    monkeypatch.setenv("FINCEPT_JWT_SECRET", "test-secret-needs-to-be-long-enough")
    from fincept_core.config import Settings

    Settings.clear_cache()
    from api.auth import encode_token

    return encode_token({"sub": "test-reviewer"})


@pytest.fixture()
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture()
def client_with_registry(registry: ModelRegistryDB) -> TestClient:
    """TestClient wired to a minimal FastAPI app with the registry installed."""
    from api.routes.quant_foundry import router

    app = FastAPI()
    app.include_router(router, prefix="/quant-foundry")
    app.state.quant_foundry_registry = registry
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client_without_registry() -> TestClient:
    """TestClient with NO registry on app.state (disabled state â†’ 503)."""
    from api.routes.quant_foundry import router

    app = FastAPI()
    app.include_router(router, prefix="/quant-foundry")
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers â€” seed FK parent rows + full evidence chain
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
        blocking_issues=[],
        registered_at_ns=time.time_ns(),
        content_hash=content_hash,
    )


def _seed_parents(engine, artifact_id="art-001", callback_id="cb-001", dossier_hash="h" * 64):
    with Session(engine) as session:
        session.add(_make_artifact_manifest(artifact_id))
        session.add(_make_callback_receipt(callback_id))
        session.add(_make_dossier(content_hash=dossier_hash, artifact_id=artifact_id))
        session.commit()


def _setup_full_evidence_chain(registry, engine, settled_count=50):
    """Seed a complete evidence chain for a successful promotion."""
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
    registry.record_metrics(
        version_id="ver-001",
        metric_type="sentinel",
        metrics_dict={
            "model_id": "model-001",
            "passed": True,
            "issues": [],
            "checks_run": [],
            "ts_ns": time.time_ns(),
            "pbo": None,
            "pbo_flagged": False,
        },
    )
    # C7: selfcheck, PIT evidence, feature_set, backend metrics.
    registry.record_metrics(
        version_id="ver-001",
        metric_type="selfcheck",
        metrics_dict={
            "passed": True,
            "n_rows_scored": 10,
            "bundle_sha256": "a" * 64,
        },
    )
    registry.record_metrics(
        version_id="ver-001",
        metric_type="pit_evidence",
        metrics_dict={"verified": True, "evidence_sha256": "e" * 64},
    )
    registry.record_metrics(
        version_id="ver-001",
        metric_type="feature_set",
        metrics_dict={"feature_set_version": "fs-v1"},
    )
    registry.record_metrics(
        version_id="ver-001",
        metric_type="backend",
        metrics_dict={"production_eligible": True},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistryListModels:
    def test_get_registry_models_returns_list(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        auth_headers: dict[str, str],
    ) -> None:
        # Seed a model.
        registry.register_model(model_id="model-001", name="Momentum XGB", model_family="lightgbm")
        response = client_with_registry.get("/quant-foundry/registry/models", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["model_id"] == "model-001"
        assert body[0]["name"] == "Momentum XGB"

    def test_get_registry_models_requires_auth(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
    ) -> None:
        registry.register_model(model_id="model-001", name="Momentum XGB", model_family="lightgbm")
        response = client_with_registry.get("/quant-foundry/registry/models")
        assert response.status_code == 401

    def test_get_registry_models_empty_returns_list(
        self,
        client_with_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_with_registry.get("/quant-foundry/registry/models", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []


class TestRegistryGetModel:
    def test_get_model_detail(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        auth_headers: dict[str, str],
    ) -> None:
        registry.register_model(model_id="model-001", name="Test Model", model_family="xgboost_gpu")
        response = client_with_registry.get(
            "/quant-foundry/registry/models/model-001", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model_id"] == "model-001"
        assert body["name"] == "Test Model"

    def test_get_model_unknown_returns_404(
        self,
        client_with_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_with_registry.get(
            "/quant-foundry/registry/models/nonexistent", headers=auth_headers
        )
        assert response.status_code == 404


class TestRegistryListVersions:
    def test_list_versions(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
        auth_headers: dict[str, str],
    ) -> None:
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
        response = client_with_registry.get(
            "/quant-foundry/registry/models/model-001/versions", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["version_id"] == "ver-001"


class TestRegistryListMetrics:
    def test_list_metrics(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
        auth_headers: dict[str, str],
    ) -> None:
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
        registry.record_metrics(
            version_id="ver-001",
            metric_type="training",
            metrics_dict={"accuracy": 0.9},
        )
        response = client_with_registry.get(
            "/quant-foundry/registry/models/model-001/metrics", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["metric_type"] == "training"


class TestRegistryPromote:
    def test_promote_with_valid_evidence_returns_receipt(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
        auth_headers: dict[str, str],
    ) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        response = client_with_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "model-001",
                "target_level": "research_approved",
                "review_note": "looks good",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "approved"
        assert body["request"]["model_id"] == "model-001"
        assert body["request"]["target_level"] == "research_approved"
        assert body["review_note"] == "looks good"

    def test_promote_requires_auth(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
    ) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        response = client_with_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "model-001",
                "target_level": "research_approved",
                "review_note": "",
            },
        )
        assert response.status_code == 401

    def test_promote_unknown_model_returns_404(
        self,
        client_with_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_with_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "nonexistent",
                "target_level": "research_approved",
                "review_note": "",
            },
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_promote_invalid_target_level_returns_422(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
        auth_headers: dict[str, str],
    ) -> None:
        _setup_full_evidence_chain(registry, engine, settled_count=50)
        response = client_with_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "model-001",
                "target_level": "invalid_level",
                "review_note": "",
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_promote_insufficient_evidence_fails_closed(
        self,
        client_with_registry: TestClient,
        registry: ModelRegistryDB,
        engine,
        auth_headers: dict[str, str],
    ) -> None:
        # settled_count=3 is below min_settled_count=10 â†’ gate rejects.
        _setup_full_evidence_chain(registry, engine, settled_count=3)
        response = client_with_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "model-001",
                "target_level": "research_approved",
                "review_note": "trying",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "rejected"
        assert body["rejection_reason"] == "insufficient_evidence"


class TestRegistryNotConfigured:
    def test_list_models_503_when_not_configured(
        self,
        client_without_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_without_registry.get(
            "/quant-foundry/registry/models", headers=auth_headers
        )
        assert response.status_code == 503

    def test_get_model_503_when_not_configured(
        self,
        client_without_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_without_registry.get(
            "/quant-foundry/registry/models/model-001", headers=auth_headers
        )
        assert response.status_code == 503

    def test_list_versions_503_when_not_configured(
        self,
        client_without_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_without_registry.get(
            "/quant-foundry/registry/models/model-001/versions",
            headers=auth_headers,
        )
        assert response.status_code == 503

    def test_list_metrics_503_when_not_configured(
        self,
        client_without_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_without_registry.get(
            "/quant-foundry/registry/models/model-001/metrics",
            headers=auth_headers,
        )
        assert response.status_code == 503

    def test_promote_503_when_not_configured(
        self,
        client_without_registry: TestClient,
        auth_headers: dict[str, str],
    ) -> None:
        response = client_without_registry.post(
            "/quant-foundry/registry/promote",
            json={
                "model_id": "model-001",
                "target_level": "research_approved",
                "review_note": "",
            },
            headers=auth_headers,
        )
        assert response.status_code == 503
