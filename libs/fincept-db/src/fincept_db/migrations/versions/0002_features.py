"""features hypertable

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "features",
        sa.Column("symbol", sa.String(32), primary_key=True, nullable=False),
        sa.Column("freq", sa.String(8), primary_key=True, nullable=False),
        sa.Column("ts_event", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("values", JSONB, nullable=False),
        sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_features_sym_freq_ts", "features", ["symbol", "freq", "ts_event"])

    # Hypertable + compression mirrors the bars table strategy: 1-day chunks
    # are right-sized for ~360k rows/day (50 features x 5 symbols x 1440 1m
    # bars).  Compression after 14 days bounds storage; retention is left
    # unbounded for now since features are the input to backtests + training.
    op.execute(
        "SELECT create_hypertable('features', 'ts_event', "
        "chunk_time_interval => 86400000000000, if_not_exists => TRUE)"
    )
    op.execute(
        "ALTER TABLE features SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, freq')"
    )
    op.execute("SELECT add_compression_policy('features', 1209600000000000)")


def downgrade() -> None:
    op.execute("SELECT remove_compression_policy('features', if_exists => TRUE)")
    op.drop_index("ix_features_sym_freq_ts", table_name="features")
    op.drop_table("features")
