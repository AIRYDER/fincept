"""fincept_tools.redis_client — lazy singleton Redis client for tools.

Follows the same pattern as ``fincept_db.engine.get_engine()``: a
process-global lazy singleton that creates the Redis client on first
access and reuses it for all subsequent calls. This eliminates the
per-call TCP connection setup/teardown overhead that the previous
``Redis.from_url()``-per-tool-invocation pattern had.

Usage::

    from fincept_tools.redis_client import get_redis

    r = get_redis()
    await r.xadd(STREAM_ORDERS, fields)

For tests, call ``await reset_redis()`` between tests to clear the
singleton, or patch ``get_redis`` directly.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from fincept_core.config import get_settings

_redis: Redis[Any] | None = None


def get_redis() -> Redis[Any]:
    """Return the lazy singleton Redis client.

    Creates the client on first call using ``settings.REDIS_URL``.
    Subsequent calls return the same instance. The client is never
    closed by callers — it persists for the process lifetime.
    """
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().REDIS_URL)
    return _redis


async def reset_redis() -> None:
    """Close and clear the singleton Redis client.

    Called between tests to ensure isolation. In production, the client
    persists for the process lifetime and is closed by the OS on exit.
    """
    global _redis
    if _redis is not None:
        await _redis.aclose()  # type: ignore[attr-defined]
        _redis = None
