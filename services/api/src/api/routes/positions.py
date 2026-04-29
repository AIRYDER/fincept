"""
api.routes.positions — live position read endpoints.

Backed by the Redis hash that the portfolio service populates
(``positions:{strategy_id}``).  Each call is one or more HGETALLs —
sub-millisecond, no DB.

  GET /positions                   All positions across all strategies.
  GET /positions/{strategy_id}     Positions for a single strategy.

Both filter to non-zero positions by default (UI doesn't want to show
flat closed positions); pass ``include_flat=true`` for the full set.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.auth import require_user
from api.deps import get_position_store
from portfolio.store import PositionStore

router = APIRouter()


@router.get("")
async def list_positions(
    include_flat: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
) -> list[dict[str, Any]]:
    """Return positions across all strategies in the system."""
    out: list[dict[str, Any]] = []
    for strategy_id in await store.known_strategies():
        positions = await store.get_all(strategy_id)
        for pos in positions.values():
            if not include_flat and pos.quantity == Decimal(0):
                continue
            out.append(pos.model_dump(mode="json"))
    return out


@router.get("/{strategy_id}")
async def list_strategy_positions(
    strategy_id: str,
    include_flat: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
) -> list[dict[str, Any]]:
    """Return positions for a single ``strategy_id``."""
    positions = await store.get_all(strategy_id)
    return [
        pos.model_dump(mode="json")
        for pos in positions.values()
        if include_flat or pos.quantity != Decimal(0)
    ]
