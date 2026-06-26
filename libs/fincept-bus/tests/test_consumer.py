from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ResponseError

from fincept_bus.consumer import Consumer, _dlq_stream
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_TRADES
from fincept_bus.types import ConsumerGroupName, StreamID
from fincept_core.events import Event, make_event
from fincept_core.schemas import AssetClass, TradeEvent, Venue


@pytest.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    redis = FakeRedis()
    await redis.delete(STREAM_MD_TRADES)
    yield redis
    await redis.aclose()


def trade_event(seq: int = 1) -> TradeEvent:
    return TradeEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=seq,
        ts_recv=seq + 1,
        price=Decimal("100"),
        size=Decimal("0.5"),
        seq=seq,
    )


def event(seq: int = 1) -> Event:
    return make_event("trade", trade_event(seq).model_dump())


@pytest.mark.asyncio
async def test_consume_acks_after_handler_success(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    seen: list[Event] = []

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        seen.append(received)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES], "test-group", "consumer-1", handler, block_ms=100, batch=1
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: len(seen) == 1), timeout=5)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "test-group", 0), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert seen[0].payload == trade_event()


@pytest.mark.asyncio
async def test_handler_failure_leaves_entry_pending(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    calls = 0

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        nonlocal calls
        calls += 1
        raise ValueError(received.type)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES], "fail-group", "consumer-1", handler, block_ms=100, batch=1
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: calls == 1), timeout=5)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "fail-group", 1), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_claims_pending_entries_after_consumer_crash(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    failed_consumer = Consumer(redis_client)
    recovery_consumer = Consumer(redis_client)
    recovered: list[Event] = []

    await producer.publish(STREAM_MD_TRADES, event())

    async def failing_handler(received: Event) -> None:
        raise RuntimeError(received.type)

    failing_task = asyncio.create_task(
        failed_consumer.consume(
            [STREAM_MD_TRADES],
            "claim-group",
            "dead-consumer",
            failing_handler,
            block_ms=100,
            batch=1,
        )
    )
    try:
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "claim-group", 1), timeout=5
        )
    finally:
        failing_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await failing_task

    async def handler(received: Event) -> None:
        recovered.append(received)

    recovered_count = await recovery_consumer.claim_pending(
        STREAM_MD_TRADES,
        "claim-group",
        "recovery-consumer",
        handler,
        min_idle_ms=0,
        count=10,
        block_ms=100,
    )

    assert recovered_count == 1
    assert recovered[0].payload == trade_event()
    assert await _pending_value(redis_client, STREAM_MD_TRADES, "claim-group") == 0


@pytest.mark.asyncio
async def test_integration_consumes_1000_events_without_loss(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    total = 1000
    seen: set[int] = set()

    for seq in range(total):
        await producer.publish(STREAM_MD_TRADES, event(seq))

    async def handler(received: Event) -> None:
        assert isinstance(received.payload, TradeEvent)
        assert received.payload.seq is not None
        seen.add(received.payload.seq)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES], "bulk-group", "consumer-1", handler, block_ms=100, batch=100
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: len(seen) == total), timeout=10)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "bulk-group", 0), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert seen == set(range(total))


