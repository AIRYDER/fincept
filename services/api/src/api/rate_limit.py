"""
api.rate_limit — Redis-backed fixed-window rate limiter.

The limiter is intentionally small and dependency-free.  It uses an
``INCR`` + ``EXPIRE``-on-first counter per bucket key so callers only
pay two Redis round-trips in the steady state.

Why not a proper token bucket?  A fixed window is plenty for the
current needs (guarding the OpenBB dispatcher from runaway LLM
callers).  If per-second smoothing ever matters we can swap to a
Lua-script token bucket without changing the call site.

Example
-------

.. code-block:: python

    async def route(
        user: dict = Depends(require_user),
        redis: Redis = Depends(get_redis),
    ):
        state = await enforce_rate_limit(
            redis,
            f"rl:openbb:dispatch:{user['sub']}",
            limit=60,
            window_sec=60,
        )
        # state.remaining / state.reset_sec are useful for X-RateLimit headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis


class RateLimitExceeded(Exception):
    """Raised by :func:`enforce_rate_limit` when the bucket is empty.

    Carries ``retry_after`` (seconds) and the bucket metadata so the
    route handler can map it to a uniform JSON error body + HTTP 429
    response without re-querying Redis.
    """

    def __init__(self, *, limit: int, window_sec: int, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded: {limit} requests per {window_sec}s")
        self.limit = limit
        self.window_sec = window_sec
        self.retry_after = retry_after


@dataclass(frozen=True)
class RateLimitState:
    """Snapshot returned on a successful rate-limit check."""

    count: int
    limit: int
    remaining: int
    reset_sec: int


async def enforce_rate_limit(
    redis: Redis[Any],
    key: str,
    *,
    limit: int,
    window_sec: int,
) -> RateLimitState:
    """Increment the bucket and raise if the caller is over budget.

    ``key`` should be fully namespaced by the caller (e.g.
    ``rl:openbb:dispatch:{user_id}``) — this helper does not invent a
    prefix so different routes can share the same primitive without
    colliding.
    """
    count = int(await redis.incr(key))
    if count == 1:
        # First hit of the window: stamp the TTL.  Any subsequent
        # INCR inside the window preserves the original expiry, which
        # is exactly what a fixed-window limiter wants.
        await redis.expire(key, window_sec)

    ttl = int(await redis.ttl(key))
    if ttl < 0:
        # Missing TTL should not happen after the expire above, but if
        # the key was created without one (e.g. external writer), repair
        # it so callers don't get perma-rejected.
        await redis.expire(key, window_sec)
        ttl = window_sec

    if count > limit:
        raise RateLimitExceeded(
            limit=limit, window_sec=window_sec, retry_after=max(ttl, 1)
        )

    return RateLimitState(
        count=count,
        limit=limit,
        remaining=max(limit - count, 0),
        reset_sec=ttl,
    )
