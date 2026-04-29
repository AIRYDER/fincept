from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from fincept_core.events import Event, deserialize

from .types import ConsumerGroupName, StreamID

RedisFields = Mapping[str | bytes, str | bytes]
Handler = Callable[[Event], Awaitable[None]]
ClaimedMessages = list[tuple[StreamID | bytes, RedisFields]]


class Consumer:
    def __init__(self, redis: Redis[Any]) -> None:
        self.redis = redis

    async def consume(
        self,
        streams: list[str],
        group: ConsumerGroupName,
        consumer_name: str,
        handler: Handler,
        *,
        block_ms: int = 1000,
        batch: int = 100,
        claim_idle_ms: int = 60_000,
    ) -> None:
        await self.ensure_groups(streams, group)
        stream_offsets = {stream: ">" for stream in streams}
        while True:
            await self._claim_stale(
                streams, group, consumer_name, handler, claim_idle_ms, batch, block_ms
            )
            response = await self.redis.xreadgroup(
                group,
                consumer_name,
                stream_offsets,
                count=batch,
                block=block_ms,
            )
            for stream_name, messages in response:
                stream = self._to_text(stream_name)
                for message_id, fields in messages:
                    await self._handle_message(stream, group, message_id, fields, handler, block_ms)

    async def ensure_groups(self, streams: Iterable[str], group: ConsumerGroupName) -> None:
        for stream in streams:
            await self.ensure_group(stream, group)

    async def ensure_group(self, stream: str, group: ConsumerGroupName) -> None:
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def claim_pending(
        self,
        stream: str,
        group: ConsumerGroupName,
        consumer_name: str,
        handler: Handler,
        *,
        min_idle_ms: int = 60_000,
        count: int = 100,
        block_ms: int = 1000,
    ) -> int:
        await self.ensure_group(stream, group)
        pending = await self.redis.xpending_range(stream, group, min="-", max="+", count=count)
        message_ids = [
            entry["message_id"]
            for entry in pending
            if int(entry["time_since_delivered"]) >= min_idle_ms
        ]
        if not message_ids:
            return 0
        claimed = await self._xclaim(stream, group, consumer_name, min_idle_ms, message_ids)
        handled = 0
        for message_id, fields in claimed:
            acked = await self._handle_message(stream, group, message_id, fields, handler, block_ms)
            if acked:
                handled += 1
        return handled

    async def _claim_stale(
        self,
        streams: Sequence[str],
        group: ConsumerGroupName,
        consumer_name: str,
        handler: Handler,
        min_idle_ms: int,
        count: int,
        block_ms: int,
    ) -> None:
        for stream in streams:
            await self.claim_pending(
                stream,
                group,
                consumer_name,
                handler,
                min_idle_ms=min_idle_ms,
                count=count,
                block_ms=block_ms,
            )

    async def _handle_message(
        self,
        stream: str,
        group: ConsumerGroupName,
        message_id: StreamID | bytes,
        fields: RedisFields,
        handler: Handler,
        block_ms: int,
    ) -> bool:
        event = deserialize(fields)
        started_ns = time.perf_counter_ns()
        try:
            await handler(event)
        except Exception:
            return False
        elapsed_ns = time.perf_counter_ns() - started_ns
        if elapsed_ns > block_ms * 1_000_000:
            raise TimeoutError("consumer handler exceeded block_ms")
        await self._xack(stream, group, message_id)
        return True

    async def _xclaim(
        self,
        stream: str,
        group: ConsumerGroupName,
        consumer_name: str,
        min_idle_ms: int,
        message_ids: list[StreamID | bytes],
    ) -> ClaimedMessages:
        redis = cast(Any, self.redis)
        claimed = await redis.xclaim(stream, group, consumer_name, min_idle_ms, message_ids)
        return cast(ClaimedMessages, claimed)

    async def _xack(
        self, stream: str, group: ConsumerGroupName, message_id: StreamID | bytes
    ) -> None:
        redis = cast(Any, self.redis)
        await redis.xack(stream, group, message_id)

    @staticmethod
    def _to_text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)
