"""SQLAlchemy 2.0 ORM models for model registry tables (migration 0005).

These models mirror the six tables created by the ``0005_model_registry``
Alembic migration. They follow the same declarative style as
``fincept_db.callback_tables`` (``DeclarativeBase`` subclass, ``Mapped`` /
``mapped_column``, JSON for structured fields, BigInteger for nanosecond
timestamps).

The models are registered on the shared ``Base`` from ``fincept_db.models`` so
``Base.metadata.create_all`` (used by the test fixtures) creates them alongside
the existing tables. The migration is the source of truth for production
schemas; these models exist so the registry DB layer can use typed ORM rows
instead of raw SQL.

Security invariants mirrored from the Pydantic layer:
  - ``models.current_status`` and ``model_versions.status`` have CHECK
    constraints forcing values to the ``DossierStatus`` enum domain so the DB
    rejects a bad status even if Python is bypassed.
  - ``promotion_decisions.decision`` has a CHECK constraint forcing
    ``'approved'`` or ``'rejected'``.
  - ``promotion_decisions.rejection_reason`` has a CHECK constraint forcing
    values to the ``PromotionRejectionReason`` enum domain (or NULL).
  - ``model_metrics.metric_type`` has a CHECK constraint forcing values to
    ``'training'``, ``'tournament'``, ``'sentinel'``, ``'settlement'``.
  - No column stores the callback secret, the HMAC signature bytes, or the
    raw payload. ``promotion_decisions.waivers`` is a JSONB list of
    ``{issue_code, waived_by, reason}`` dicts — never secrets.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

JSONDict = dict[str, Any]
JSONList = list[dict[str, Any]]

# Shared status domain CHECK (matches DossierStatus enum values).
_STATUS_CHECK = (
    CheckConstraint(
        "current_status IN ('candidate','research_approved','shadow_approved',"
        "'paper_approved','limited_live_approved','rejected')",
        name="ck_models_current_status_domain",
    ),
)


class ModelRow(Base):
    """ORM row for the ``models`` table (top-level model identity)."""

    __tablename__ = "models"

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("model_versions.version_id", name="fk_models_current_version_id"),
        nullable=True,
    )
    current_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="candidate"
    )
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "current_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_models_current_status_domain",
        ),
        Index("ix_models_model_family", "model_family"),
        Index("ix_models_current_status", "current_status"),
    )


class ModelVersionRow(Base):
    """ORM row for the ``model_versions`` table (one row per training run)."""

    __tablename__ = "model_versions"

    version_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    model_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("models.model_id", name="fk_model_versions_model_id"),
        nullable=False,
    )
    dossier_content_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_dossiers.content_hash", name="fk_model_versions_dossier_content_hash"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("artifact_manifests.artifact_id", name="fk_model_versions_artifact_id"),
        nullable=False,
    )
    callback_receipt_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("callback_receipts.callback_id", name="fk_model_versions_callback_receipt_id"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    created_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    promoted_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_model_versions_status_domain",
        ),
        Index("ix_model_versions_model_id", "model_id"),
        Index("ix_model_versions_status", "status"),
    )


class ModelMetricRow(Base):
    """ORM row for the ``model_metrics`` table (validation metrics)."""

    __tablename__ = "model_metrics"

    metric_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model_versions.version_id", name="fk_model_metrics_version_id"),
        nullable=False,
    )
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[JSONDict] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "metric_type IN ('training','tournament','sentinel','settlement')",
            name="ck_model_metrics_metric_type_domain",
        ),
        Index("ix_model_metrics_version_id", "version_id"),
        Index("ix_model_metrics_metric_type", "metric_type"),
    )


class PromotionRow(Base):
    """ORM row for the ``promotions`` table (one row per promotion attempt)."""

    __tablename__ = "promotions"

    promotion_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model_versions.version_id", name="fk_promotions_version_id"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "from_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_promotions_from_status_domain",
        ),
        CheckConstraint(
            "to_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_promotions_to_status_domain",
        ),
        CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_promotions_decision_domain",
        ),
        Index("ix_promotions_version_id", "version_id"),
        Index("ix_promotions_decision", "decision"),
    )


class PromotionDecisionRow(Base):
    """ORM row for the ``promotion_decisions`` table (the immutable receipt).

    Mirrors the ``PromotionReceipt`` from ``quant_foundry.promotion``. The
    ``decision`` is ``'approved'`` or ``'rejected'``; ``rejection_reason`` is
    NULL on approval and one of the ``PromotionRejectionReason`` values on
    rejection. ``waivers`` is a JSONB list of
    ``{issue_code, waived_by, reason}`` dicts — never secrets.
    """

    __tablename__ = "promotion_decisions"

    decision_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, nullable=False, unique=True
    )
    promotion_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("promotions.promotion_id", name="fk_promotion_decisions_promotion_id"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    review_note: Mapped[str] = mapped_column(String(1024), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    waivers: Mapped[JSONList] = mapped_column(JSON, nullable=False, default=list)
    decided_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_promotion_decisions_decision_domain",
        ),
        CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('no_dossier','insufficient_evidence','sentinel_failed',"
            "'blocking_issue','mvp_level_limit')",
            name="ck_promotion_decisions_rejection_reason_domain",
        ),
        Index("ix_promotion_decisions_promotion_id", "promotion_id"),
        Index("ix_promotion_decisions_decision", "decision"),
    )


class ShadowEvaluationRow(Base):
    """ORM row for the ``shadow_evaluations`` table (aggregated shadow eval)."""

    __tablename__ = "shadow_evaluations"

    evaluation_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    version_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("model_versions.version_id", name="fk_shadow_evaluations_version_id"),
        nullable=False,
    )
    settled_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_metrics: Mapped[JSONDict] = mapped_column(JSON, nullable=False, default=dict)
    evaluated_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tournament_result_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "settled_count >= 0", name="ck_shadow_evaluations_settled_count_nonneg"
        ),
        Index("ix_shadow_evaluations_version_id", "version_id"),
    )
