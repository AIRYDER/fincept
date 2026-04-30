"""
scripts/wait_heartbeat.py - wait for a service's Redis heartbeat key.

Usage::

  uv run python scripts/wait_heartbeat.py <service_name> [--timeout 30]

Exits 0 once ``service:heartbeat:{name}`` is set, 1 on timeout.  Used
from ``scripts/start.ps1`` to gate progress until each spawned service
proves it's alive.

Why not poll the API's /services endpoint?  That requires a JWT and
the API may not be the first thing up.  Going straight to Redis is
auth-free, dependency-free, and unaffected by API state.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from redis.asyncio import Redis

from fincept_core.config import get_settings
from fincept_core.heartbeat import HEARTBEAT_PREFIX


async def wait(name: str, timeout_sec: float, poll_interval_sec: float = 0.5) -> int:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    key = f"{HEARTBEAT_PREFIX}{name}"
    try:
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while asyncio.get_event_loop().time() < deadline:
            value = await redis.get(key)
            if value is not None:
                return 0
            await asyncio.sleep(poll_interval_sec)
        return 1
    finally:
        await redis.aclose()  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(prog="wait_heartbeat")
    parser.add_argument("name", help="service name (e.g. orchestrator)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds to wait before giving up (default 30)",
    )
    args = parser.parse_args()
    return asyncio.run(wait(args.name, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
