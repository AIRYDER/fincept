"""initial schema with timescale hypertables

Revision ID: 0001
Revises:
Create Date: 2026-04-26 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "trades",
        sa.Column("venue", sa.String(32), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(32), primary_key=True, nullable=False),
        sa.Column("ts_event", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("seq", sa.BigInteger, primary_key=True, nullable=False, server_default="0"),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("ts_recv", sa.BigInteger, nullable=False),
        sa.Column("price", sa.Numeric(28, 12), nullable=False),
        sa.Column("size", sa.Numeric(28, 12), nullable=False),
        sa.Column("side", sa.String(4), nullable=True),
    )
    op.create_index("ix_trades_symbol_ts", "trades", ["symbol", "ts_event"])

    op.create_table(
        "bars",
        sa.Column("venue", sa.String(32), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(32), primary_key=True, nullable=False),
        sa.Column("freq", sa.String(8), primary_key=True, nullable=False),
        sa.Column("ts_event", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("open", sa.Numeric(28, 12), nullable=False),
        sa.Column("high", sa.Numeric(28, 12), nullable=False),
        sa.Column("low", sa.Numeric(28, 12), nullable=False),
        sa.Column("close", sa.Numeric(28, 12), nullable=False),
        sa.Column("volume", sa.Numeric(28, 12), nullable=False),
        sa.Column("trades", sa.Integer, nullable=False),
        sa.Column("vwap", sa.Numeric(28, 12), nullable=True),
    )
    op.create_index("ix_bars_symbol_freq_ts", "bars", ["symbol", "freq", "ts_event"])

    op.create_table(
        "book_deltas",
        sa.Column("venue", sa.String(32), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(32), primary_key=True, nullable=False),
        sa.Column("ts_event", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("seq", sa.BigInteger, primary_key=True, nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("ts_recv", sa.BigInteger, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index("ix_book_deltas_symbol_ts", "book_deltas", ["symbol", "ts_event"])

    op.create_table(
        "audit_log",
        sa.Column("event_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("ts_event", sa.BigInteger, nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index("ix_audit_corr", "audit_log", ["correlation_id"])
    op.create_index("ix_audit_ts", "audit_log", ["ts_event"])

    op.create_table(
        "strategies",
        sa.Column("strategy_id", sa.String(64), primary_key=True, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
    )

    op.create_table(
        "universe",
        sa.Column("symbol", sa.String(32), primary_key=True, nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("venue_default", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )

    op.execute(
        "SELECT create_hypertable('trades', 'ts_event', "
        "chunk_time_interval => 86400000000000, if_not_exists => TRUE)"
    )
    op.execute(
        "SELECT create_hypertable('bars', 'ts_event', "
        "chunk_time_interval => 86400000000000, if_not_exists => TRUE)"
    )
    op.execute(
        "SELECT create_hypertable('book_deltas', 'ts_event', "
        "chunk_time_interval => 3600000000000, if_not_exists => TRUE)"
    )

    op.execute(
        "ALTER TABLE trades SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'venue, symbol')"
    )
    op.execute(
        "ALTER TABLE bars SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'venue, symbol, freq')"
    )
    op.execute(
        "ALTER TABLE book_deltas SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'venue, symbol')"
    )

    op.execute("SELECT add_compression_policy('trades', INTERVAL '7 days')")
    op.execute("SELECT add_compression_policy('bars', INTERVAL '30 days')")
    op.execute("SELECT add_compression_policy('book_deltas', INTERVAL '1 day')")

    op.execute("SELECT add_retention_policy('trades', INTERVAL '30 days')")
    op.execute("SELECT add_retention_policy('book_deltas', INTERVAL '7 days')")


def downgrade() -> None:
    op.execute("SELECT remove_retention_policy('book_deltas', if_exists => TRUE)")
    op.execute("SELECT remove_retention_policy('trades', if_exists => TRUE)")
    op.execute("SELECT remove_compression_policy('book_deltas', if_exists => TRUE)")
    op.execute("SELECT remove_compression_policy('bars', if_exists => TRUE)")
    op.execute("SELECT remove_compression_policy('trades', if_exists => TRUE)")

    op.drop_table("universe")
    op.drop_table("strategies")
    op.drop_index("ix_audit_ts", table_name="audit_log")
    op.drop_index("ix_audit_corr", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_book_deltas_symbol_ts", table_name="book_deltas")
    op.drop_table("book_deltas")
    op.drop_index("ix_bars_symbol_freq_ts", table_name="bars")
    op.drop_table("bars")
    op.drop_index("ix_trades_symbol_ts", table_name="trades")
    op.drop_table("trades")