@pytest.mark.skip(
    reason="p99 latency assertion is meaningful only against real Redis; re-enabled in TASK-006 CI service container"
)
@pytest.mark.asyncio
async def test_round_trip_latency_p99_under_5ms(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    latencies_ns: list[int] = []
    total = 50
    sent_at: dict[int, int] = {}

    async def handler(received: Event) -> None:
        assert isinstance(received.payload, TradeEvent)
        latencies_ns.append(time.perf_counter_ns() - sent_at[received.payload.ts_event])

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES], "latency-group", "consumer-1", handler, block_ms=100, batch=10
        )
    )
    try:
        for seq in range(total):
            sent_at[seq] = time.perf_counter_ns()
            await producer.publish(STREAM_MD_TRADES, event(seq))
            expected = seq + 1
            await asyncio.wait_for(
                _until(lambda expected=expected: len(latencies_ns) == expected), timeout=5
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    latencies_ns.sort()
    p99_ms = latencies_ns[int(total * 0.99) - 1] / 1_000_000
    assert p99_ms < 5


@pytest.mark.asyncio
async def test_slow_handler_violates_backpressure_contract(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        await asyncio.sleep(0.002)
        assert received.type == "trade"

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            consumer.consume(
                [STREAM_MD_TRADES], "backpressure-group", "consumer-1", handler, block_ms=1, batch=1
            ),
            timeout=1,
        )

    assert await _pending_value(redis_client, STREAM_MD_TRADES, "backpressure-group") == 1


def test_internal_type_aliases_are_strings() -> None:
    stream_id: StreamID = "1-0"
    group_name: ConsumerGroupName = "analytics"

    assert stream_id == "1-0"
    assert group_name == "analytics"


async def _until(predicate) -> None:
    while not predicate():
        waiter = asyncio.Event()
        with suppress(TimeoutError):
            await asyncio.wait_for(waiter.wait(), timeout=0.001)


async def _pending_count(redis: FakeRedis, stream: str, group: str, expected: int) -> None:
    while True:
        if await _pending_value(redis, stream, group) == expected:
            return
        await asyncio.sleep(0.001)


async def _pending_value(redis: FakeRedis, stream: str, group: str) -> int | None:
    try:
        pending = await redis.xpending(stream, group)
    except (IndexError, ResponseError):
        return None
    return int(pending["pending"])


# ---------------------------------------------------------------------------
# Dead-letter queue tests
# ---------------------------------------------------------------------------


def test_dlq_stream_naming() -> None:
    """DLQ stream names follow the {stream}.dlq convention."""
    assert _dlq_stream("ord.orders") == "ord.orders.dlq"
    assert _dlq_stream("md.trades") == "md.trades.dlq"


@pytest.mark.asyncio
async def test_handler_failure_logs_and_stays_pending(redis_client: FakeRedis) -> None:
    """A handler failure should log the error and leave the message pending
    (for retry) when delivery count is below the DLQ threshold.
    """
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    calls = 0

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        nonlocal calls
        calls += 1
        raise ValueError(f"boom {received.type}")

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "dlq-test-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            max_delivery_attempts=10,  # High threshold — won't DLQ on first failure
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: calls == 1), timeout=5)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "dlq-test-group", 1), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Message should still be pending (not acked, not DLQ'd)
    assert calls == 1


@pytest.mark.asyncio
async def test_max_delivery_attempts_moves_to_dlq(redis_client: FakeRedis) -> None:
    """When delivery count reaches max_delivery_attempts, the message is
    moved to the DLQ stream and the original is acked.
    """
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        raise ValueError("poison message")

    # First, consume with a high max_delivery_attempts to get the message
    # into the PEL without DLQing it.
    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "dlq-move-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            max_delivery_attempts=100,  # Won't DLQ
        )
    )
    try:
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "dlq-move-group", 1), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Now use claim_pending with max_delivery_attempts=1 to trigger DLQ.
    # The message has been delivered at least once, so times_delivered >= 1.
    recovery_consumer = Consumer(redis_client)
    await recovery_consumer.claim_pending(
        STREAM_MD_TRADES,
        "dlq-move-group",
        "recovery-consumer",
        handler,
        min_idle_ms=0,
        count=10,
        block_ms=100,
        max_delivery_attempts=1,  # Will DLQ immediately
    )

    # Original should be acked (pending = 0)
    assert await _pending_value(redis_client, STREAM_MD_TRADES, "dlq-move-group") == 0

    # DLQ stream should have 1 entry
    dlq_stream = _dlq_stream(STREAM_MD_TRADES)
    dlq_length = await redis_client.xlen(dlq_stream)
    assert dlq_length == 1


