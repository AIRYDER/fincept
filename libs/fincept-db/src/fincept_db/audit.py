from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fincept_core.clock import now_ns
from fincept_core.ids import new_id

from .engine import session_scope
from .models import AuditLog


async def append(
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> str:
    event_id: str = new_id()
    async with session_scope() as session:
        stmt = (
            pg_insert(AuditLog)
            .values(
                event_id=event_id,
                ts_event=now_ns(),
                actor=actor,
                event_type=event_type,
                correlation_id=correlation_id,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        await session.execute(stmt)
    return event_id


async def read_by_correlation(correlation_id: str) -> list[dict[str, Any]]:
    async with session_scope() as session:
        query = (
            select(AuditLog)
            .where(AuditLog.correlation_id == correlation_id)
            .order_by(AuditLog.ts_event)
        )
        rows = (await session.execute(query)).scalars().all()
        return [
            {
                "event_id": row.event_id,
                "ts_event": row.ts_event,
                "actor": row.actor,
                "event_type": row.event_type,
                "correlation_id": row.correlation_id,
                "payload": row.payload,
            }
            for row in rows
        ]
