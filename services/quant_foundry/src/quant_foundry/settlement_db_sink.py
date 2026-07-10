"""quant_foundry.settlement_db_sink — DB-backed settlement record store.

Postgres-backed implementation of the settlement record persistence protocol.
Mirrors ``SettlementLedger._append()`` (JSONL) but writes to the
``settlement_records`` table (migration 0008) via a **sync** SQLAlchemy engine.

This is the C10 skeleton — the repository methods exist and are tested, but
they are NOT wired into production execution yet. The feature flags in
``quant_foundry.c10_flags`` control when the production ``SettlementLedger``
starts dual-writing to Postgres.

Why sync, not async:
  Same rationale as ``db_sinks.py`` — the settlement worker is sync, and
  adding a second connection pool is acceptable for the first cut.

Idempotency:
  ``INSERT ... ON CONFLICT (settlement_id) DO NOTHING`` so a replayed
  settlement does not create a second row. The ``settlement_id`` is
  deterministic: ``f"{prediction_id}:{cost_model_version}"``.

Security:
  No secrets, no raw payloads — only settlement computation outputs
  (returns, Brier score, calibration bucket, cost model version).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from fincept_db.settlement_tables import SettlementRecordRow
from quant_foundry.outcomes import SettlementRecord, SettlementStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dialect_insert(engine: Engine) -> Callable[..., Any]:
    """Return the dialect-specific insert() for the engine."""
    name = engine.dialect.name
    if name == "sqlite":
        return sqlite_insert
    return pg_insert


def _settlement_id(prediction_id: str, cost_model_version: str) -> str:
    """Build the deterministic settlement_id.

    ``f"{prediction_id}:{cost_model_version}"`` — the same idempotency key
    as ``SettlementLedger._find()``.
    """
    return f"{prediction_id}:{cost_model_version}"


def _record_to_values(record: SettlementRecord, now_ns: int) -> dict[str, Any]:
    """Convert a SettlementRecord to a dict of column-name -> value."""
    return {
        "schema_version": 1,
        "settlement_id": _settlement_id(record.prediction_id, record.cost_model_version),
        "prediction_id": record.prediction_id,
        "model_id": record.model_id,
        "symbol": record.symbol,
        "ts_event": record.ts_event,
        "horizon_ns": record.horizon_ns,
        "status": record.status.value,
        "settled_at_ns": record.settled_at_ns,
        "realized_return_gross": (
            Decimal(str(record.realized_return_gross))
            if record.realized_return_gross is not None
            else None
        ),
        "realized_return_net": (
            Decimal(str(record.realized_return_net))
            if record.realized_return_net is not None
            else None
        ),
        "abnormal_return": (
            Decimal(str(record.abnormal_return)) if record.abnormal_return is not None else None
        ),
        "brier": (Decimal(str(record.brier)) if record.brier is not None else None),
        "calibration_bucket": record.calibration_bucket,
        "cost_model_version": record.cost_model_version,
        "decision_window_start": record.decision_window_start,
        "decision_window_end": record.decision_window_end,
        "created_at_ns": now_ns,
    }


def _row_to_record(row: SettlementRecordRow) -> SettlementRecord:
    """Convert a SettlementRecordRow back to a SettlementRecord."""
    return SettlementRecord(
        prediction_id=row.prediction_id,
        model_id=row.model_id,
        symbol=row.symbol,
        ts_event=row.ts_event,
        horizon_ns=row.horizon_ns,
        status=SettlementStatus(row.status),
        settled_at_ns=row.settled_at_ns,
        realized_return_gross=(
            float(row.realized_return_gross) if row.realized_return_gross is not None else None
        ),
        realized_return_net=(
            float(row.realized_return_net) if row.realized_return_net is not None else None
        ),
        abnormal_return=(float(row.abnormal_return) if row.abnormal_return is not None else None),
        brier=float(row.brier) if row.brier is not None else None,
        calibration_bucket=row.calibration_bucket,
        cost_model_version=row.cost_model_version,
        decision_window_start=row.decision_window_start,
        decision_window_end=row.decision_window_end,
    )


# ---------------------------------------------------------------------------
# DbSettlementStore
# ---------------------------------------------------------------------------


class DbSettlementStore:
    """DB-backed settlement record store.

    Writes settlement records to the ``settlement_records`` table (migration
    0008) using a sync SQLAlchemy engine. All writes are idempotent via
    ``ON CONFLICT (settlement_id) DO NOTHING``.

    This is the C10 skeleton — the methods exist and are tested, but they are
    NOT wired into production execution yet. The feature flags in
    ``quant_foundry.c10_flags`` control when the production
    ``SettlementLedger`` starts dual-writing to Postgres.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Return the engine (lazy-init from get_sync_engine if not injected)."""
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, record: SettlementRecord, *, now_ns: int | None = None) -> bool:
        """Write a settlement record to Postgres. Idempotent.

        Returns True if a new row was inserted, False if the record already
        existed (idempotent replay).
        """
        ts = now_ns if now_ns is not None else time.time_ns()
        values = _record_to_values(record, ts)
        insert_fn = _dialect_insert(self.engine)
        stmt = insert_fn(SettlementRecordRow).values(**values)
        stmt = stmt.on_conflict_do_nothing(index_elements=["settlement_id"])
        with Session(self.engine) as session:
            result = session.execute(stmt)
            session.commit()
            # CursorResult.rowcount is 1 for a new insert, 0 for ON CONFLICT DO NOTHING.
            rowcount: int = int(result.rowcount)  # type: ignore[attr-defined,unused-ignore]
            return rowcount > 0

    def write_batch(self, records: Sequence[SettlementRecord], *, now_ns: int | None = None) -> int:
        """Write a batch of settlement records. Idempotent.

        Returns the number of new rows inserted (existing records are no-ops).
        """
        ts = now_ns if now_ns is not None else time.time_ns()
        inserted = 0
        for record in records:
            if self.write(record, now_ns=ts):
                inserted += 1
        return inserted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, prediction_id: str, cost_model_version: str) -> SettlementRecord | None:
        """Return the settlement record for (prediction_id, cost_model_version)."""
        sid = _settlement_id(prediction_id, cost_model_version)
        with Session(self.engine) as session:
            row = session.get(SettlementRecordRow, sid)
            if row is None:
                return None
            return _row_to_record(row)

    def list_for_model(
        self,
        model_id: str,
        *,
        limit: int = 1000,
        status: str | None = None,
    ) -> list[SettlementRecord]:
        """List settlement records for a model, newest-first by settled_at_ns."""
        with Session(self.engine) as session:
            stmt = select(SettlementRecordRow).where(SettlementRecordRow.model_id == model_id)
            if status is not None:
                stmt = stmt.where(SettlementRecordRow.status == status)
            stmt = stmt.order_by(
                SettlementRecordRow.settled_at_ns.desc().nullslast(),
                SettlementRecordRow.ts_event.desc(),
            ).limit(limit)
            rows = session.scalars(stmt).all()
            return [_row_to_record(r) for r in rows]

    def list_all(
        self,
        *,
        limit: int = 10000,
        status: str | None = None,
    ) -> list[SettlementRecord]:
        """List all settlement records, newest-first by settled_at_ns."""
        with Session(self.engine) as session:
            stmt = select(SettlementRecordRow)
            if status is not None:
                stmt = stmt.where(SettlementRecordRow.status == status)
            stmt = stmt.order_by(
                SettlementRecordRow.settled_at_ns.desc().nullslast(),
                SettlementRecordRow.ts_event.desc(),
            ).limit(limit)
            rows = session.scalars(stmt).all()
            return [_row_to_record(r) for r in rows]

    def count(self, *, model_id: str | None = None, status: str | None = None) -> int:
        """Count settlement records, optionally filtered by model_id and/or status."""
        from sqlalchemy import func

        with Session(self.engine) as session:
            stmt = select(func.count()).select_from(SettlementRecordRow)
            if model_id is not None:
                stmt = stmt.where(SettlementRecordRow.model_id == model_id)
            if status is not None:
                stmt = stmt.where(SettlementRecordRow.status == status)
            return int(session.scalar(stmt) or 0)
