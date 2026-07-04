"""model registry tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04 00:00:00.000000

Persists the model registry — the durable home for model identity, versions,
metrics, promotion decisions, and shadow evaluations. Six tables mirror the
Pydantic invariants already enforced on the trusted side (DossierStatus enum,
PromotionGate, PromotionReceipt):

  - models               (top-level model identity)
  - model_versions       (one row per training run / dossier)
  - model_metrics        (validation metrics: training, tournament, sentinel, settlement)
  - promotions           (one row per promotion attempt — audit trail)
  - promotion_decisions  (the immutable PromotionReceipt from PromotionGate)
  - shadow_evaluations   (aggregated shadow evaluation result)

Design rules (see references/fincept-db-schema.md):
  - JSONB for structured fields, BigInteger for ns timestamps.
  - CHECK constraints for enum-like columns (status, decision, metric_type,
    rejection_reason) so the DB rejects bad values even if Python is bypassed.
  - No secrets, no signature bytes, no raw payloads in any column. The
    promotion_decisions row stores the review_note + rejection_reason + waivers
    (JSONB list of issue_code/waived_by/reason), never the callback secret, the
    HMAC signature, or the raw payload bytes.
  - FK references to migration 0004 tables (artifact_manifests.artifact_id,
    callback_receipts.callback_id, model_dossiers.content_hash) so a version
    row cannot exist without its artifact, its callback receipt, and its
    dossier.

The registry persists evidence; the gate enforces. The registry's promote()
method assembles PromotionEvidence from the registry tables, calls
PromotionGate.evaluate(...), persists the resulting PromotionReceipt into
promotion_decisions, and only then updates model_versions.status /
models.current_status. If the gate rejects, status does NOT change but the
rejection receipt IS persisted (audit trail).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004b"
branch_labels: str | None = None
depends_on: str | None = None

# Shared status domain CHECK (matches DossierStatus enum values).
_STATUS_DOMAIN = (
    "status IN ('candidate','research_approved','shadow_approved',"
    "'paper_approved','limited_live_approved','rejected')"
)


def upgrade() -> None:
    # --- models (top-level model identity) ---
    op.create_table(
        "models",
        sa.Column("model_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("model_family", sa.String(64), nullable=False),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.Column("current_version_id", sa.String(128), nullable=True),
        sa.Column("current_status", sa.String(32), nullable=False, server_default="'candidate'"),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.ForeignKeyConstraint(
            ["current_version_id"],
            ["model_versions.version_id"],
            name="fk_models_current_version_id",
            use_alter=True,
        ),
        sa.CheckConstraint(
            "current_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_models_current_status_domain",
        ),
    )
    op.create_index("ix_models_model_family", "models", ["model_family"])
    op.create_index("ix_models_current_status", "models", ["current_status"])

    # --- model_versions (one row per training run / dossier) ---
    op.create_table(
        "model_versions",
        sa.Column("version_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("dossier_content_hash", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("callback_receipt_id", sa.String(128), nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="'candidate'"),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.Column("promoted_at_ns", sa.BigInteger, nullable=True),
        sa.ForeignKeyConstraint(
            ["model_id"], ["models.model_id"], name="fk_model_versions_model_id"
        ),
        sa.ForeignKeyConstraint(
            ["dossier_content_hash"],
            ["model_dossiers.content_hash"],
            name="fk_model_versions_dossier_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact_manifests.artifact_id"],
            name="fk_model_versions_artifact_id",
        ),
        sa.ForeignKeyConstraint(
            ["callback_receipt_id"],
            ["callback_receipts.callback_id"],
            name="fk_model_versions_callback_receipt_id",
        ),
        sa.CheckConstraint(
            "status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_model_versions_status_domain",
        ),
    )
    op.create_index("ix_model_versions_model_id", "model_versions", ["model_id"])
    op.create_index("ix_model_versions_status", "model_versions", ["status"])

    # --- model_metrics (validation metrics) ---
    op.create_table(
        "model_metrics",
        sa.Column("metric_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("metric_type", sa.String(32), nullable=False),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at_ns", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"], ["model_versions.version_id"], name="fk_model_metrics_version_id"
        ),
        sa.CheckConstraint(
            "metric_type IN ('training','tournament','sentinel','settlement')",
            name="ck_model_metrics_metric_type_domain",
        ),
    )
    op.create_index("ix_model_metrics_version_id", "model_metrics", ["version_id"])
    op.create_index("ix_model_metrics_metric_type", "model_metrics", ["metric_type"])

    # --- promotions (one row per promotion attempt — audit trail) ---
    op.create_table(
        "promotions",
        sa.Column("promotion_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("requested_at_ns", sa.BigInteger, nullable=False),
        sa.Column("decided_at_ns", sa.BigInteger, nullable=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"], ["model_versions.version_id"], name="fk_promotions_version_id"
        ),
        sa.CheckConstraint(
            "from_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_promotions_from_status_domain",
        ),
        sa.CheckConstraint(
            "to_status IN ('candidate','research_approved','shadow_approved',"
            "'paper_approved','limited_live_approved','rejected')",
            name="ck_promotions_to_status_domain",
        ),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_promotions_decision_domain",
        ),
    )
    op.create_index("ix_promotions_version_id", "promotions", ["version_id"])
    op.create_index("ix_promotions_decision", "promotions", ["decision"])

    # --- promotion_decisions (the immutable PromotionReceipt) ---
    op.create_table(
        "promotion_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True, nullable=False, unique=True),
        sa.Column("promotion_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("review_note", sa.String(1024), nullable=False),
        sa.Column("rejection_reason", sa.String(32), nullable=True),
        sa.Column("waivers", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("decided_at_ns", sa.BigInteger, nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(
            ["promotion_id"], ["promotions.promotion_id"], name="fk_promotion_decisions_promotion_id"
        ),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_promotion_decisions_decision_domain",
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('no_dossier','insufficient_evidence','sentinel_failed',"
            "'blocking_issue','mvp_level_limit')",
            name="ck_promotion_decisions_rejection_reason_domain",
        ),
    )
    op.create_index(
        "ix_promotion_decisions_promotion_id", "promotion_decisions", ["promotion_id"]
    )
    op.create_index(
        "ix_promotion_decisions_decision", "promotion_decisions", ["decision"]
    )

    # --- shadow_evaluations (aggregated shadow evaluation) ---
    op.create_table(
        "shadow_evaluations",
        sa.Column("evaluation_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("settled_count", sa.Integer, nullable=False),
        sa.Column("evaluation_metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evaluated_at_ns", sa.BigInteger, nullable=False),
        sa.Column("tournament_result_id", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["version_id"], ["model_versions.version_id"], name="fk_shadow_evaluations_version_id"
        ),
        sa.CheckConstraint(
            "settled_count >= 0", name="ck_shadow_evaluations_settled_count_nonneg"
        ),
    )
    op.create_index(
        "ix_shadow_evaluations_version_id", "shadow_evaluations", ["version_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shadow_evaluations_version_id", table_name="shadow_evaluations"
    )
    op.drop_table("shadow_evaluations")

    op.drop_index(
        "ix_promotion_decisions_decision", table_name="promotion_decisions"
    )
    op.drop_index(
        "ix_promotion_decisions_promotion_id", table_name="promotion_decisions"
    )
    op.drop_table("promotion_decisions")

    op.drop_index("ix_promotions_decision", table_name="promotions")
    op.drop_index("ix_promotions_version_id", table_name="promotions")
    op.drop_table("promotions")

    op.drop_index("ix_model_metrics_metric_type", table_name="model_metrics")
    op.drop_index("ix_model_metrics_version_id", table_name="model_metrics")
    op.drop_table("model_metrics")

    op.drop_index("ix_model_versions_status", table_name="model_versions")
    op.drop_index("ix_model_versions_model_id", table_name="model_versions")
    op.drop_table("model_versions")

    op.drop_index("ix_models_current_status", table_name="models")
    op.drop_index("ix_models_model_family", table_name="models")
    op.drop_table("models")
