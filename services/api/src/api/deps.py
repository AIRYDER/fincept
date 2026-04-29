"""
api.deps — FastAPI dependency providers.

Centralised so tests can override via ``app.dependency_overrides``:

  - ``get_redis``    Lazily-constructed async Redis client.
  - ``get_position_store``  Wraps the Redis client in a PortfolioStore so
                            position routes don't touch Redis directly.

Both providers are async functions returning singletons cached on the
running event loop.  The ``app.state.redis`` slot is the canonical home
of the Redis client; ``main.lifespan`` populates it on startup and
closes it on shutdown.
"""

from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis

from portfolio.store import PositionStore


async def get_redis(request: Request) -> Redis:  # type: ignore[type-arg]
    redis: Redis | None = getattr(request.app.state, "redis", None)  # type: ignore[type-arg]
    if redis is None:
        raise RuntimeError(
            "redis client missing from app.state - did the lifespan run?"
        )
    return redis


async def get_position_store(request: Request) -> PositionStore:
    redis = await get_redis(request)
    return PositionStore(redis)
