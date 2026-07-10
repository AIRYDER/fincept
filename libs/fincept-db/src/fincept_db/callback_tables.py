"""SQLAlchemy 2.0 ORM models for callback ingestion tables (migration 0004).

These models mirror the six tables created by the ``0004_callback_ingestion``
Alembic migration. They follow the same declarative style as
``fincept_db.models`` (``DeclarativeBase`` subclass, ``Mapped`` / ``mapped_column``,
JSONB for structured fields, BigInteger for nanosecond timestamps).

The models are registered on the shared ``Base`` from ``fincept_db.models`` so
``Base.metadata.create_all`` (used by the test fixtures) creates them alongside
the existing tables. The migration is the source of truth for production
schemas; these models exist so the DB-backed sinks can use typed ORM rows
instead of raw SQL.

Security invariants mirrored from the Pydantic layer:
  - ``shadow_predictions.authority`` has a CHECK constraint forcing
    ``'shadow-only'`` so the DB rejects a non-shadow prediction even if
    Python is bypassed.
  - No column stores the callback secret, the HMAC signature bytes, or the
    raw payload. ``callback_receipts.payload_ref`` is a file path to the
    raw payload on disk; ``payload_hash`` is the SHA-256 of that payload.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

JSONDict = dict[str, Any]


class ArtifactManifestRow(Base):
    """ORM row for the ``artifact_manifests`` table (mirrors ArtifactManifest)."""

    __tablename__ = "artifact_manifests"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lockfile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_image_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifact_manifests_size_nonneg"),
        Index("ix_artifact_manifests_sha256", "sha256"),
        Index("ix_artifact_manifests_model_family", "model_family"),
    )


class ModelDossierRow(Base):
    """ORM row for the ``model_dossiers`` table (mirrors DossierRecord)."""

    __tablename__ = "model_dossiers"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_manifest_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("artifact_manifests.artifact_id"),
        nullable=False,
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_manifest_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_manifest_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lockfile_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_image_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hardware_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trial_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    training_metrics: Mapped[JSONDict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    settlement_evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    shadow_prediction_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    blocking_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    registered_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)

    __table_args__ = (
        CheckConstraint("trial_count >= 0", name="ck_model_dossiers_trial_count_nonneg"),
        CheckConstraint(
            "status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected','retired')",
            name="ck_model_dossiers_status_domain",
        ),
        Index("ix_model_dossiers_model_id", "model_id"),
        Index("ix_model_dossiers_status", "status"),
        Index("ix_model_dossiers_artifact_manifest_id", "artifact_manifest_id"),
    )


class CallbackReceiptRow(Base):
    """ORM row for the ``callback_receipts`` table (mirrors InboxRecord)."""

    __tablename__ = "callback_receipts"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    callback_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint(
            "status IN ('received','duplicate','processed','rejected','failed')",
            name="ck_callback_receipts_status_domain",
        ),
        Index("ix_callback_receipts_job_id", "job_id"),
        Index("ix_callback_receipts_idempotency_key", "idempotency_key"),
        Index("ix_callback_receipts_status", "status"),
        Index("ix_callback_receipts_received_at_ns", "received_at_ns"),
    )


class CallbackDlqRow(Base):
    """ORM row for the ``callback_dlq`` table (mirrors DLQRecord)."""

    __tablename__ = "callback_dlq"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dlq_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    callback_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    rejection_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    rejection_detail: Mapped[str] = mapped_column(String(512), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    backoff_base_seconds: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("1.0")
    )
    is_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint(
            "rejection_reason IN ('signature_failed','missing_required_fields',"
            "'artifact_verify_failed','duplicate_callback','stale_manifest',"
            "'payload_tamper','invalid_schema','job_id_mismatch',"
            "'domain_effect_failed')",
            name="ck_callback_dlq_rejection_reason_domain",
        ),
        Index("ix_callback_dlq_job_id", "job_id"),
        Index("ix_callback_dlq_rejection_reason", "rejection_reason"),
        Index(
            "ix_callback_dlq_is_retryable_next_retry",
            "is_retryable",
            "next_retry_at_ns",
        ),
    )


class CallbackMetricRow(Base):
    """ORM row for the ``callback_metrics`` table (mirrors metrics events)."""

    __tablename__ = "callback_metrics"

    ts_ns: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    event: Mapped[str] = mapped_column(String(16), primary_key=True, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "event IN ('received','accepted','rejected')",
            name="ck_callback_metrics_event_domain",
        ),
        Index("ix_callback_metrics_ts_ns", "ts_ns"),
        Index("ix_callback_metrics_event", "event"),
    )


class ShadowPredictionRow(Base):
    """ORM row for the ``shadow_predictions`` table (mirrors ShadowPrediction)."""

    __tablename__ = "shadow_predictions"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prediction_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    horizon_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    p_up: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    feature_availability: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    batch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "authority = 'shadow-only'",
            name="ck_shadow_predictions_authority_shadow_only",
        ),
        Index("ix_shadow_predictions_model_id_ts", "model_id", "ts_event"),
        Index("ix_shadow_predictions_symbol_ts", "symbol", "ts_event"),
        Index("ix_shadow_predictions_batch_hash", "batch_hash"),
    )
