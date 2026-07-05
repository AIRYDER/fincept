"""Integration tests for the gateway CostTracker + DB sink backend wiring.

These tests prove the Phase A integration:
  1. Dispatch creates a ``training_jobs`` row via ``CostTracker``.
  2. A signed callback updates the job status and links the callback receipt
     id on the ``training_jobs`` row.
  3. ``QUANT_FOUNDRY_SINK_BACKEND=db`` routes callback ingestion through the
     DB-backed sinks (``DbDossierStore``, ``DbShadowLedgerStore``,
     ``CallbackReceiptDbStore``, ``CallbackDlqDbStore``,
     ``CallbackMetricsDbStore``) instead of the JSONL-backed sinks.

All tests use an in-memory SQLite engine (no Postgres required). The gateway
is constructed in ``runpod`` mode with a :class:`RecordingRunPodClient` so no
live RunPod API calls are made.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.db_sinks import (
    CallbackDlqDbStore,
    CallbackMetricsDbStore,
    CallbackReceiptDbStore,
    DbDossierStore,
    DbShadowLedgerStore,
)
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus
from quant_foundry.runpod_client import DispatchResult, DispatchStatus
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    ShadowPrediction,
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
    ShadowPredictionRow,
)
from fincept_db.models import Base
from fincept_db.observability import TrainingJobRow

# ---------------------------------------------------------------------------
# Recording RunPod client (no live API calls)
# ---------------------------------------------------------------------------


class RecordingRunPodClient:
    cost_per_dispatch_cents = 0

    def __init__(self, *, endpoint_id: str) -> None:
        self.endpoint_id = endpoint_id
        self.dispatches: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any]] = {}

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        runpod_job_id = f"rp-{self.endpoint_id}-{len(self.dispatches) + 1}"
        self.dispatches.append(
            {
                "job_id": job_id,
                "request_payload": request_payload,
                "budget_cents": budget_cents,
                "runpod_job_id": runpod_job_id,
            }
        )
        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.DISPATCHED,
            runpod_job_id=runpod_job_id,
        )

    def check_status(self, runpod_job_id: str) -> dict[str, Any]:
        return self.statuses.get(runpod_job_id, {"status": "IN_PROGRESS"})

    def check_health(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "status": "ok"}


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _training_payload(job_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "dataset:training",
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


def _signed_training_output(job_id: str, *, secret: str) -> dict[str, Any]:
    artifact = ArtifactManifest(
        artifact_id="artifact:integration",
        sha256="a" * 64,
        size_bytes=2048,
        uri=None,
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="feature-hash",
        label_schema_hash="label-hash",
        code_git_sha="git-sha",
        lockfile_hash="lock-hash",
        container_image_digest="container-digest",
    )
    dossier = ModelDossier(
        model_id="model:qf:train:integration:1",
        artifact_manifest_id=artifact.artifact_id,
        dataset_manifest_id="dataset:training",
        code_git_sha="git-sha",
        lockfile_hash="lock-hash",
        container_image_digest="container-digest",
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
        worker_id="runpod-training",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier.model_dump(mode="json"),
            "artifact_manifest": artifact.model_dump(mode="json"),
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
        "callback_ts": ts,
    }


def _signed_inference_output(job_id: str, *, secret: str) -> dict[str, Any]:
    prediction = ShadowPrediction(
        prediction_id="pred:integration:1",
        model_id="model:qf:train:integration:1",
        symbol="AAPL",
        ts_event=1_000,
        horizon_ns=3_600_000_000_000,
        direction=0.42,
        confidence=0.74,
        authority=Authority.SHADOW_ONLY,
        p_up=0.61,
        feature_availability={"AAPL": True},
        latency_ms=3.5,
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-inference",
        result_type="inference_batch",
        payload={"predictions": [prediction.model_dump(mode="json")]},
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
        "callback_ts": ts,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all callback + observability tables.

    Creates the 6 callback ingestion tables plus the observability tables
    (training_jobs, job_cost_events, job_metrics, cost_summary). FK
    enforcement is enabled via a connect pragma so the
    training_jobs.callback_receipt_id -> callback_receipts.callback_id FK
    is honored.
    """
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
        ShadowPredictionRow.__table__,
        TrainingJobRow.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    eng.dispose()


@pytest.fixture()
def cost_tracker(engine):
    return CostTracker(engine=engine)


# ---------------------------------------------------------------------------
# Test 1: dispatch creates a training_jobs row via CostTracker
# ---------------------------------------------------------------------------


