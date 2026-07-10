"""
api.deps — FastAPI dependency providers.

Centralised so tests can override via ``app.dependency_overrides``:

  - ``get_redis``                 Lazily-constructed async Redis client.
  - ``get_position_store``        Wraps Redis in a PortfolioStore so
                                  position routes don't touch Redis
                                  directly.
  - ``get_strategy_config_store`` Wraps the filesystem-backed
                                  StrategyConfigStore (Phase F).
                                  Tests override the configs_dir.

All providers are functions that resolve a request-scoped or
process-singleton resource.  The ``app.state.redis`` slot is the
canonical home of the Redis client; ``main.lifespan`` populates it
on startup and closes it on shutdown.
"""

from __future__ import annotations

from fastapi import Request
from fincept_core.strategy_config import (
    StrategyConfigStore,
)
from fincept_core.strategy_config import (
    get_strategy_config_store as _get_strategy_config_store_singleton,
)
from portfolio.store import PositionStore
from redis.asyncio import Redis


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


def get_strategy_config_store() -> StrategyConfigStore:
    """Filesystem-backed strategy config store.

    Returns the process-wide singleton so all routes share the same
    on-disk state.  Tests can override via
    ``app.dependency_overrides[get_strategy_config_store]`` to point
    at a tmp_path-backed store, or by calling
    ``fincept_core.strategy_config.reset_strategy_config_store`` to
    rebind the singleton against a fresh dir.
    """
    return _get_strategy_config_store_singleton()
