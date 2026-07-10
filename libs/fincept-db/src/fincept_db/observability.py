"""SQLAlchemy 2.0 ORM models for observability and cost tracking (migration 0004b).

These models mirror the four tables created by the ``0004b_observability``
Alembic migration. They follow the same declarative style as
``fincept_db.callback_tables`` (``DeclarativeBase`` subclass via the shared
``Base``, ``Mapped`` / ``mapped_column``, generic ``JSON`` type for
cross-dialect test compatibility, BigInteger for nanosecond timestamps).

The models are registered on the shared ``Base`` from ``fincept_db.models`` so
``Base.metadata.create_all`` (used by the test fixtures) creates them alongside
the existing tables. The migration is the source of truth for production
schemas; these models exist so the ``CostTracker`` can use typed ORM rows
instead of raw SQL.

Security invariants (mirrors callback_tables.py):
  - No column stores the callback secret, the HMAC signature bytes, or the
    raw request payload. ``training_jobs.request_payload_ref`` is a file path
    to the request JSON on disk, never the payload itself.
  - ``callback_receipt_id`` is a FK to ``callback_receipts.callback_id``,
    set when the callback arrives — it links the job to its receipt without
    duplicating any receipt data.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

JSONDict = dict[str, Any]


class TrainingJobRow(Base):
    """ORM row for the ``training_jobs`` table (one row per dispatched job)."""

    __tablename__ = "training_jobs"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="dispatched")
    dispatched_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completed_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_timeout_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gpu_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    container_image: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_payload_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    callback_receipt_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("callback_receipts.callback_id"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('canary','research','production')",
            name="ck_training_jobs_mode_domain",
        ),
        CheckConstraint(
            "status IN ('dispatched','running','completed','failed','cancelled','timed_out')",
            name="ck_training_jobs_status_domain",
        ),
        CheckConstraint("gpu_count >= 0", name="ck_training_jobs_gpu_count_nonneg"),
        Index("ix_training_jobs_model_family", "model_family"),
        Index("ix_training_jobs_status", "status"),
        Index("ix_training_jobs_dispatched_at_ns", "dispatched_at_ns"),
        Index("ix_training_jobs_callback_receipt_id", "callback_receipt_id"),
    )


class JobCostEventRow(Base):
    """ORM row for the ``job_cost_events`` table (cost events for a job)."""

    __tablename__ = "job_cost_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("training_jobs.job_id"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    recorded_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extra_metadata: Mapped[JSONDict | None] = mapped_column("metadata", JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('gpu_seconds','storage_gb_hours','network_egress_gb','overhead')",
            name="ck_job_cost_events_event_type_domain",
        ),
        CheckConstraint("amount >= 0", name="ck_job_cost_events_amount_nonneg"),
        CheckConstraint("unit_cost >= 0", name="ck_job_cost_events_unit_cost_nonneg"),
        CheckConstraint("total_cost >= 0", name="ck_job_cost_events_total_cost_nonneg"),
        Index("ix_job_cost_events_job_id", "job_id"),
        Index("ix_job_cost_events_event_type", "event_type"),
        Index("ix_job_cost_events_recorded_at_ns", "recorded_at_ns"),
    )


class JobMetricRow(Base):
    """ORM row for the ``job_metrics`` table (operational metrics for a job)."""

    __tablename__ = "job_metrics"

    metric_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("training_jobs.job_id"),
        nullable=False,
    )
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "metric_type IN ('duration','gpu_utilization','memory_usage',"
            "'disk_usage','cold_start','queue_time')",
            name="ck_job_metrics_metric_type_domain",
        ),
        Index("ix_job_metrics_job_id", "job_id"),
        Index("ix_job_metrics_metric_type", "metric_type"),
        Index("ix_job_metrics_recorded_at_ns", "recorded_at_ns"),
    )


class CostSummaryRow(Base):
    """ORM row for the ``cost_summary`` table (period cost rollup)."""

    __tablename__ = "cost_summary"

    summary_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_end_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    total_gpu_seconds: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    __table_args__ = (
        UniqueConstraint(
            "model_family",
            "period_start_ns",
            name="uq_cost_summary_model_family_period_start",
        ),
        Index("ix_cost_summary_model_family", "model_family"),
        Index("ix_cost_summary_period_start_ns", "period_start_ns"),
    )