def test_dispatch_creates_training_jobs_row(tmp_path, engine, cost_tracker) -> None:
    """Dispatching a training job records a training_jobs row via CostTracker.

    The row is created with status='dispatched' and the model_family /
    gpu_type / gpu_count / execution_timeout_ms / container_image fields
    extracted from the request payload.
    """
    secret = "dispatch-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
        cost_tracker=cost_tracker,
    )

    job_id = "qf:train:cost:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )

    # The training_jobs row must exist with status='dispatched'.
    with Session(engine) as session:
        row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        assert row is not None, "training_jobs row should be created on dispatch"
        assert row.status == "dispatched"
        assert row.model_family == "gbm"
        # The gateway mode ("runpod") is a transport mode; the CostTracker
        # mode field is a deployment tier ('canary'/'research'/'production').
        # The gateway maps to 'canary' (the default shadow-only tier) unless
        # the payload overrides it via 'deployment_mode'.
        assert row.mode == "canary"
        assert row.gpu_type == "RTX_4090"
        assert row.gpu_count == 1
        assert row.execution_timeout_ms == 1_860_000
        assert row.container_image == "ghcr.io/fincept/quant-foundry-worker:latest"
        assert row.callback_receipt_id is None
        # request_payload_ref is a file path, never the raw payload.
        assert row.request_payload_ref is not None
        assert "request_payloads" in row.request_payload_ref


def test_dispatch_without_cost_tracker_does_not_crash(tmp_path) -> None:
    """When no CostTracker is injected, dispatch still works (best-effort)."""
    secret = "no-tracker-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
    )

    job_id = "qf:train:notracker:1"
    receipt = gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    assert receipt["enabled"] is True
    assert receipt["status"] is not None


# ---------------------------------------------------------------------------
# Test 2: callback updates status and links receipt
# ---------------------------------------------------------------------------


def test_callback_updates_status_and_links_receipt(tmp_path, engine, cost_tracker) -> None:
    """A signed training callback updates the training_jobs row status to
    'completed' and links the callback_receipt_id from the inbox record.
    """
    secret = "callback-link-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
        cost_tracker=cost_tracker,
    )

    job_id = "qf:train:link:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]

    # Simulate a RunPod completion with a signed training callback.
    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_training_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()
    assert receipts[0]["ok"] is True
    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

    # The training_jobs row must be updated: status='completed' + receipt linked.
    with Session(engine) as session:
        row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        assert row is not None
        assert row.status == "completed"
        assert row.completed_at_ns is not None
        # callback_receipt_id must be set to the inbox record's callback_id.
        assert row.callback_receipt_id is not None
        in_rec = gateway.inbox.get_by_job_id(job_id)
        assert in_rec is not None
        assert row.callback_receipt_id == in_rec.callback_id


def test_callback_inference_updates_status(tmp_path, engine, cost_tracker) -> None:
    """A signed inference callback updates the training_jobs row status."""
    secret = "infer-link-secret"
    inference_client = RecordingRunPodClient(endpoint_id="infer-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"inference": inference_client},
        cost_tracker=cost_tracker,
    )

    job_id = "qf:infer:link:1"
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "artifact_ref": "artifact:trained",
        "symbols": ["AAPL"],
        "horizons_ns": [3_600_000_000_000],
        "feature_snapshot_ref": "feature-snapshot:live",
        "model_id": "model:qf:infer:link:1",
        "decision_time": 1_000,
        "feature_rows": [
            {
                "symbol": "AAPL",
                "event_ts": 900,
                "decision_time": 1_000,
                "features": [
                    {"name": "momentum", "value": 0.25, "observed_at": 990},
                    {"name": "volatility", "value": 0.05, "observed_at": 990},
                ],
            }
        ],
        "expected_features": ["momentum", "volatility"],
        "model_family": "gbm",
    }
    gateway.create_job(
        job_id=job_id,
        job_type="inference",
        idempotency_key=f"idem-{job_id}",
        request_payload=payload,
    )
    runpod_job_id = inference_client.dispatches[0]["runpod_job_id"]

    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_inference_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()
    assert receipts[0]["ok"] is True
    assert receipts[0]["result"] == "processed"

    with Session(engine) as session:
        row = session.scalars(
            select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
        ).first()
        assert row is not None
        assert row.status == "completed"
        assert row.callback_receipt_id is not None


# ---------------------------------------------------------------------------
# Test 3: sink_backend=db routes through DB sinks
# ---------------------------------------------------------------------------


