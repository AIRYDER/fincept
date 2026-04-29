"""
api.routes.orders — order list endpoint backed by the audit log.

Orders aren't persisted to a dedicated table in v1; the OMS appends a
state row to ``audit_log`` for every transition (PENDING_NEW -> NEW ->
FILLED / REJECTED).  ``fincept_db.audit.list_recent_orders`` collapses
those rows to the latest state per ``order_id`` and returns the most
recent N (default 100, max 1000).

When order volume outgrows the audit log this will move to a dedicated
``orders`` table; for v1 one query against the indexed ``correlation_id``
column is plenty fast.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import require_user
from fincept_core.schemas import OrderStatus
from fincept_db.audit import list_recent_orders

router = APIRouter()


@router.get("")
async def list_orders(
    strategy_id: str | None = Query(None),
    status: OrderStatus | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return latest Order snapshot per order_id, newest first."""
    return await list_recent_orders(
        strategy_id=strategy_id,
        status=status.value if status is not None else None,
        limit=limit,
    )
