"""
api.routes.data — read-only market-data endpoints.

  GET /universe                List active universe symbols (filtered by
                               asset_class if requested).
  GET /bars/{symbol}           Historical OHLCV bars.  ``freq`` defaults
                               to "1m"; ``start``/``end`` are required
                               and use UTC nanoseconds.

Both delegate to fincept-db readers; the API layer adds auth + light
parameter validation + JSON serialisation only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_user
from fincept_db.bars import read_bars
from fincept_db.universe import read_universe

router = APIRouter()


@router.get("/universe")
async def list_universe(
    asset_class: str | None = Query(None),
    active_only: bool = Query(True),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return universe rows; optionally filter by asset_class."""
    return await read_universe(asset_class=asset_class, active_only=active_only)


@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str,
    start: int = Query(
        ..., description="Start of range in UTC nanoseconds (inclusive)"
    ),
    end: int = Query(..., description="End of range in UTC nanoseconds (exclusive)"),
    freq: str = Query("1m", description="Bar frequency: 1m | 1h | 1d"),
    venue: str | None = Query(None, description="Optional venue filter"),
    _: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """Return bars in ``[start, end)`` for ``symbol`` at ``freq``."""
    if start >= end:
        raise HTTPException(status_code=400, detail="start must be < end")
    bars = await read_bars(symbol, freq, start, end, venue=venue)
    # Pydantic dump for JSON-friendly Decimal handling.
    return [bar.model_dump(mode="json") for bar in bars]