def test_sink_backend_db_uses_db_sinks(tmp_path, engine) -> None:
    """When sink_backend='db', the gateway constructs DB-backed sinks.

    The shadow_ledger and dossier_store must be the DB-backed implementations,
    and the dlq must be a DB-backed DLQ (not the JSONL CallbackDLQ). A signed
    training callback must write a dossier row to the DB (model_dossiers
    table) and a callback receipt row (callback_receipts table).
    """
    secret = "db-sink-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
        sink_backend="db",
        db_engine=engine,
    )

    # The sinks must be the DB-backed implementations.
    assert isinstance(gateway.shadow_ledger, DbShadowLedgerStore)
    assert isinstance(gateway.dossier_store, DbDossierStore)
    assert gateway._callback_receipt_db_store is not None
    assert isinstance(gateway._callback_receipt_db_store, CallbackReceiptDbStore)
    # The DLQ must be wired (not None) and backed by the DB store.
    assert gateway.dlq is not None
    assert isinstance(gateway._dlq_db_store, CallbackDlqDbStore)
    # The callback metrics store must be the DB-backed implementation.
    assert isinstance(gateway._callback_metrics_db_store, CallbackMetricsDbStore)
    assert isinstance(gateway.callback_metrics_store(), CallbackMetricsDbStore)

    job_id = "qf:train:dbsink:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]

    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_training_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()
    assert receipts[0]["ok"] is True
    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

    # The dossier must be written to the DB (model_dossiers table).
    with Session(engine) as session:
        dossiers = session.scalars(select(ModelDossierRow)).all()
        assert len(dossiers) == 1, "dossier should be written to the DB"
        assert dossiers[0].model_id == "model:qf:train:integration:1"

        # The callback receipt must be written to the DB (callback_receipts).
        receipts_rows = session.scalars(
            select(CallbackReceiptRow).where(CallbackReceiptRow.job_id == job_id)
        ).all()
        assert len(receipts_rows) >= 1, "callback receipt should be written to the DB"

        # The callback metrics events must be in the DB (callback_metrics).
        metric_rows = session.scalars(select(CallbackMetricRow)).all()
        events = {r.event for r in metric_rows}
        assert "received" in events
        assert "accepted" in events


def test_sink_backend_jsonl_uses_jsonl_sinks(tmp_path) -> None:
    """When sink_backend='jsonl' (default), the gateway uses JSONL sinks."""
    secret = "jsonl-sink-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
    )

    # The sinks must NOT be the DB-backed implementations.
    assert not isinstance(gateway.shadow_ledger, DbShadowLedgerStore)
    assert not isinstance(gateway.dossier_store, DbDossierStore)
    assert gateway._callback_receipt_db_store is None
    assert gateway._callback_metrics_db_store is None
    # DLQ is disabled by default in JSONL mode (backward compatible).
    assert gateway.dlq is None


def test_sink_backend_db_dlq_writes_to_db(tmp_path, engine) -> None:
    """A rejected callback in db mode writes a DLQ row to callback_dlq."""
    secret = "db-dlq-secret"
    training_client = RecordingRunPodClient(endpoint_id="train-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"training": training_client},
        sink_backend="db",
        db_engine=engine,
    )

    job_id = "qf:train:dbdlq:1"
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    runpod_job_id = training_client.dispatches[0]["runpod_job_id"]

    # Bad signature -> rejected -> DLQ row in the DB.
    training_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_training_output(job_id, secret="wrong-secret"),
    }

    receipts = gateway.poll_runpod_results()
    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "bad_signature"

    with Session(engine) as session:
        dlq_rows = session.scalars(select(CallbackDlqRow)).all()
        assert len(dlq_rows) == 1, "bad-signature callback should write a DLQ row"
        assert dlq_rows[0].job_id == job_id
        assert dlq_rows[0].rejection_reason == "signature_failed"


# ---------------------------------------------------------------------------
# Test 4: from_env reads QUANT_FOUNDRY_SINK_BACKEND
# ---------------------------------------------------------------------------


def test_from_env_reads_sink_backend(tmp_path, monkeypatch) -> None:
    """from_env() reads QUANT_FOUNDRY_SINK_BACKEND and passes it to __init__."""
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "local_mock")
    monkeypatch.setenv("QUANT_FOUNDRY_SHADOW_ONLY", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "env-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_BASE_DIR", str(tmp_path / "qf"))
    monkeypatch.setenv("QUANT_FOUNDRY_SINK_BACKEND", "db")

    gateway = QuantFoundryGateway.from_env()
    assert gateway.sink_backend == "db"
    assert isinstance(gateway.shadow_ledger, DbShadowLedgerStore)
    assert isinstance(gateway.dossier_store, DbDossierStore)


def test_from_env_defaults_to_jsonl(tmp_path, monkeypatch) -> None:
    """from_env() defaults to jsonl when QUANT_FOUNDRY_SINK_BACKEND is unset."""
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "local_mock")
    monkeypatch.setenv("QUANT_FOUNDRY_SHADOW_ONLY", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "env-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_BASE_DIR", str(tmp_path / "qf"))
    monkeypatch.delenv("QUANT_FOUNDRY_SINK_BACKEND", raising=False)

    gateway = QuantFoundryGateway.from_env()
    assert gateway.sink_backend == "jsonl"
    assert not isinstance(gateway.shadow_ledger, DbShadowLedgerStore)


def test_from_env_invalid_value_defaults_to_jsonl(tmp_path, monkeypatch) -> None:
    """An invalid QUANT_FOUNDRY_SINK_BACKEND value defaults to jsonl."""
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "local_mock")
    monkeypatch.setenv("QUANT_FOUNDRY_SHADOW_ONLY", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "env-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_BASE_DIR", str(tmp_path / "qf"))
    monkeypatch.setenv("QUANT_FOUNDRY_SINK_BACKEND", "invalid")

    gateway = QuantFoundryGateway.from_env()
    assert gateway.sink_backend == "jsonl"
