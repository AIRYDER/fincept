"""
fincept_core.heartbeat - lightweight Redis-backed liveness signal.

Each long-running service starts a background coroutine that writes
``service:heartbeat:{name}`` every ``interval_sec`` with a TTL of
``ttl_sec``.  If the service crashes, the key expires and the
dashboard / /services endpoint surfaces it as DOWN.

Usage in a service's ``main.run()``::

    from fincept_core.heartbeat import beat_periodically

    heartbeat_task = asyncio.create_task(beat_periodically(redis, "orchestrator"))
    try:
        ... usual run loop ...
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

This is intentionally simpler than a full health endpoint per service:
no HTTP port, no auth, no metrics - just "did this service write to
Redis in the last ``ttl_sec`` seconds".  The TTL is enforced server-side
so a crashed process that never gets to delete the key still expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from redis.asyncio import Redis

HEARTBEAT_PREFIX = "service:heartbeat:"
DEFAULT_INTERVAL_SEC = 10
DEFAULT_TTL_SEC = 30


def _key(name: str) -> str:
    return f"{HEARTBEAT_PREFIX}{name}"


async def beat_periodically(
    redis: Redis[Any],
    name: str,
    *,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> None:
    """Write a heartbeat key every ``interval_sec`` until cancelled.

    ``ttl_sec`` MUST exceed ``interval_sec`` (with margin) or the key
    will expire between writes and the service will appear DOWN.  A
    3x ratio is recommended so a single missed write tolerates one
    pause without flapping.
    """
    if ttl_sec <= interval_sec:
        raise ValueError(f"ttl_sec ({ttl_sec}) must be greater than interval_sec ({interval_sec})")
    key = _key(name)
    try:
        while True:
            await redis.set(key, str(time.time()), ex=ttl_sec)
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        # Best-effort cleanup so the dashboard immediately reflects
        # graceful shutdown.  Swallow errors - the TTL will catch it
        # if Redis is unreachable here.
        with contextlib.suppress(Exception):
            await redis.delete(key)
        raise


async def read_all(redis: Redis[Any]) -> dict[str, float]:
    """Return ``{service_name: last_beat_unix_time}`` for every live service.

    Services whose key has expired do not appear in the result.
    """
    out: dict[str, float] = {}
    async for raw_key in redis.scan_iter(match=f"{HEARTBEAT_PREFIX}*", count=100):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        name = key[len(HEARTBEAT_PREFIX) :]
        raw_val = await redis.get(raw_key)
        if raw_val is None:
            continue
        try:
            out[name] = float(raw_val.decode() if isinstance(raw_val, bytes) else raw_val)
        except ValueError:
            continue
    return out
    return out
