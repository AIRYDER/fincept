"""promotion gate hardening (C7)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-09 00:00:00.000000

C7 hardens the promotion gate to enforce the full evidence chain
before any model can advance. This migration:

1. Adds ``retired`` to the status domain CHECK constraints on
   ``models.current_status``, ``model_versions.status``,
   ``promotions.from_status``, and ``promotions.to_status``.
   ``retired`` is a terminal status — no promotion path out of it.

2. Adds the new C7 rejection reasons to the
   ``promotion_decisions.rejection_reason`` CHECK constraint:
   - missing_selfcheck
   - selfcheck_failed
   - missing_bundle_sha256
   - missing_callback_receipt
   - callback_not_processed
   - missing_artifact_uri
   - dossier_hash_mismatch
   - backend_not_production_eligible
   - feature_set_version_not_verified
   - pit_evidence_missing
   - pit_evidence_not_verified
   - retired_is_terminal

No new tables or columns are added — only CHECK constraint updates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

# Updated status domain — adds 'retired'.
_STATUS_DOMAIN = (
    "status IN ('candidate','research_approved','shadow_approved',"
    "'paper_approved','limited_live_approved','rejected','retired')"
)

# Updated rejection reason domain — adds all C7 reasons.
_REJECTION_REASON_DOMAIN = (
    "rejection_reason IS NULL OR rejection_reason IN "
    "('no_dossier','insufficient_evidence','sentinel_failed',"
    "'blocking_issue','mvp_level_limit',"
    "'missing_selfcheck','selfcheck_failed','missing_bundle_sha256',"
    "'missing_callback_receipt','callback_not_processed',"
    "'missing_artifact_uri','dossier_hash_mismatch',"
    "'backend_not_production_eligible','feature_set_version_not_verified',"
    "'pit_evidence_missing','pit_evidence_not_verified',"
    "'retired_is_terminal')"
)

# Updated metric_type domain — adds C7 evidence metric types.
_METRIC_TYPE_DOMAIN = (
    "metric_type IN ('training','tournament','sentinel','settlement',"
    "'selfcheck','pit_evidence','feature_set','backend')"
)


def upgrade() -> None:
    # --- Update status CHECK constraints to include 'retired' ---
    # models.current_status
    op.drop_constraint("ck_models_current_status_domain", "models", type_="check")
    op.create_check_constraint(
        "ck_models_current_status_domain",
        "models",
        _STATUS_DOMAIN.replace("status IN", "current_status IN"),
    )

    # model_versions.status
    op.drop_constraint("ck_model_versions_status_domain", "model_versions", type_="check")
    op.create_check_constraint(
        "ck_model_versions_status_domain",
        "model_versions",
        _STATUS_DOMAIN,
    )

    # model_dossiers.status (created in 0004)
    op.drop_constraint("ck_model_dossiers_status_domain", "model_dossiers", type_="check")
    op.create_check_constraint(
        "ck_model_dossiers_status_domain",
        "model_dossiers",
        _STATUS_DOMAIN,
    )

    # promotions.from_status
    op.drop_constraint("ck_promotions_from_status_domain", "promotions", type_="check")
    op.create_check_constraint(
        "ck_promotions_from_status_domain",
        "promotions",
        _STATUS_DOMAIN.replace("status IN", "from_status IN"),
    )

    # promotions.to_status
    op.drop_constraint("ck_promotions_to_status_domain", "promotions", type_="check")
    op.create_check_constraint(
        "ck_promotions_to_status_domain",
        "promotions",
        _STATUS_DOMAIN.replace("status IN", "to_status IN"),
    )

    # --- Update rejection_reason CHECK constraint ---
    op.drop_constraint(
        "ck_promotion_decisions_rejection_reason_domain",
        "promotion_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_promotion_decisions_rejection_reason_domain",
        "promotion_decisions",
        _REJECTION_REASON_DOMAIN,
    )

    # --- Update model_metrics.metric_type CHECK constraint ---
    op.drop_constraint(
        "ck_model_metrics_metric_type_domain",
        "model_metrics",
        type_="check",
    )
    op.create_check_constraint(
        "ck_model_metrics_metric_type_domain",
        "model_metrics",
        _METRIC_TYPE_DOMAIN,
    )


def downgrade() -> None:
    # Original status domain (without 'retired').
    _OLD_STATUS_DOMAIN = (
        "status IN ('candidate','research_approved','shadow_approved',"
        "'paper_approved','limited_live_approved','rejected')"
    )
    _OLD_REJECTION_REASON_DOMAIN = (
        "rejection_reason IS NULL OR rejection_reason IN "
        "('no_dossier','insufficient_evidence','sentinel_failed',"
        "'blocking_issue','mvp_level_limit')"
    )
    _OLD_METRIC_TYPE_DOMAIN = (
        "metric_type IN ('training','tournament','sentinel','settlement')"
    )

    # Revert model_metrics.metric_type CHECK.
    op.drop_constraint(
        "ck_model_metrics_metric_type_domain",
        "model_metrics",
        type_="check",
    )
    op.create_check_constraint(
        "ck_model_metrics_metric_type_domain",
        "model_metrics",
        _OLD_METRIC_TYPE_DOMAIN,
    )

    # Revert rejection_reason CHECK.
    op.drop_constraint(
        "ck_promotion_decisions_rejection_reason_domain",
        "promotion_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_promotion_decisions_rejection_reason_domain",
        "promotion_decisions",
        _OLD_REJECTION_REASON_DOMAIN,
    )

    # Revert status CHECK constraints.
    op.drop_constraint("ck_promotions_to_status_domain", "promotions", type_="check")
    op.create_check_constraint(
        "ck_promotions_to_status_domain",
        "promotions",
        _OLD_STATUS_DOMAIN.replace("status IN", "to_status IN"),
    )

    op.drop_constraint("ck_promotions_from_status_domain", "promotions", type_="check")
    op.create_check_constraint(
        "ck_promotions_from_status_domain",
        "promotions",
        _OLD_STATUS_DOMAIN.replace("status IN", "from_status IN"),
    )

    op.drop_constraint("ck_model_dossiers_status_domain", "model_dossiers", type_="check")
    op.create_check_constraint(
        "ck_model_dossiers_status_domain",
        "model_dossiers",
        _OLD_STATUS_DOMAIN,
    )

    op.drop_constraint("ck_model_versions_status_domain", "model_versions", type_="check")
    op.create_check_constraint(
        "ck_model_versions_status_domain",
        "model_versions",
        _OLD_STATUS_DOMAIN,
    )

    op.drop_constraint("ck_models_current_status_domain", "models", type_="check")
    op.create_check_constraint(
        "ck_models_current_status_domain",
        "models",
        _OLD_STATUS_DOMAIN.replace("status IN", "current_status IN"),
    )
