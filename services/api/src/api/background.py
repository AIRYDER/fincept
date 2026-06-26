"""
api.background — periodic tasks that run alongside FastAPI.

Two schedulers run in parallel:

  - ``AlpacaScheduler``  pulls positions + marks from /v2/positions
                         every ``interval_sec`` (default 60s).
  - ``NewsScheduler``    pulls /v1beta1/news + per-symbol 1-min bars
                         every ``interval_sec`` (default 30s) so the
                         dashboard news tab can render sparklines and
                         book-impact dollars with zero API calls on
                         the read path.

Both skip themselves when Alpaca credentials aren't configured, log
``.ok`` / ``.error`` events with counts, and honour CancelledError so
lifespan shutdown is fast.
"""

from __future__ import annotations

import asyncio
from typing import Any

from redis.asyncio import Redis

from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from oms.alpaca.news_sync import (
    refresh_snapshot_bars,
    sync_recent_news,
)
from oms.alpaca.sync_runner import sync_positions_and_marks

log = get_logger(__name__)


class AlpacaScheduler:
    """Periodic Alpaca → Redis sync task."""

    def __init__(
        self,
        redis: Redis[Any],
        *,
        interval_sec: int = 60,
    ) -> None:
        self._redis = redis
        self._interval = interval_sec
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        settings = get_settings()
        if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
            log.info("alpaca.scheduler.skip", reason="no_credentials")
            return
        self._task = asyncio.create_task(self._loop(), name="alpaca-sync")
        log.info("alpaca.scheduler.start", interval_sec=self._interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        log.info("alpaca.scheduler.stop")

    async def _loop(self) -> None:
        settings = get_settings()
        # Do an initial sync immediately so the dashboard has data on boot;
        # don't wait a full interval.
        while True:
            try:
                summary = await sync_positions_and_marks(
                    redis=self._redis,
                    api_key=settings.ALPACA_API_KEY or "",
                    api_secret=settings.ALPACA_API_SECRET or "",
                    base_url=settings.ALPACA_BASE_URL,
                )
                log.info("alpaca.sync.ok", **summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("alpaca.sync.error", error=str(exc))
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise


class NewsScheduler:
    """Periodic Alpaca → Redis news sync task.

    On every tick we do ONE of:

      - A full ``sync_recent_news`` (cheap if few new articles because
        existing ones are skipped by ID).
      - A ``refresh_snapshot_bars`` pass that extends the bar series
        on the 20 newest articles (so sparklines keep ticking).

    We interleave them 1:1 - that gives ~60s granularity for both the
    "new article" latency and the sparkline refresh rate at a 30s
    base interval, at the cost of one Alpaca call per tick.
    """

    def __init__(
        self,
        redis: Redis[Any],
        *,
        interval_sec: int = 30,
        lookback_minutes: int = 480,
        limit: int = 50,
    ) -> None:
        self._redis = redis
        self._interval = interval_sec
        self._lookback = lookback_minutes
        self._limit = limit
        self._task: asyncio.Task[None] | None = None
        self._tick = 0

    def start(self) -> None:
        settings = get_settings()
        if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
            log.info("news.scheduler.skip", reason="no_credentials")
            return
        self._task = asyncio.create_task(self._loop(), name="news-sync")
        log.info(
            "news.scheduler.start",
            interval_sec=self._interval,
            lookback_minutes=self._lookback,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        log.info("news.scheduler.stop")

    async def _loop(self) -> None:
        settings = get_settings()
        while True:
            try:
                if self._tick % 2 == 0:
                    summary = await sync_recent_news(
                        redis=self._redis,
                        api_key=settings.ALPACA_API_KEY or "",
                        api_secret=settings.ALPACA_API_SECRET or "",
                        lookback_minutes=self._lookback,
                        limit=self._limit,
                    )
                    log.info("news.sync.ok", **summary)
                else:
                    summary = await refresh_snapshot_bars(
                        redis=self._redis,
                        api_key=settings.ALPACA_API_KEY or "",
                        api_secret=settings.ALPACA_API_SECRET or "",
                    )
                    log.info("news.refresh.ok", **summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("news.sync.error", error=str(exc))
            self._tick += 1
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
