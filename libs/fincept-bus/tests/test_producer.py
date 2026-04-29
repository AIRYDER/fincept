from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis

from fincept_bus.producer import Producer
from fincept_bus.streams import RETENTION, STREAM_MD_TRADES
from fincept_core.events import Event, make_event, parse_event
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
    )


def event(seq: int = 1) -> Event:
    return make_event("trade", trade_event(seq).model_dump())


@pytest.mark.asyncio
async def test_publish_returns_id(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    message_id = await producer.publish(STREAM_MD_TRADES, event())

    assert "-" in message_id


@pytest.mark.asyncio
async def test_publish_serializes_event_and_uses_stream_retention(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)
    message_id = await producer.publish(STREAM_MD_TRADES, event())

    messages = await redis_client.xrange(STREAM_MD_TRADES, min=message_id, max=message_id)
    assert len(messages) == 1
    _, fields = messages[0]
    decoded = {
        key.decode() if isinstance(key, bytes) else key: value.decode()
        if isinstance(value, bytes)
        else value
        for key, value in fields.items()
    }
    assert decoded["type"] == "trade"
    assert int(decoded["published_at"]) > 0
    assert len(decoded["event_id"]) == 26
    parsed = parse_event({"type": decoded["type"], "payload": decoded["payload"]})
    assert parsed.payload == trade_event()
    assert RETENTION[STREAM_MD_TRADES] == 1_000_000


@pytest.mark.asyncio
async def test_publish_uses_approximate_maxlen(redis_client: FakeRedis) -> None:
    producer = Producer(redis_client)

    for seq in range(3):
        await producer.publish("unknown.stream", event(seq))

    messages = await redis_client.xrange("unknown.stream")
    assert len(messages) == 3
