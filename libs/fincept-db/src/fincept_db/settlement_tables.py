"""SQLAlchemy 2.0 ORM models for settlement tables (migration 0008).

These models mirror the ``settlement_records`` table created by the
``0008_settlement_records`` Alembic migration. They follow the same
declarative style as ``fincept_db.callback_tables`` and
``fincept_db.registry_tables`` (``DeclarativeBase`` subclass, ``Mapped`` /
``mapped_column``, BigInteger for nanosecond timestamps).

The models are registered on the shared ``Base`` from ``fincept_db.models`` so
``Base.metadata.create_all`` (used by the test fixtures) creates them alongside
the existing tables. The migration is the source of truth for production
schemas; these models exist so the DB-backed settlement sink can use typed ORM
rows instead of raw SQL.

Design (see reports/c10-postgres-sink-flip/C10_POSTGRES_SINK_PREFLIGHT_DESIGN.md):
  - One row per ``(prediction_id, cost_model_version)`` pair.
  - ``settlement_id`` is the PK: ``f"{prediction_id}:{cost_model_version}"``.
  - ``UNIQUE (prediction_id, cost_model_version)`` for query clarity.
  - CHECK constraints for ``status`` and ``cost_model_version`` domains.
  - No secrets, no raw payloads — only settlement computation outputs.
  - No FK to ``models.model_id`` (settlement can arrive before model
    registration — soft FK only).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class SettlementRecordRow(Base):
    """ORM row for the ``settlement_records`` table (mirrors SettlementRecord).

    One row per settled (or pending) prediction. The fields mirror
    ``quant_foundry.outcomes.SettlementRecord`` — the canonical settlement
    record produced by ``SettlementLedger.settle()``.

    Idempotency: ``settlement_id`` is deterministic
    (``f"{prediction_id}:{cost_model_version}"``) and the unique constraint
    on ``(prediction_id, cost_model_version)`` ensures a replayed settlement
    does not create a second row.
    """

    __tablename__ = "settlement_records"

    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settlement_id: Mapped[str] = mapped_column(String(256), primary_key=True, nullable=False)
    prediction_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    horizon_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    settled_at_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    realized_return_gross: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    realized_return_net: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    abnormal_return: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    brier: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    calibration_bucket: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cost_model_version: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_window_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision_window_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_time','pending_data','settled')",
            name="ck_settlement_records_status_domain",
        ),
        CheckConstraint(
            "cost_model_version IN ('cm-v1','v1.default')",
            name="ck_settlement_records_cost_model_version_domain",
        ),
        CheckConstraint(
            "calibration_bucket IS NULL OR calibration_bucket IN "
            "('0.0-0.2','0.2-0.4','0.4-0.6','0.6-0.8','0.8-1.0')",
            name="ck_settlement_records_calibration_bucket_domain",
        ),
        UniqueConstraint(
            "prediction_id",
            "cost_model_version",
            name="uq_settlement_records_prediction_id_cost_model_version",
        ),
        Index("ix_settlement_records_model_id_ts", "model_id", "ts_event"),
        Index("ix_settlement_records_symbol_ts", "symbol", "ts_event"),
        Index("ix_settlement_records_status", "status"),
        Index("ix_settlement_records_prediction_id", "prediction_id"),
        Index("ix_settlement_records_cost_model_version", "cost_model_version"),
    )
