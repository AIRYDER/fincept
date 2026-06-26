from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from fincept_core.clock import now_ns
from fincept_core.events import Event, deserialize
from fincept_core.logging import get_logger

from .types import ConsumerGroupName, StreamID

log = get_logger(__name__)

RedisFields = Mapping[str | bytes, str | bytes]
Handler = Callable[[Event], Awaitable[None]]
ClaimedMessages = list[tuple[StreamID | bytes, RedisFields]]

# Default max delivery attempts before a message is moved to the DLQ.
# A poison message that fails 5 times is almost certainly a schema mismatch
# or handler bug — retrying forever would block the consumer group.
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5

# DLQ stream naming convention: append ".dlq" to the source stream.
DLQ_SUFFIX = ".dlq"

# DLQ retention: keep last 100k failed messages per stream.
DLQ_RETENTION = 100_000


def _dlq_stream(stream: str) -> str:
    """Return the DLQ stream name for a given source stream."""
    return f"{stream}{DLQ_SUFFIX}"


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
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> None:
        await self.ensure_groups(streams, group)
        stream_offsets = {stream: ">" for stream in streams}
        while True:
            await self._claim_stale(
                streams,
                group,
                consumer_name,
                handler,
                claim_idle_ms,
                batch,
                block_ms,
                max_delivery_attempts,
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
                    await self._handle_message(
                        stream,
                        group,
                        message_id,
                        fields,
                        handler,
                        block_ms,
                        max_delivery_attempts,
                    )

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
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> int:
        await self.ensure_group(stream, group)
        pending = await self.redis.xpending_range(stream, group, min="-", max="+", count=count)
        message_ids: list[Any] = []
        for entry in pending:
            if int(entry["time_since_delivered"]) < min_idle_ms:
                continue
            # Check delivery count — if exceeded, move to DLQ instead of retrying.
            times_delivered = int(entry.get("times_delivered", 1))
            if times_delivered >= max_delivery_attempts:
                await self._move_to_dlq(
                    stream,
                    group,
                    entry["message_id"],
                    times_delivered,
                    "max_delivery_attempts_exceeded",
                )
                continue
            message_ids.append(entry["message_id"])
        if not message_ids:
            return 0
        claimed = await self._xclaim(stream, group, consumer_name, min_idle_ms, message_ids)
        handled = 0
        for message_id, fields in claimed:
            acked = await self._handle_message(
                stream,
                group,
                message_id,
                fields,
                handler,
                block_ms,
                max_delivery_attempts,
            )
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
        max_delivery_attempts: int,
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
                max_delivery_attempts=max_delivery_attempts,
            )

    async def _handle_message(
        self,
        stream: str,
        group: ConsumerGroupName,
        message_id: StreamID | bytes,
        fields: RedisFields,
        handler: Handler,
        block_ms: int,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> bool:
        event = deserialize(fields)
        started_ns = time.perf_counter_ns()
        try:
            await handler(event)
        except Exception as exc:
            # Log the failure (previously silent).
            log.warning(
                "consumer.handler_failed",
                stream=stream,
                message_id=self._to_text(message_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            # Check delivery count to decide DLQ vs retry.
            delivery_count = await self._get_delivery_count(stream, group, message_id)
            if delivery_count >= max_delivery_attempts:
                await self._move_to_dlq(
                    stream,
                    group,
                    message_id,
                    delivery_count,
                    f"{type(exc).__name__}: {exc}",
                )
                return True  # Acked via DLQ move — message is handled.
            return False
        elapsed_ns = time.perf_counter_ns() - started_ns
        if elapsed_ns > block_ms * 1_000_000:
            raise TimeoutError("consumer handler exceeded block_ms")
        await self._xack(stream, group, message_id)
        return True

    async def _get_delivery_count(
        self,
        stream: str,
        group: ConsumerGroupName,
        message_id: StreamID | bytes,
    ) -> int:
        """Get the number of times a message has been delivered.

        Returns 1 if the message is not found in the PEL (e.g. already
        acked by a concurrent consumer).
        """
        try:
            pending = await self.redis.xpending_range(
                stream, group, min=message_id, max=message_id, count=1
            )
            if pending:
                return int(pending[0].get("times_delivered", 1))
        except Exception:
            pass
        return 1

    async def _move_to_dlq(
        self,
        stream: str,
        group: ConsumerGroupName,
        message_id: StreamID | bytes,
        times_delivered: int,
        error_reason: str,
    ) -> None:
        """Move a poison message to the DLQ stream and ack the original.

        The DLQ entry contains:
        - original_stream: source stream name
        - original_message_id: the message ID from the source stream
        - error_reason: the exception that caused the failure
        - times_delivered: how many times the handler was called
        - moved_at_ns: timestamp of the DLQ move
        - fields: the original message fields (JSON-encoded)
        """
        dlq = _dlq_stream(stream)
        # Serialize the original fields for the DLQ entry.
        import json

        decoded_fields = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in fields.items()
        }
        dlq_entry = {
            "original_stream": stream,
            "original_message_id": self._to_text(message_id),
            "error_reason": error_reason[:500],  # Truncate long errors
            "times_delivered": str(times_delivered),
            "moved_at_ns": str(now_ns()),
            "fields": json.dumps(decoded_fields, sort_keys=True),
        }
        try:
            await self.redis.xadd(dlq, dlq_entry, maxlen=DLQ_RETENTION, approximate=True)
            await self._xack(stream, group, message_id)
            log.error(
                "consumer.dlq_moved",
                stream=stream,
                dlq=dlq,
                message_id=self._to_text(message_id),
                times_delivered=times_delivered,
                error=error_reason,
            )
        except Exception as exc:
            # If the DLQ move fails, the message stays in the PEL and
            # will be retried on the next claim cycle. This is safer
            # than losing the message entirely.
            log.error(
                "consumer.dlq_move_failed",
                stream=stream,
                message_id=self._to_text(message_id),
                error=f"{type(exc).__name__}: {exc}",
            )

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
