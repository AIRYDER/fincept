from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=0)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_recv: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    size: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    side: Mapped[str | None] = mapped_column(String(4), nullable=True)

    __table_args__ = (Index("ix_trades_symbol_ts", "symbol", "ts_event"),)


class Bar(Base):
    __tablename__ = "bars"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    freq: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    trades: Mapped[int] = mapped_column(Integer, nullable=False)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)

    __table_args__ = (Index("ix_bars_symbol_freq_ts", "symbol", "freq", "ts_event"),)


class BookDelta(Base):
    __tablename__ = "book_deltas"

    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_recv: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_book_deltas_symbol_ts", "symbol", "ts_event"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_audit_corr", "correlation_id"),
        Index("ix_audit_ts", "ts_event"),
    )


class Strategy(Base):
    __tablename__ = "strategies"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class UniverseSymbol(Base):
    __tablename__ = "universe"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    venue_default: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
