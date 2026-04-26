from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from fincept_core.clock import now_ns
from fincept_core.events import Event, serialize
from fincept_core.ids import new_id

from .streams import RETENTION


class Producer:
    def __init__(self, redis: Redis[Any]) -> None:
        self.redis = redis

    async def publish(self, stream: str, event: Event) -> str:
        message_id = await self.redis.xadd(
            stream,
            serialize(event, event_id=new_id(), published_at=now_ns()),
            maxlen=RETENTION.get(stream),
            approximate=True,
        )
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)