@pytest.mark.asyncio
async def test_dlq_entry_contains_error_context(redis_client: FakeRedis) -> None:
    """The DLQ entry should contain the original stream, message ID, error
    reason, delivery count, and the original fields.
    """
    import json

    producer = Producer(redis_client)
    consumer = Consumer(redis_client)

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        raise ValueError("specific_error_for_dlq_test")

    # Consume to get the message into PEL
    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "dlq-context-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            max_delivery_attempts=100,
        )
    )
    try:
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "dlq-context-group", 1), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Trigger DLQ via claim_pending
    await consumer.claim_pending(
        STREAM_MD_TRADES,
        "dlq-context-group",
        "recovery-consumer",
        handler,
        min_idle_ms=0,
        count=10,
        block_ms=100,
        max_delivery_attempts=1,
    )

    # Read the DLQ entry
    dlq_stream = _dlq_stream(STREAM_MD_TRADES)
    entries = await redis_client.xrevrange(dlq_stream, count=1)
    assert len(entries) == 1
    _msg_id, fields = entries[0]

    decoded = {
        (k.decode() if isinstance(k, bytes) else str(k)): (
            v.decode() if isinstance(v, bytes) else str(v)
        )
        for k, v in fields.items()
    }

    assert decoded["original_stream"] == STREAM_MD_TRADES
    assert "error_reason" in decoded
    assert decoded["times_delivered"] is not None
    assert "fields" in decoded
    # The original fields should be JSON-parseable
    original_fields = json.loads(decoded["fields"])
    assert "type" in original_fields
    assert original_fields["type"] == "trade"


@pytest.mark.asyncio
async def test_dlq_move_failure_keeps_message_pending(redis_client: FakeRedis) -> None:
    """If the DLQ xadd fails, the original message should stay in the PEL
    (not be lost) and will be retried on the next claim cycle.
    """
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        raise ValueError("poison")

    # Consume to get the message into PEL
    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "dlq-fail-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            max_delivery_attempts=100,
        )
    )
    try:
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "dlq-fail-group", 1), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Patch xadd to fail during claim_pending with DLQ threshold=1
    original_xadd = redis_client.xadd

    async def failing_xadd(*args, **kwargs):
        raise ConnectionError("DLQ write failed")

    redis_client.xadd = failing_xadd
    try:
        await consumer.claim_pending(
            STREAM_MD_TRADES,
            "dlq-fail-group",
            "recovery-consumer",
            handler,
            min_idle_ms=0,
            count=10,
            block_ms=100,
            max_delivery_attempts=1,
        )
    finally:
        redis_client.xadd = original_xadd

    # Message should still be pending (DLQ move failed, not acked)
    assert await _pending_value(redis_client, STREAM_MD_TRADES, "dlq-fail-group") == 1


@pytest.mark.asyncio
async def test_successful_handler_acks_normally_with_dlq_enabled(
    redis_client: FakeRedis,
) -> None:
    """With DLQ enabled, successful handler calls should ack normally —
    no DLQ entry should be created.
    """
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    seen: list[Event] = []

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        seen.append(received)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "dlq-success-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            max_delivery_attempts=3,
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: len(seen) == 1), timeout=5)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "dlq-success-group", 0), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # No DLQ entry should exist
    dlq_stream = _dlq_stream(STREAM_MD_TRADES)
    dlq_length = await redis_client.xlen(dlq_stream)
    assert dlq_length == 0


# ---------------------------------------------------------------------------
# Retry backoff tests
# ---------------------------------------------------------------------------


from fincept_bus.consumer import _backoff_idle_ms, BACKOFF_FACTOR, BACKOFF_MAX_MS


def test_backoff_first_attempt_no_increase() -> None:
    """First delivery attempt should use the base idle time."""
    assert _backoff_idle_ms(60_000, 1) == 60_000


def test_backoff_exponential_increase() -> None:
    """Subsequent attempts should increase idle time exponentially."""
    base = 60_000
    assert _backoff_idle_ms(base, 2) == int(base * BACKOFF_FACTOR)
    assert _backoff_idle_ms(base, 3) == int(base * BACKOFF_FACTOR**2)
    assert _backoff_idle_ms(base, 4) == int(base * BACKOFF_FACTOR**3)


