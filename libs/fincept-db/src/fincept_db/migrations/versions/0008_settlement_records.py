"""settlement records table (C10)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-10 00:00:00.000000

C10 adds the ``settlement_records`` table — the durable, Postgres-backed
home for canonical settlement records (one row per settled prediction).

This is the one new table C10 needs. All other critical records (dossiers,
callback receipts, artifact manifests, shadow predictions, promotion
decisions, model metrics, dataset manifests) already have Postgres tables
from migrations 0004-0007.

Design (see reports/c10-postgres-sink-flip/C10_POSTGRES_SINK_PREFLIGHT_DESIGN.md):
  - One row per ``(prediction_id, cost_model_version)`` pair — the same
    idempotency key as ``SettlementLedger._find()``.
  - ``settlement_id`` is the PK: ``f"{prediction_id}:{cost_model_version}"``.
  - ``UNIQUE (prediction_id, cost_model_version)`` for query clarity.
  - BigInteger for nanosecond timestamps (``ts_event``, ``horizon_ns``,
    ``settled_at_ns``, ``decision_window_start``, ``decision_window_end``,
    ``created_at_ns``).
  - Numeric(28,12) for return/Brier fields (matches shadow_predictions
    precision for direction/confidence).
  - CHECK constraints for ``status`` and ``cost_model_version`` domains.
  - No secrets, no raw payloads — only settlement computation outputs.
  - No FK to ``models.model_id`` (settlement can arrive before model
    registration — soft FK only).

Downgrade: ``DROP TABLE settlement_records``. Additive only — no existing
table is modified.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_records",
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("settlement_id", sa.String(256), primary_key=True, nullable=False),
        sa.Column("prediction_id", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("ts_event", sa.BigInteger, nullable=False),
        sa.Column("horizon_ns", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("settled_at_ns", sa.BigInteger, nullable=True),
        sa.Column("realized_return_gross", sa.Numeric(28, 12), nullable=True),
        sa.Column("realized_return_net", sa.Numeric(28, 12), nullable=True),
        sa.Column("abnormal_return", sa.Numeric(28, 12), nullable=True),
        sa.Column("brier", sa.Numeric(28, 12), nullable=True),
        sa.Column("calibration_bucket", sa.String(16), nullable=True),
        sa.Column("cost_model_version", sa.String(16), nullable=False),
        sa.Column("decision_window_start", sa.BigInteger, nullable=False),
        sa.Column("decision_window_end", sa.BigInteger, nullable=False),
        sa.Column("created_at_ns", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_time','pending_data','settled')",
            name="ck_settlement_records_status_domain",
        ),
        sa.CheckConstraint(
            "cost_model_version IN ('cm-v1','v1.default')",
            name="ck_settlement_records_cost_model_version_domain",
        ),
        sa.CheckConstraint(
            "calibration_bucket IS NULL OR calibration_bucket IN "
            "('0.0-0.2','0.2-0.4','0.4-0.6','0.6-0.8','0.8-1.0')",
            name="ck_settlement_records_calibration_bucket_domain",
        ),
        sa.UniqueConstraint(
            "prediction_id",
            "cost_model_version",
            name="uq_settlement_records_prediction_id_cost_model_version",
        ),
    )
    op.create_index(
        "ix_settlement_records_model_id_ts",
        "settlement_records",
        ["model_id", "ts_event"],
    )
    op.create_index(
        "ix_settlement_records_symbol_ts",
        "settlement_records",
        ["symbol", "ts_event"],
    )
    op.create_index(
        "ix_settlement_records_status",
        "settlement_records",
        ["status"],
    )
    op.create_index(
        "ix_settlement_records_prediction_id",
        "settlement_records",
        ["prediction_id"],
    )
    op.create_index(
        "ix_settlement_records_cost_model_version",
        "settlement_records",
        ["cost_model_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_settlement_records_cost_model_version", table_name="settlement_records"
    )
    op.drop_index(
        "ix_settlement_records_prediction_id", table_name="settlement_records"
    )
    op.drop_index("ix_settlement_records_status", table_name="settlement_records")
    op.drop_index(
        "ix_settlement_records_symbol_ts", table_name="settlement_records"
    )
    op.drop_index(
        "ix_settlement_records_model_id_ts", table_name="settlement_records"
    )
    op.drop_table("settlement_records")
