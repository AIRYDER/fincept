from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from .ids import new_id
from .logging import get_logger

log = get_logger(__name__)


class Leader:
    def __init__(self, redis: Redis[Any], role: str, ttl_seconds: int = 15) -> None:
        self.redis = redis
        self.key = f"leader:{role}"
        self.ttl = ttl_seconds
        self.token = new_id()
        self._task: Any = None
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._is_leader:
            await self._release()
            self._is_leader = False

    async def _release(self) -> None:
        lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
        redis_client: Any = self.redis
        await redis_client.eval(lua, 1, self.key, self.token)

    async def _renew(self) -> bool:
        lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
        redis_client: Any = self.redis
        ok = await redis_client.eval(lua, 1, self.key, self.token, self.ttl * 1000)
        return bool(ok)

    async def _acquire(self) -> bool:
        redis_client: Any = self.redis
        got = await redis_client.set(self.key, self.token, nx=True, ex=self.ttl)
        return bool(got)

    async def _step(self) -> None:
        if self._is_leader:
            if not await self._renew():
                log.warning("leader.lost", role=self.key)
                self._is_leader = False
        elif await self._acquire():
            self._is_leader = True
            log.info("leader.acquired", role=self.key)

    async def _loop(self) -> None:
        try:
            while True:
                await self._step()
                await asyncio.sleep(self.ttl / 3)
        except asyncio.CancelledError:
            raise
