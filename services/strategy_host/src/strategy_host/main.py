"""
strategy_host.main — entrypoint for the live Strategy Host service.

Pipeline:

  StrategyConfigStore (filesystem) -> Supervisor.tick (every N sec)
       -> per-strategy run_strategy task -> OrderIntents -> ord.orders
                                          -> heartbeat: strategy_host

The host process is single-leader: only one instance per Redis
keyspace should run, otherwise duplicate orders fire.  Leadership
gating (``fincept_core.leadership``) is deferred -- start.ps1 spawns
exactly one strategy_host window, so the operational invariant
holds in dev.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.strategy_config import get_strategy_config_store
from fincept_core.tracing import configure_tracing
from strategy_host.runner import run_strategy
from strategy_host.supervisor import Supervisor

log = get_logger(__name__)

SERVICE_NAME = "strategy_host"


async def run(stop: asyncio.Event) -> None:
    """Connect Redis, start heartbeat + supervisor, await shutdown.

    Mirrors ``services/portfolio/main.run`` for consistency: one
    Redis client, two long-lived tasks, graceful cancel on the way
    down so the heartbeat key disappears quickly and the dashboard
    flips DOWN without waiting for TTL expiry.
    """
    settings = get_settings()
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    store = get_strategy_config_store()
    supervisor = Supervisor(store=store, redis=redis, runner=run_strategy)

    log.info(
        "strategy_host.start",
        configs_dir=str(store.configs_dir),
        configured=len(store.list_all()),
    )

    supervisor_task = asyncio.create_task(supervisor.run(stop), name="strategy_host:supervisor")
    heartbeat_task = asyncio.create_task(
        beat_periodically(redis, SERVICE_NAME),
        name="strategy_host:heartbeat",
    )

    try:
        await stop.wait()
    finally:
        for task in (heartbeat_task, supervisor_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await redis.aclose()  # type: ignore[attr-defined]


async def _main() -> None:
    configure_logging()
    configure_tracing(SERVICE_NAME)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await run(stop)
    finally:
        log.info("strategy_host.stop")


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m strategy_host.main``."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
