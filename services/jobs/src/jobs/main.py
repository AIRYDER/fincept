"""
jobs.main — APScheduler entrypoint.

Cron-fires the registered jobs:

  - ``daily_eod_load.run_daily`` at 22:30 America/New_York Mon-Fri.
    22:30 ET is well after the 16:00 NYSE close + ~6 h of yfinance
    settlement lag.  Saturdays and Sundays are skipped at the cron
    expression (so the scheduler never even creates a coroutine on
    weekends), and US holidays fall through to ``run_daily``'s own
    no-op path (yfinance returns empty bars).

Run with: ``python -m jobs.main``.

The factory ``build_scheduler`` is exposed separately so tests can
verify the schedule expression without spinning up an event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from redis.asyncio import Redis

from fincept_core.config import get_settings
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing
from jobs.daily_eod_load import run_daily

log = get_logger(__name__)

EOD_CRON_TZ = "America/New_York"
EOD_CRON_HOUR = 22
EOD_CRON_MINUTE = 30
EOD_CRON_DAY_OF_WEEK = "mon-fri"


def build_scheduler() -> AsyncIOScheduler:
    """Return a configured (but not yet started) ``AsyncIOScheduler``."""
    scheduler = AsyncIOScheduler(timezone=EOD_CRON_TZ)
    scheduler.add_job(
        run_daily,
        trigger=CronTrigger(
            day_of_week=EOD_CRON_DAY_OF_WEEK,
            hour=EOD_CRON_HOUR,
            minute=EOD_CRON_MINUTE,
            timezone=EOD_CRON_TZ,
        ),
        id="daily_eod_load",
        name="EOD equity loader (yfinance → bars_1d)",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 h grace if the host was asleep at fire time
    )
    return scheduler


async def _run() -> None:
    configure_logging()
    configure_tracing("jobs.scheduler")
    settings = get_settings()
    scheduler = build_scheduler()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "jobs"))

    scheduler.start()
    log.info("scheduler.start", jobs=[job.id for job in scheduler.get_jobs()])
    try:
        await stop.wait()
    finally:
        log.info("scheduler.stop")
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        scheduler.shutdown(wait=False)
        await redis.aclose()  # type: ignore[attr-defined]


def main() -> None:
    """Synchronous CLI entrypoint: ``python -m jobs.main``."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