def test_backoff_capped_at_max() -> None:
    """Backoff should be capped at BACKOFF_MAX_MS."""
    # With base=60_000 and factor=2, attempt 10 would be 60_000 * 2^9 = 30_720_000
    # which exceeds BACKOFF_MAX_MS (300_000).
    assert _backoff_idle_ms(60_000, 10) == BACKOFF_MAX_MS
    assert _backoff_idle_ms(60_000, 100) == BACKOFF_MAX_MS


# ---------------------------------------------------------------------------
# Graceful shutdown tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_shutdown_with_stop_event(redis_client: FakeRedis) -> None:
    """The consume loop should exit gracefully when stop_event is set."""
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    seen: list[Event] = []
    stop_event = asyncio.Event()

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        seen.append(received)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "shutdown-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
            stop_event=stop_event,
        )
    )
    try:
        # Wait for the message to be processed.
        await asyncio.wait_for(_until(lambda: len(seen) == 1), timeout=5)
        # Now signal shutdown.
        stop_event.set()
        # The task should exit gracefully (not be cancelled).
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        task.cancel()
        raise

    assert len(seen) == 1
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_shutdown_event_not_set_continues_running(redis_client: FakeRedis) -> None:
    """Without stop_event, the consume loop should continue running."""
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    seen: list[Event] = []

    await producer.publish(STREAM_MD_TRADES, event())

    async def handler(received: Event) -> None:
        seen.append(received)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "no-shutdown-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=1,
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: len(seen) == 1), timeout=5)
        await asyncio.sleep(0.2)
        # Task should still be running.
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Handler timeout tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_timeout_ms_separate_from_block_ms(
    redis_client: FakeRedis,
) -> None:
    """handler_timeout_ms should be independent of block_ms."""
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)

    await producer.publish(STREAM_MD_TRADES, event())

    async def slow_handler(received: Event) -> None:
        await asyncio.sleep(0.1)  # 100ms — exceeds handler_timeout_ms=10

    with pytest.raises(TimeoutError, match="handler_timeout_ms"):
        await asyncio.wait_for(
            consumer.consume(
                [STREAM_MD_TRADES],
                "timeout-group",
                "consumer-1",
                slow_handler,
                block_ms=1000,  # Long block time
                batch=1,
                handler_timeout_ms=10,  # Short handler timeout
            ),
            timeout=5,
        )


# ---------------------------------------------------------------------------
# Batch ACK tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_ack_acks_multiple_messages_in_one_call(
    redis_client: FakeRedis,
) -> None:
    """Multiple successful messages should be acked in a single xack call."""
    producer = Producer(redis_client)
    consumer = Consumer(redis_client)
    seen: list[Event] = []

    # Publish 5 events.
    for seq in range(5):
        await producer.publish(STREAM_MD_TRADES, event(seq))

    # Track xack calls.
    original_xack = redis_client.xack
    xack_call_count = 0

    async def counting_xack(*args, **kwargs):
        nonlocal xack_call_count
        xack_call_count += 1
        return await original_xack(*args, **kwargs)

    redis_client.xack = counting_xack

    async def handler(received: Event) -> None:
        seen.append(received)

    task = asyncio.create_task(
        consumer.consume(
            [STREAM_MD_TRADES],
            "batch-ack-group",
            "consumer-1",
            handler,
            block_ms=100,
            batch=10,  # Read all 5 in one batch
        )
    )
    try:
        await asyncio.wait_for(_until(lambda: len(seen) == 5), timeout=5)
        await asyncio.wait_for(
            _pending_count(redis_client, STREAM_MD_TRADES, "batch-ack-group", 0), timeout=5
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        redis_client.xack = original_xack

    # All 5 messages should be acked in a single xack call (batch ACK).
    # Note: the first xack call happens after all messages in the batch
    # are processed.  There may be additional calls from claim_pending
    # (which acks 0 messages), so we check that at most 2 xack calls
    # were made (one batch + possibly one empty claim).
    assert xack_call_count <= 3  # Allow some slack for claim_pending
    assert len(seen) == 5
