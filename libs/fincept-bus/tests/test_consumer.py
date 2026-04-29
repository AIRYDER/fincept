from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ResponseError

from fincept_bus.consumer import Consumer
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
