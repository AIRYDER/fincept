"""
api.routes.positions — live position read endpoints.

Backed by the Redis hash that the portfolio service populates
(``positions:{strategy_id}``).  Each call is one or more HGETALLs —
sub-millisecond, no DB.

  GET /positions                   All positions across all strategies.
  GET /positions/{strategy_id}     Positions for a single strategy.

Both filter to non-zero positions by default (UI doesn't want to show
flat closed positions); pass ``include_flat=true`` for the full set.

Responses are enriched with a live ``mark_px`` field from
``md:last:{symbol}`` when one is available, so dashboards can render
market value and real unrealized P&L without issuing a second call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from oms.alpaca.marks import read_marks
from portfolio.store import PositionStore
from redis.asyncio import Redis

from api.auth import require_user
from api.deps import get_position_store, get_redis

router = APIRouter()


async def _enrich_with_marks(
    redis: Redis,  # type: ignore[type-arg]
    positions: list[Any],
) -> list[dict[str, Any]]:
    """Attach ``mark_px`` to each position dict when Redis has a mark."""
    symbols = sorted({pos.symbol for pos in positions})
    marks = await read_marks(redis, symbols)
    out: list[dict[str, Any]] = []
    for pos in positions:
        data = pos.model_dump(mode="json")
        if pos.symbol in marks:
            data["mark_px"] = str(marks[pos.symbol])
        out.append(data)
    return out


@router.get("")
async def list_positions(
    include_flat: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> list[dict[str, Any]]:
    """Return positions across all strategies in the system."""
    collected: list[Any] = []
    for strategy_id in await store.known_strategies():
        positions = await store.get_all(strategy_id)
        for pos in positions.values():
            if not include_flat and pos.quantity == Decimal(0):
                continue
            collected.append(pos)
    return await _enrich_with_marks(redis, collected)


@router.get("/{strategy_id}")
async def list_strategy_positions(
    strategy_id: str,
    include_flat: bool = Query(False),
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
    redis: Redis = Depends(get_redis),  # type: ignore[type-arg]
) -> list[dict[str, Any]]:
    """Return positions for a single ``strategy_id``."""
    positions = await store.get_all(strategy_id)
    collected = [
        pos for pos in positions.values() if include_flat or pos.quantity != Decimal(0)
    ]
    return await _enrich_with_marks(redis, collected)
