"""
api.routes.strategies — strategy registry read endpoint.

Backed by Redis: a SET ``portfolio:strategies`` (populated by the
portfolio service as fills land for new strategies).  Each strategy in
the set has a corresponding hash ``positions:{strategy_id}`` we can
inspect for current state.

  GET /strategies                  List known strategies + position count.

start / stop endpoints are deferred — they require a strategy host
service (TASK-040 territory) that doesn't exist yet.  When that lands,
this module will gain ``POST /{strategy_id}/start|stop`` that RPC's to
the host.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.auth import require_user
from api.deps import get_position_store
from portfolio.store import PositionStore

router = APIRouter()


@router.get("")
async def list_strategies(
    _: dict[str, Any] = Depends(require_user),
    store: PositionStore = Depends(get_position_store),
) -> list[dict[str, Any]]:
    """Return ``[{strategy_id, position_count}, ...]`` sorted by id."""
    out: list[dict[str, Any]] = []
    for strategy_id in sorted(await store.known_strategies()):
        positions = await store.get_all(strategy_id)
        out.append(
            {
                "strategy_id": strategy_id,
                "position_count": len(positions),
                "open_positions": sum(1 for p in positions.values() if p.quantity != 0),
            }
        )
    return out
