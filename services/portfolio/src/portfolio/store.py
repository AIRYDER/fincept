"""
portfolio.store — Redis-backed live position cache for fast UI reads.

Layout:

  HKEY    ``positions:{strategy_id}``
  HFIELD  ``{symbol}``
  HVALUE  ``Position.model_dump_json()``

A separate hash per strategy means the upcoming ``/strategies/{id}/positions``
endpoint is a single ``HGETALL`` — O(symbols-in-strategy), no scan, no DB.
The /positions aggregate endpoint will iterate strategy hashes via a
known-strategies set (kept in ``strategies`` index, populated as we
write).

The store is intentionally **flat** and **last-write-wins**.  No version
counters, no optimistic concurrency.  The OMS is the only writer to
fills, so the portfolio service is the only writer to positions —
there's no contention to resolve.  If a Fill arrives out of order
(``ts_event`` decreasing), the math in ``fincept_core.portfolio`` still
produces a correct cumulative position because it's path-independent
under associativity of the four-case logic *for fills processed in
order*.  Out-of-order fills are a TASK-074 concern.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from fincept_core.schemas import Position

POSITIONS_KEY_TEMPLATE = "positions:{strategy_id}"
STRATEGIES_INDEX = "portfolio:strategies"


class PositionStore:
    """Async Redis hash wrapper for live positions."""

    def __init__(self, redis: Redis[Any]) -> None:
        self._redis = redis

    @staticmethod
    def _key(strategy_id: str) -> str:
        return POSITIONS_KEY_TEMPLATE.format(strategy_id=strategy_id)

    async def put(self, position: Position) -> None:
        """Store *position* under ``positions:{strategy_id}`` keyed by symbol."""
        await self._redis.hset(
            self._key(position.strategy_id),
            position.symbol,
            position.model_dump_json(),
        )
        await self._redis.sadd(STRATEGIES_INDEX, position.strategy_id)

    async def get(self, strategy_id: str, symbol: str) -> Position | None:
        """Return the cached Position for ``(strategy_id, symbol)``, or None."""
        raw = await self._redis.hget(self._key(strategy_id), symbol)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return Position.model_validate_json(raw)

    async def get_all(self, strategy_id: str) -> dict[str, Position]:
        """All positions for ``strategy_id`` as ``{symbol: Position}``."""
        raw = await self._redis.hgetall(self._key(strategy_id))
        out: dict[str, Position] = {}
        for k, v in raw.items():
            sym = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            out[sym] = Position.model_validate_json(val)
        return out

    async def known_strategies(self) -> set[str]:
        """Return the set of strategy IDs that have ever recorded a position."""
        raw = await self._redis.smembers(STRATEGIES_INDEX)
        return {s.decode() if isinstance(s, bytes) else s for s in raw}
