"""observability and cost tracking tables

Revision ID: 0004b
Revises: 0004
Create Date: 2026-07-04 00:00:00.000000

Adds observability and cost tracking tables for the RunPod training
pipeline. Four tables record the lifecycle, cost, and operational
metrics of every training job dispatched to RunPod:

  - training_jobs     (one row per dispatched RunPod training job)
  - job_cost_events   (cost events for a training job)
  - job_metrics       (operational metrics for a training job)
  - cost_summary      (daily/period cost rollup per model_family)

Design rules (mirrors 0004_callback_ingestion — see references/fincept-db-schema.md):
  - JSONB for structured fields, BigInteger for ns timestamps.
  - UNIQUE indexes on immutability keys (job_id, event_id, metric_id,
    summary_id) so INSERT ... ON CONFLICT DO NOTHING provides DB-layer
    idempotency (defense in depth).
  - CHECK constraints for enum-like columns (status, mode, event_type,
    metric_type) so the DB rejects bad values even if Python is
    bypassed.
  - CHECK constraints for non-negative numerics (amount, unit_cost,
    total_cost) so the DB rejects negative costs even if Python is
    bypassed.
  - No secrets, no signature bytes, no raw payloads in any column. The
    training_jobs row stores ``request_payload_ref`` (a file path to
    the request JSON on disk), never the secret, the signature, or the
    raw payload bytes. ``callback_receipt_id`` is a FK to
    callback_receipts.callback_id, set when the callback arrives.

Revision note: this migration runs BETWEEN 0004 and 0005. The revision
ID is ``0004b`` with ``down_revision="0004"`` so Alembic orders it
after the callback ingestion tables (which training_jobs FKs to via
callback_receipt_id) and before any future 0005 migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004b"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- training_jobs (one row per dispatched RunPod training job) ---
    op.create_table(
        "training_jobs",
        sa.Column("job_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("model_family", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="'dispatched'"),
        sa.Column("dispatched_at_ns", sa.BigInteger, nullable=False),
        sa.Column("started_at_ns", sa.BigInteger, nullable=True),
        sa.Column("completed_at_ns", sa.BigInteger, nullable=True),
        sa.Column("execution_timeout_ms", sa.BigInteger, nullable=True),
        sa.Column("gpu_type", sa.String(64), nullable=True),
        sa.Column("gpu_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("container_image", sa.String(256), nullable=True),
        sa.Column("request_payload_ref", sa.String(512), nullable=True),
        sa.Column(
            "callback_receipt_id",
            sa.String(128),
            sa.ForeignKey("callback_receipts.callback_id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "mode IN ('canary','research','production')",
            name="ck_training_jobs_mode_domain",
        ),
        sa.CheckConstraint(
            "status IN ('dispatched','running','completed','failed','cancelled','timed_out')",
            name="ck_training_jobs_status_domain",
        ),
        sa.CheckConstraint(
            "gpu_count >= 0", name="ck_training_jobs_gpu_count_nonneg"
        ),
    )
    op.create_index("ix_training_jobs_model_family", "training_jobs", ["model_family"])
    op.create_index("ix_training_jobs_status", "training_jobs", ["status"])
    op.create_index(
        "ix_training_jobs_dispatched_at_ns", "training_jobs", ["dispatched_at_ns"]
    )
    op.create_index(
        "ix_training_jobs_callback_receipt_id", "training_jobs", ["callback_receipt_id"]
    )

    # --- job_cost_events (cost events for a training job) ---
    op.create_table(
        "job_cost_events",
        sa.Column("event_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.String(128),
            sa.ForeignKey("training_jobs.job_id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="'USD'"),
        sa.Column("recorded_at_ns", sa.BigInteger, nullable=False),
        sa.Column("metadata", JSONB, nullable=True),
        sa.CheckConstraint(
            "event_type IN ('gpu_seconds','storage_gb_hours','network_egress_gb','overhead')",
            name="ck_job_cost_events_event_type_domain",
        ),
        sa.CheckConstraint(
            "amount >= 0", name="ck_job_cost_events_amount_nonneg"
        ),
        sa.CheckConstraint(
            "unit_cost >= 0", name="ck_job_cost_events_unit_cost_nonneg"
        ),
        sa.CheckConstraint(
            "total_cost >= 0", name="ck_job_cost_events_total_cost_nonneg"
        ),
    )
    op.create_index("ix_job_cost_events_job_id", "job_cost_events", ["job_id"])
    op.create_index(
        "ix_job_cost_events_event_type", "job_cost_events", ["event_type"]
    )
    op.create_index(
        "ix_job_cost_events_recorded_at_ns", "job_cost_events", ["recorded_at_ns"]
    )

    # --- job_metrics (operational metrics for a training job) ---
    op.create_table(
        "job_metrics",
        sa.Column("metric_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.String(128),
            sa.ForeignKey("training_jobs.job_id"),
            nullable=False,
        ),
        sa.Column("metric_type", sa.String(32), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("recorded_at_ns", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "metric_type IN ('duration','gpu_utilization','memory_usage',"
            "'disk_usage','cold_start','queue_time')",
            name="ck_job_metrics_metric_type_domain",
        ),
    )
    op.create_index("ix_job_metrics_job_id", "job_metrics", ["job_id"])
    op.create_index(
        "ix_job_metrics_metric_type", "job_metrics", ["metric_type"]
    )
    op.create_index(
        "ix_job_metrics_recorded_at_ns", "job_metrics", ["recorded_at_ns"]
    )

    # --- cost_summary (daily/period cost rollup per model_family) ---
    op.create_table(
        "cost_summary",
        sa.Column("summary_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("model_family", sa.String(64), nullable=False),
        sa.Column("period_start_ns", sa.BigInteger, nullable=False),
        sa.Column("period_end_ns", sa.BigInteger, nullable=False),
        sa.Column("total_jobs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_cost", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column(
            "total_gpu_seconds", sa.Numeric(18, 6), nullable=False, server_default="0"
        ),
        sa.Column("currency", sa.String(8), nullable=False, server_default="'USD'"),
        sa.UniqueConstraint(
            "model_family",
            "period_start_ns",
            name="uq_cost_summary_model_family_period_start",
        ),
    )
    op.create_index(
        "ix_cost_summary_model_family", "cost_summary", ["model_family"]
    )
    op.create_index(
        "ix_cost_summary_period_start_ns", "cost_summary", ["period_start_ns"]
    )


def downgrade() -> None:
    op.drop_index("ix_cost_summary_period_start_ns", table_name="cost_summary")
    op.drop_index("ix_cost_summary_model_family", table_name="cost_summary")
    op.drop_table("cost_summary")

    op.drop_index("ix_job_metrics_recorded_at_ns", table_name="job_metrics")
    op.drop_index("ix_job_metrics_metric_type", table_name="job_metrics")
    op.drop_index("ix_job_metrics_job_id", table_name="job_metrics")
    op.drop_table("job_metrics")

    op.drop_index("ix_job_cost_events_recorded_at_ns", table_name="job_cost_events")
    op.drop_index("ix_job_cost_events_event_type", table_name="job_cost_events")
    op.drop_index("ix_job_cost_events_job_id", table_name="job_cost_events")
    op.drop_table("job_cost_events")

    op.drop_index(
        "ix_training_jobs_callback_receipt_id", table_name="training_jobs"
    )
    op.drop_index(
        "ix_training_jobs_dispatched_at_ns", table_name="training_jobs"
    )
    op.drop_index("ix_training_jobs_status", table_name="training_jobs")
    op.drop_index("ix_training_jobs_model_family", table_name="training_jobs")
    op.drop_table("training_jobs")
