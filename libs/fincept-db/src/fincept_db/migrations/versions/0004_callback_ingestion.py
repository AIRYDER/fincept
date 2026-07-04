"""callback ingestion tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04 00:00:00.000000

Persists the results of signed RunPod worker callbacks into fincept-db
(Postgres) so trained models become platform assets instead of dying with
the worker. Six tables mirror the Pydantic invariants already enforced on
the trusted side:

  - artifact_manifests   (ArtifactManifest)
  - model_dossiers       (DossierRecord, FK -> artifact_manifests)
  - callback_receipts    (InboxRecord audit trail)
  - callback_dlq         (DLQRecord dead-letter queue)
  - callback_metrics     (callback metrics events)
  - shadow_predictions   (ShadowPrediction, CHECK authority='shadow-only')

Design rules (see references/fincept-db-schema.md):
  - JSONB for structured fields, BigInteger for ns timestamps.
  - UNIQUE indexes on immutability keys (content_hash, artifact_id,
    callback_id, idempotency_key, prediction_id) so INSERT ... ON CONFLICT
    DO NOTHING provides DB-layer idempotency (defense in depth).
  - CHECK constraints for enum-like columns (status, rejection_reason,
    event, authority) so the DB rejects bad values even if Python is
    bypassed.
  - No secrets, no signature bytes, no raw payloads in any column. The
    receipt row stores signature_valid: bool + payload_hash + payload_ref
    (a file path to the raw payload on disk), never the secret, the
    signature, or the raw payload bytes.

Sync engine note: the CallbackProcessor is sync, so engine.py now exposes
get_sync_engine() + sync_session_scope() alongside the async engine. The
DB-backed sinks use sync sessions; the route stays sync. A second
connection pool is the cost — acceptable for the first cut (see
references/fincept-db-schema.md, option 1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- artifact_manifests (created first — model_dossiers FKs to it) ---
    op.create_table(
        "artifact_manifests",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("artifact_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("uri", sa.String(512), nullable=True),
        sa.Column("model_family", sa.String(64), nullable=False),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("label_schema_hash", sa.String(64), nullable=False),
        sa.Column("code_git_sha", sa.String(64), nullable=True),
        sa.Column("lockfile_hash", sa.String(64), nullable=True),
        sa.Column("container_image_digest", sa.String(128), nullable=True),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_manifests_size_nonneg"),
    )
    op.create_index("ix_artifact_manifests_sha256", "artifact_manifests", ["sha256"])
    op.create_index(
        "ix_artifact_manifests_model_family", "artifact_manifests", ["model_family"]
    )

    # --- model_dossiers ---
    op.create_table(
        "model_dossiers",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column(
            "artifact_manifest_id",
            sa.String(128),
            sa.ForeignKey("artifact_manifests.artifact_id"),
            nullable=False,
        ),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_manifest_id", sa.String(128), nullable=False),
        sa.Column("dataset_manifest_ref", sa.String(256), nullable=True),
        sa.Column("feature_schema_hash", sa.String(64), nullable=False),
        sa.Column("label_schema_hash", sa.String(64), nullable=False),
        sa.Column("code_git_sha", sa.String(64), nullable=True),
        sa.Column("lockfile_hash", sa.String(64), nullable=True),
        sa.Column("container_image_digest", sa.String(128), nullable=True),
        sa.Column("random_seed", sa.Integer, nullable=True),
        sa.Column("hardware_class", sa.String(64), nullable=True),
        sa.Column("trial_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "training_metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="'candidate'"
        ),
        sa.Column(
            "settlement_evidence_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "shadow_prediction_refs",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "blocking_issues",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("registered_at_ns", sa.BigInteger, nullable=True),
        sa.Column("content_hash", sa.String(64), primary_key=True, nullable=False),
        sa.CheckConstraint(
            "trial_count >= 0", name="ck_model_dossiers_trial_count_nonneg"
        ),
        sa.CheckConstraint(
            "status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_model_dossiers_status_domain",
        ),
    )
    op.create_index("ix_model_dossiers_model_id", "model_dossiers", ["model_id"])
    op.create_index("ix_model_dossiers_status", "model_dossiers", ["status"])
    op.create_index(
        "ix_model_dossiers_artifact_manifest_id",
        "model_dossiers",
        ["artifact_manifest_id"],
    )

    # --- callback_receipts ---
    op.create_table(
        "callback_receipts",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("callback_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("signature_valid", sa.Boolean, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_ref", sa.String(512), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("received_at_ns", sa.BigInteger, nullable=False),
        sa.Column("processed_at_ns", sa.BigInteger, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.String(512), nullable=True),
        sa.Column(
            "history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.CheckConstraint(
            "status IN ('received','duplicate','processed','rejected','failed')",
            name="ck_callback_receipts_status_domain",
        ),
    )
    op.create_index("ix_callback_receipts_job_id", "callback_receipts", ["job_id"])
    op.create_index(
        "ix_callback_receipts_idempotency_key",
        "callback_receipts",
        ["idempotency_key"],
    )
    op.create_index("ix_callback_receipts_status", "callback_receipts", ["status"])
    op.create_index(
        "ix_callback_receipts_received_at_ns", "callback_receipts", ["received_at_ns"]
    )

    # --- callback_dlq ---
    op.create_table(
        "callback_dlq",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("dlq_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("callback_id", sa.String(128), nullable=True),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("manifest_hash", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("rejection_reason", sa.String(32), nullable=False),
        sa.Column("rejection_detail", sa.String(512), nullable=False),
        sa.Column("payload_ref", sa.String(512), nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("next_retry_at_ns", sa.BigInteger, nullable=True),
        sa.Column(
            "backoff_base_seconds",
            sa.Numeric(10, 4),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("is_retryable", sa.Boolean, nullable=False),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.Column("updated_at_ns", sa.BigInteger, nullable=False),
        sa.Column(
            "history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.CheckConstraint(
            "rejection_reason IN ('signature_failed','missing_required_fields',"
            "'artifact_verify_failed','duplicate_callback','stale_manifest',"
            "'payload_tamper','invalid_schema','job_id_mismatch',"
            "'domain_effect_failed')",
            name="ck_callback_dlq_rejection_reason_domain",
        ),
    )
    op.create_index("ix_callback_dlq_job_id", "callback_dlq", ["job_id"])
    op.create_index(
        "ix_callback_dlq_rejection_reason", "callback_dlq", ["rejection_reason"]
    )
    op.create_index(
        "ix_callback_dlq_is_retryable_next_retry",
        "callback_dlq",
        ["is_retryable", "next_retry_at_ns"],
    )

    # --- callback_metrics ---
    op.create_table(
        "callback_metrics",
        sa.Column("ts_ns", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("event", sa.String(16), primary_key=True, nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "event IN ('received','accepted','rejected')",
            name="ck_callback_metrics_event_domain",
        ),
    )
    op.create_index("ix_callback_metrics_ts_ns", "callback_metrics", ["ts_ns"])
    op.create_index("ix_callback_metrics_event", "callback_metrics", ["event"])

    # --- shadow_predictions ---
    op.create_table(
        "shadow_predictions",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "prediction_id", sa.String(128), primary_key=True, nullable=False
        ),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("ts_event", sa.BigInteger, nullable=False),
        sa.Column("horizon_ns", sa.BigInteger, nullable=False),
        sa.Column("direction", sa.Numeric(28, 12), nullable=True),
        sa.Column("confidence", sa.Numeric(28, 12), nullable=True),
        sa.Column("authority", sa.String(32), nullable=False),
        sa.Column("p_up", sa.Numeric(28, 12), nullable=True),
        sa.Column("feature_availability", JSONB, nullable=True),
        sa.Column("latency_ms", sa.Numeric(10, 4), nullable=True),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("received_at_ns", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "authority = 'shadow-only'", name="ck_shadow_predictions_authority_shadow_only"
        ),
    )
    op.create_index(
        "ix_shadow_predictions_model_id_ts",
        "shadow_predictions",
        ["model_id", "ts_event"],
    )
    op.create_index(
        "ix_shadow_predictions_symbol_ts",
        "shadow_predictions",
        ["symbol", "ts_event"],
    )
    op.create_index(
        "ix_shadow_predictions_batch_hash", "shadow_predictions", ["batch_hash"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shadow_predictions_batch_hash", table_name="shadow_predictions"
    )
    op.drop_index(
        "ix_shadow_predictions_symbol_ts", table_name="shadow_predictions"
    )
    op.drop_index(
        "ix_shadow_predictions_model_id_ts", table_name="shadow_predictions"
    )
    op.drop_table("shadow_predictions")

    op.drop_index("ix_callback_metrics_event", table_name="callback_metrics")
    op.drop_index("ix_callback_metrics_ts_ns", table_name="callback_metrics")
    op.drop_table("callback_metrics")

    op.drop_index(
        "ix_callback_dlq_is_retryable_next_retry", table_name="callback_dlq"
    )
    op.drop_index(
        "ix_callback_dlq_rejection_reason", table_name="callback_dlq"
    )
    op.drop_index("ix_callback_dlq_job_id", table_name="callback_dlq")
    op.drop_table("callback_dlq")

    op.drop_index(
        "ix_callback_receipts_received_at_ns", table_name="callback_receipts"
    )
    op.drop_index("ix_callback_receipts_status", table_name="callback_receipts")
    op.drop_index(
        "ix_callback_receipts_idempotency_key", table_name="callback_receipts"
    )
    op.drop_index("ix_callback_receipts_job_id", table_name="callback_receipts")
    op.drop_table("callback_receipts")

    op.drop_index(
        "ix_model_dossiers_artifact_manifest_id", table_name="model_dossiers"
    )
    op.drop_index("ix_model_dossiers_status", table_name="model_dossiers")
    op.drop_index("ix_model_dossiers_model_id", table_name="model_dossiers")
    op.drop_table("model_dossiers")

    op.drop_index(
        "ix_artifact_manifests_model_family", table_name="artifact_manifests"
    )
    op.drop_index("ix_artifact_manifests_sha256", table_name="artifact_manifests")
    op.drop_table("artifact_manifests")
