"""provider data capture table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-10 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "provider_data",
        sa.Column("record_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("dataset", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.String(256), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("ts_event", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("ts_observed", sa.BigInteger, nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("normalized", JSONB, nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_provider_data_provider_dataset_ts",
        "provider_data",
        ["provider", "dataset", "ts_event"],
    )
    op.create_index("ix_provider_data_symbol_ts", "provider_data", ["symbol", "ts_event"])
    op.create_index("ix_provider_data_request_hash", "provider_data", ["request_hash"])
    op.execute(
        "SELECT create_hypertable('provider_data', 'ts_event', "
        "chunk_time_interval => 86400000000000, if_not_exists => TRUE)"
    )
    op.execute(
        "ALTER TABLE provider_data SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'provider, dataset')"
    )
    op.execute("SELECT add_compression_policy('provider_data', INTERVAL '14 days')")


def downgrade() -> None:
    op.execute("SELECT remove_compression_policy('provider_data', if_exists => TRUE)")
    op.drop_index("ix_provider_data_request_hash", table_name="provider_data")
    op.drop_index("ix_provider_data_symbol_ts", table_name="provider_data")
    op.drop_index("ix_provider_data_provider_dataset_ts", table_name="provider_data")
    op.drop_table("provider_data")
