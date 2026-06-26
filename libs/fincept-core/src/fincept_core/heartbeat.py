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

Stats: an optional ``stats_callback`` can be passed to ``beat_periodically``
to include service-specific metrics (buffer sizes, drop counts, etc.) in
the heartbeat value.  The callback is called on each beat and should
return a JSON-serializable dict.  The value is stored as JSON::

    {"ts": 1234567890.123, "stats": {...}}

When no callback is provided, the value is just the timestamp string
(backward compatible with existing dashboards).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis

HEARTBEAT_PREFIX = "service:heartbeat:"
DEFAULT_INTERVAL_SEC = 10
DEFAULT_TTL_SEC = 30

StatsCallback = Callable[[], dict[str, Any]]


def _key(name: str) -> str:
    return f"{HEARTBEAT_PREFIX}{name}"


async def beat_periodically(
    redis: Redis[Any],
    name: str,
    *,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    ttl_sec: int = DEFAULT_TTL_SEC,
    stats_callback: StatsCallback | None = None,
) -> None:
    """Write a heartbeat key every ``interval_sec`` until cancelled.

    ``ttl_sec`` MUST exceed ``interval_sec`` (with margin) or the key
    will expire between writes and the service will appear DOWN.  A
    3x ratio is recommended so a single missed write tolerates one
    pause without flapping.

    ``stats_callback``: if provided, called on each beat to collect
    service-specific metrics.  The return value is included in the
    heartbeat value as JSON.  Exceptions from the callback are logged
    and swallowed — heartbeat liveness must never fail due to stats
    collection errors.
    """
    if ttl_sec <= interval_sec:
        raise ValueError(f"ttl_sec ({ttl_sec}) must be greater than interval_sec ({interval_sec})")
    key = _key(name)
    try:
        while True:
            ts = time.time()
            if stats_callback is not None:
                try:
                    stats = stats_callback()
                    value = json.dumps({"ts": ts, "stats": stats})
                except Exception:
                    # Stats collection failure must not break heartbeat.
                    value = str(ts)
            else:
                value = str(ts)
            await redis.set(key, value, ex=ttl_sec)
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
        val_str = raw_val.decode() if isinstance(raw_val, bytes) else raw_val
        try:
            # Try parsing as JSON (new format with stats).
            parsed = json.loads(val_str)
            if isinstance(parsed, dict) and "ts" in parsed:
                out[name] = float(parsed["ts"])
            else:
                out[name] = float(val_str)
        except (ValueError, TypeError):
            try:
                out[name] = float(val_str)
            except ValueError:
                continue
    return out


async def read_all_with_stats(redis: Redis[Any]) -> dict[str, dict[str, Any]]:
    """Return ``{service_name: {"ts": float, "stats": dict|None}}`` for every
    live service.  Services without stats_callback have ``stats=None``.
    """
    out: dict[str, dict[str, Any]] = {}
    async for raw_key in redis.scan_iter(match=f"{HEARTBEAT_PREFIX}*", count=100):
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        name = key[len(HEARTBEAT_PREFIX) :]
        raw_val = await redis.get(raw_key)
        if raw_val is None:
            continue
        val_str = raw_val.decode() if isinstance(raw_val, bytes) else raw_val
        try:
            parsed = json.loads(val_str)
            if isinstance(parsed, dict) and "ts" in parsed:
                out[name] = {"ts": float(parsed["ts"]), "stats": parsed.get("stats")}
            else:
                out[name] = {"ts": float(val_str), "stats": None}
        except (ValueError, TypeError):
            try:
                out[name] = {"ts": float(val_str), "stats": None}
            except ValueError:
                continue
    return out
