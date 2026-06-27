from __future__ import annotations

import asyncio
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

# Default handler timeout in milliseconds.  This is separate from
# ``block_ms`` (which controls how long xreadgroup blocks waiting for
# new messages).  A handler that exceeds this timeout raises
# TimeoutError, which is treated as a handler failure (the message
# stays in the PEL for retry or eventual DLQ).
DEFAULT_HANDLER_TIMEOUT_MS = 5_000

# Retry backoff: multiply the claim idle time by this factor for each
# delivery attempt.  This prevents tight retry loops where a failing
# message is re-claimed and re-failed immediately.
#   attempt 1: claim_idle_ms * BACKOFF_FACTOR^0 = claim_idle_ms
#   attempt 2: claim_idle_ms * BACKOFF_FACTOR^1 = claim_idle_ms * 2
#   attempt 3: claim_idle_ms * BACKOFF_FACTOR^2 = claim_idle_ms * 4
#   ...
# Capped at BACKOFF_MAX_MS to avoid excessively long delays.
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_MS = 300_000  # 5 minutes


def _dlq_stream(stream: str) -> str:
    """Return the DLQ stream name for a given source stream."""
    return f"{stream}{DLQ_SUFFIX}"


def _backoff_idle_ms(base_idle_ms: int, delivery_count: int) -> int:
    """Calculate the idle time before re-claiming a failed message.

    Exponential backoff: base * factor^(delivery_count - 1), capped.
    """
    if delivery_count <= 1:
        return base_idle_ms
    backoff = int(base_idle_ms * (BACKOFF_FACTOR ** (delivery_count - 1)))
    return min(backoff, BACKOFF_MAX_MS)


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
        handler_timeout_ms: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Consume messages from one or more streams.

        Parameters
        ----------
        streams
            Stream names to read from.
        group
            Consumer group name.
        consumer_name
            This consumer's name (for PEL tracking).
        handler
            Async callable invoked for each event.
        block_ms
            How long xreadgroup blocks waiting for new messages (ms).
        batch
            Max messages per xreadgroup call.
        claim_idle_ms
            Base idle time before claiming stale pending messages (ms).
            Actual idle time is increased with exponential backoff for
            messages that have failed multiple times.
        max_delivery_attempts
            Delivery count at which a message is moved to the DLQ.
        handler_timeout_ms
            Max time a handler may take per event (ms).  If None,
            defaults to ``block_ms`` (backward compatible).
        stop_event
            If provided, the consume loop checks this event after each
            iteration and exits gracefully when set.  In-flight handlers
            are allowed to complete before exiting.
        """
        await self.ensure_groups(streams, group)
        stream_offsets = {stream: ">" for stream in streams}
        effective_timeout = handler_timeout_ms if handler_timeout_ms is not None else block_ms
        while True:
            if stop_event is not None and stop_event.is_set():
                log.info("consumer.shutdown", group=group, consumer=consumer_name)
                return
            await self._claim_stale(
                streams,
                group,
                consumer_name,
                handler,
                claim_idle_ms,
                batch,
                effective_timeout,
                max_delivery_attempts,
            )
            if stop_event is not None and stop_event.is_set():
                log.info("consumer.shutdown", group=group, consumer=consumer_name)
                return
            response = await self.redis.xreadgroup(
                group,
                consumer_name,
                stream_offsets,
                count=batch,
                block=block_ms,
            )
            # Batch ACK: collect successful message IDs per stream.
            for stream_name, messages in response:
                stream = self._to_text(stream_name)
                ack_ids: list[Any] = []
                for message_id, fields in messages:
                    handled = await self._handle_message(
                        stream,
                        group,
                        message_id,
                        fields,
                        handler,
                        effective_timeout,
                        max_delivery_attempts,
                    )
                    if handled:
                        ack_ids.append(message_id)
                # Batch ACK all successfully handled messages in one call.
                if ack_ids:
                    await self._xack_batch(stream, group, ack_ids)

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
        handler_timeout_ms: int | None = None,
    ) -> int:
        """Claim and re-process stale pending messages.

        Uses exponential backoff: messages that have failed multiple
        times are only re-claimed after a longer idle period, preventing
        tight retry loops.
        """
        await self.ensure_group(stream, group)
        effective_timeout = handler_timeout_ms if handler_timeout_ms is not None else block_ms
        pending = await self.redis.xpending_range(stream, group, min="-", max="+", count=count)
        # Group messages by their backoff-adjusted idle time.  We only
        # claim messages whose time_since_delivered exceeds the
        # backoff-adjusted threshold for their delivery count.
        message_ids: list[Any] = []
        for entry in pending:
            times_delivered = int(entry.get("times_delivered", 1))
            # Check delivery count — if exceeded, move to DLQ instead of retrying.
            if times_delivered >= max_delivery_attempts:
                await self._move_to_dlq(
                    stream,
                    group,
                    entry["message_id"],
                    times_delivered,
                    "max_delivery_attempts_exceeded",
                )
                continue
            # Apply exponential backoff: increase the required idle time
            # based on how many times the message has been delivered.
            required_idle = _backoff_idle_ms(min_idle_ms, times_delivered)
            if int(entry["time_since_delivered"]) < required_idle:
                continue
            message_ids.append(entry["message_id"])
        if not message_ids:
            return 0
        claimed = await self._xclaim(stream, group, consumer_name, min_idle_ms, message_ids)
        handled = 0
        ack_ids: list[Any] = []
        for message_id, fields in claimed:
            ok = await self._handle_message(
                stream,
                group,
                message_id,
                fields,
                handler,
                effective_timeout,
                max_delivery_attempts,
            )
            if ok:
                ack_ids.append(message_id)
                handled += 1
        if ack_ids:
            await self._xack_batch(stream, group, ack_ids)
        return handled

    async def _claim_stale(
        self,
        streams: Sequence[str],
        group: ConsumerGroupName,
        consumer_name: str,
        handler: Handler,
        min_idle_ms: int,
        count: int,
        handler_timeout_ms: int,
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
                block_ms=handler_timeout_ms,
                max_delivery_attempts=max_delivery_attempts,
                handler_timeout_ms=handler_timeout_ms,
            )

    async def _handle_message(
        self,
        stream: str,
        group: ConsumerGroupName,
        message_id: StreamID | bytes,
        fields: RedisFields,
        handler: Handler,
        handler_timeout_ms: int,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    ) -> bool:
        """Process a single message.  Returns True if the message was
        successfully handled (and should be acked), False if it should
        stay in the PEL for retry.

        On handler failure:
        - Log the error (previously silent).
        - Check delivery count.  If >= max_delivery_attempts, move to DLQ
          and return True (acked via DLQ move).
        - Otherwise return False (stays pending for retry with backoff).
        """
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
        if elapsed_ns > handler_timeout_ms * 1_000_000:
            raise TimeoutError(
                f"consumer handler exceeded handler_timeout_ms ({handler_timeout_ms}ms)"
            )
        return True  # Caller will batch-ack.

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
            return 1
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
        # Fetch the original message fields from the stream so they can
        # be preserved in the DLQ entry for debugging/replay.
        import json

        raw_messages = await self.redis.xrange(stream, min=message_id, max=message_id)
        fields: dict[Any, Any] = {}
        if raw_messages:
            # xrange returns [(message_id, {field: value, ...}), ...]
            fields = raw_messages[0][1] if raw_messages[0] else {}

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
        """Acknowledge a single message (used by DLQ moves)."""
        redis = cast(Any, self.redis)
        await redis.xack(stream, group, message_id)

    async def _xack_batch(
        self, stream: str, group: ConsumerGroupName, message_ids: list[StreamID | bytes]
    ) -> None:
        """Acknowledge multiple messages in a single Redis call.

        This is significantly more efficient than per-message xack calls
        for high-throughput streams.  Redis xack accepts multiple IDs:
        ``XACK stream group id1 id2 id3 ...``
        """
        if not message_ids:
            return
        redis = cast(Any, self.redis)
        await redis.xack(stream, group, *message_ids)

    @staticmethod
    def _to_text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)
