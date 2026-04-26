# TASK-003 · `fincept-bus` Redis Streams wrapper

**Phase:** F · **Depends on:** TASK-002 · **Blocks:** all services

## Goal

Typed publisher and consumer-group reader for Redis Streams with retention + maxlen + ack/retry semantics.

## Files to create

```
libs/fincept-bus/
├── pyproject.toml
├── src/fincept_bus/
│   ├── __init__.py
│   ├── streams.py           # stream name + retention constants
│   ├── producer.py
│   └── consumer.py
└── tests/
    ├── test_producer.py
    └── test_consumer.py
```

## `pyproject.toml`

```toml
[project]
name = "fincept-bus"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["redis>=5.1", "fincept-core"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fincept_bus"]
```

## Contracts

### `streams.py`

Copy the stream name constants from `spec/CONTRACTS.md §6` verbatim. Additionally:

```python
# Retention (maxlen ~) per stream
RETENTION: dict[str, int] = {
    STREAM_MD_TRADES:   1_000_000,    # ~1-day crypto window
    STREAM_MD_BOOKS:    1_000_000,
    STREAM_MD_BARS_1M:    200_000,
    STREAM_SIG_PREDICT:   100_000,
    STREAM_SIG_SENT:       50_000,
    STREAM_SIG_REGIME:     10_000,
    STREAM_DECISIONS:   1_000_000,    # WORM in archive; maxlen protects memory
    STREAM_ORDERS:      1_000_000,
    STREAM_FILLS:       1_000_000,
    STREAM_POSITIONS:     100_000,
}
```

### `producer.py`

```python
from typing import TypeVar
from pydantic import BaseModel
from redis.asyncio import Redis
from fincept_core.clock import now_ns
from fincept_core.events import StreamEnvelope, serialize
from fincept_core.ids import new_id
from .streams import RETENTION

T = TypeVar("T", bound=BaseModel)

class Producer:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, stream: str, payload: BaseModel) -> str:
        env: StreamEnvelope = StreamEnvelope(event_id=new_id(), published_at=now_ns(), payload=payload)
        maxlen = RETENTION.get(stream)
        msg_id = await self.redis.xadd(
            stream,
            serialize(env),
            maxlen=maxlen,
            approximate=True,
        )
        return msg_id.decode() if isinstance(msg_id, bytes) else msg_id
```

### `consumer.py`

```python
from typing import AsyncIterator, TypeVar
from pydantic import BaseModel
from redis.asyncio import Redis
from fincept_core.events import StreamEnvelope, deserialize
from fincept_core.logging import get_logger

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)

class Consumer:
    """Consumer-group reader with explicit ack. One instance = one consumer."""

    def __init__(self, redis: Redis, group: str, consumer: str) -> None:
        self.redis = redis
        self.group = group
        self.consumer = consumer

    async def ensure_group(self, stream: str) -> None:
        try:
            await self.redis.xgroup_create(stream, self.group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read(
        self, stream: str, model_cls: type[T], block_ms: int = 1000, batch: int = 100
    ) -> AsyncIterator[tuple[str, StreamEnvelope[T]]]:
        """Yield (msg_id, envelope). Caller must ack via ack(stream, msg_id)."""
        await self.ensure_group(stream)
        while True:
            resp = await self.redis.xreadgroup(
                self.group, self.consumer, {stream: ">"}, count=batch, block=block_ms
            )
            if not resp:
                continue
            for _stream, messages in resp:
                for msg_id_b, fields in messages:
                    msg_id = msg_id_b.decode()
                    try:
                        env = deserialize(fields, model_cls)
                        yield msg_id, env
                    except Exception as e:
                        log.error("consumer.decode_failed", msg_id=msg_id, err=str(e))
                        # park poisoned message; do not ack so it redelivers OR dead-letter it
                        await self.redis.xack(stream, self.group, msg_id_b)

    async def ack(self, stream: str, msg_id: str) -> None:
        await self.redis.xack(stream, self.group, msg_id)
```

## Tests

### `tests/test_producer.py`

```python
import pytest
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.schemas import TradeEvent, Venue, AssetClass
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_TRADES

@pytest.mark.asyncio
async def test_publish_returns_id():
    r = Redis.from_url("redis://localhost:6379/15"); await r.delete(STREAM_MD_TRADES)
    p = Producer(r)
    ev = TradeEvent(venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=1, ts_recv=2, price=Decimal("100"), size=Decimal("0.5"))
    mid = await p.publish(STREAM_MD_TRADES, ev)
    assert "-" in mid
    await r.aclose()
```

### `tests/test_consumer.py`

```python
import pytest, asyncio
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.schemas import TradeEvent, Venue, AssetClass
from fincept_bus.producer import Producer
from fincept_bus.consumer import Consumer
from fincept_bus.streams import STREAM_MD_TRADES

@pytest.mark.asyncio
async def test_roundtrip():
    r = Redis.from_url("redis://localhost:6379/15")
    await r.delete(STREAM_MD_TRADES)
    p = Producer(r); c = Consumer(r, group="t-grp", consumer="t-1")
    ev = TradeEvent(venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=1, ts_recv=2, price=Decimal("100"), size=Decimal("0.5"))
    await p.publish(STREAM_MD_TRADES, ev)
    async def one():
        async for mid, env in c.read(STREAM_MD_TRADES, TradeEvent, block_ms=500, batch=1):
            await c.ack(STREAM_MD_TRADES, mid)
            assert env.payload.price == Decimal("100")
            return
    await asyncio.wait_for(one(), timeout=5)
    await r.aclose()
```

## Out of scope

- No dead-letter queue (defer to TASK-070 in Phase H).
- No pipelining optimization (defer until profiling shows it matters).

## Done when

- [ ] Files exist
- [ ] `pytest libs/fincept-bus/tests` green (requires running Redis)
- [ ] `mypy`, `ruff` clean
