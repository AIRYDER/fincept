# TASK-044 · Paper OMS (singleton: order state machine + fill simulator + audit)

**Phase:** O · **Depends on:** TASK-041, TASK-010 (live mid-price feed) · **Blocks:** TASK-045 (portfolio)

**Status:** [x] Implemented and verified.  Includes TASK-045 (Portfolio service) which is the natural downstream of Fills.

## As-built deviations from the original draft

| Spec said | We did | Why |
|---|---|---|
| `consumer.read(STREAM, Schema)` async-iterator API | Wired through the existing `Consumer.consume(streams, group, name, handler)` API; handler narrows on `isinstance(payload, OrderIntent)` | Spec snippet didn't match the actual `fincept_bus.Consumer` shape from earlier tasks. Aligning with what the bus exposes. |
| `producer.publish(stream, fill)` directly with a Pydantic model | Wrapped: `Event(type="fill", payload=fill)` etc. via the existing producer surface | `Producer.publish` is typed `(stream, Event)`. Extending the `Event.payload` union to accept `OrderIntent`, `Order`, `Fill`, `Position` keeps every stream message a deserializable Event. Same pattern as `AlertEvent` (TASK-014) and `FeatureFrame` (TASK-016). |
| `s.trading_mode` (lowercase) | Reads `s.TRADING_MODE` (the actual `Settings` field) | Lowercase form would `AttributeError` at startup. |
| OMS pipeline expressed inline in `main.py` | Extracted `process_intent(intent, prices, filler) -> IntentResult` as a pure synchronous function in `oms.processor` | Lets tests exercise the full intent -> states -> fill flow without a Redis consumer loop. `main.py` is thin: tail streams, dispatch through `process_intent`, publish results. Pinned by `test_processor.py`. |
| `Leader(redis, role="oms")` HA / leadership election | **Skipped** | Single-process OMS for v1.  HA election + dual-writer suppression is a Phase H (TASK-072+) concern; adding it now would over-engineer without test coverage of the failure modes. Deferred. |
| `random.gauss` directly inside `PaperFiller` | `rng: Callable[[float, float], float]` injected at construction (defaults to `random.gauss`) | Tests use `lambda mu, _sigma: mu` for deterministic fills; production wires the real RNG. Same DI rule used in TASK-014 (clock) and TASK-016 (producer). |
| Spec didn't address strategy attribution for Fills | Portfolio service recovers `strategy_id` by querying the OMS audit log: `audit.read_by_correlation(fill.order_id)` returns the `oms.intent` row whose payload includes `strategy_id` | `Fill` schema has no `strategy_id` field. The audit trail already carries it. Resolver is injectable so tests use a constant; production wires the audit lookup. Documented in `services/portfolio/src/portfolio/main.py`. |
| Spec didn't address position math drift between backtester and live | Extracted `apply_fill_to_position(prev, fill, *, strategy_id)` to `fincept_core.portfolio` and refactored both the backtester engine and the portfolio service to call it | Same problem TASK-017 solved with `FeatureComputer` — kill drift risk by centralising the kernel. The four-case decision tree (open-fresh / open-more / exact-close / cross-flip) lives in one place. Pinned by 12 tests in `libs/fincept-core/tests/test_portfolio.py`. |
| **TASK-045 spec doesn't exist** | Designed Portfolio service from BUILD_ORDER + CONTRACTS.md §5: `PositionStore` (Redis hash for fast UI reads under `positions:{strategy_id}`), `PortfolioState` (in-memory, hydrated from store on startup), `apply_fill` (consumer-loop entry point that mutates state + store + publishes to STREAM_POSITIONS) | The Redis hash IS the UI integration: `/positions` REST endpoint (TASK-050) does HGETALL — sub-millisecond, no DB. Streams handle the change log for WebSocket (TASK-051). Same online/offline split TASK-017 used for features. |
| Schemas without `event_type` field (`OrderIntent`, `Order`, `Fill`, `Position`) couldn't be wrapped via `make_event` because `extra="forbid"` rejected the auto-injected `event_type` key | Modified `make_event` to only inject `event_type` for schemas that declare it (market events, alerts, feature frames) | The auto-injection was a convenience for market-data events; it doesn't generalise to schemas that don't carry an `event_type` field. The contract surface is unchanged. |

## Goal

Consume `OrderIntent` from `ord.orders`, persist immutable state transitions, simulate fills using live market mid-price + a small Gaussian latency, emit `Fill` events. Full audit trail via append-only log.

## Files to create

```
services/oms/
├── pyproject.toml
├── src/oms/
│   ├── __init__.py
│   ├── main.py
│   ├── paper.py
│   ├── state.py
│   ├── prices.py        # latest mid-price cache from md.trades
│   └── audit.py
└── tests/
    ├── test_state.py
    └── test_paper.py
```

## Contracts

### `state.py`

```python
from fincept_core.schemas import OrderStatus

VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_NEW: {OrderStatus.NEW, OrderStatus.REJECTED},
    OrderStatus.NEW: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}

def can_transition(frm: OrderStatus, to: OrderStatus) -> bool:
    return to in VALID_TRANSITIONS.get(frm, set())
```

### `prices.py`

```python
from decimal import Decimal
from redis.asyncio import Redis

class LivePrices:
    """Cache latest trade price per symbol. Updated by background reader of md.trades."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._cache: dict[str, Decimal] = {}

    def update(self, symbol: str, price: Decimal) -> None:
        self._cache[symbol] = price

    def get(self, symbol: str) -> Decimal | None:
        return self._cache.get(symbol)
```

### `paper.py`

```python
import random
from decimal import Decimal
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import (
    Order, OrderIntent, OrderStatus, OrderType, Side, Fill
)

class PaperFiller:
    """Simulates fills using LivePrices. Adds Gaussian latency + small slippage."""

    def __init__(self, mean_latency_ms: float = 50.0, std_latency_ms: float = 15.0,
                 spread_bps: float = 3.0) -> None:
        self.lat_mean = mean_latency_ms
        self.lat_std = std_latency_ms
        self.spread_bps = spread_bps

    def latency_ns(self) -> int:
        ms = max(0.0, random.gauss(self.lat_mean, self.lat_std))
        return int(ms * 1_000_000)

    def fill(self, order: Order, mid: Decimal) -> Fill:
        half_spread = mid * Decimal(self.spread_bps) / Decimal(10000) / 2
        if order.order_type == OrderType.MARKET:
            px = mid + half_spread if order.side == Side.BUY else mid - half_spread
        else:
            assert order.limit_price is not None
            px = order.limit_price
        return Fill(
            fill_id=new_id(), order_id=order.order_id, ts_event=now_ns() + self.latency_ns(),
            symbol=order.symbol, side=order.side, price=px, quantity=order.quantity,
            fee=px * order.quantity * Decimal("0.0005"),  # 5 bp taker fee
            is_maker=(order.order_type == OrderType.LIMIT),
        )
```

### `audit.py`

```python
from fincept_db.audit import append_audit
from fincept_core.schemas import OrderIntent, Order, Fill, OrderStatus

async def log_intent(intent: OrderIntent) -> None:
    await append_audit("oms.intent", intent.model_dump(mode="json"))

async def log_state(order: Order, prev: OrderStatus | None) -> None:
    await append_audit("oms.state", {"order_id": order.order_id, "from": prev, "to": order.status, "data": order.model_dump(mode="json")})

async def log_fill(fill: Fill) -> None:
    await append_audit("oms.fill", fill.model_dump(mode="json"))
```

### `main.py`

```python
import asyncio, socket
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.clock import now_ns
from fincept_core.logging import configure_logging, get_logger
from fincept_core.leadership import Leader
from fincept_core.schemas import OrderIntent, Order, OrderStatus, TradeEvent
from fincept_bus.producer import Producer
from fincept_bus.consumer import Consumer
from fincept_bus.streams import (
    STREAM_ORDERS, STREAM_FILLS, STREAM_MD_TRADES,
)
from .paper import PaperFiller
from .prices import LivePrices
from .state import can_transition
from . import audit

configure_logging()
log = get_logger(__name__)

async def price_updater(consumer: Consumer, prices: LivePrices) -> None:
    async for mid, env in consumer.read(STREAM_MD_TRADES, TradeEvent, batch=500):
        prices.update(env.payload.symbol, env.payload.price)
        await consumer.ack(STREAM_MD_TRADES, mid)

async def order_processor(consumer: Consumer, producer: Producer, prices: LivePrices, filler: PaperFiller) -> None:
    async for mid, env in consumer.read(STREAM_ORDERS, OrderIntent):
        intent = env.payload
        await audit.log_intent(intent)
        order = Order(**intent.model_dump(), created_at=now_ns(), updated_at=now_ns(), status=OrderStatus.PENDING_NEW)
        await audit.log_state(order, prev=None)
        # transition NEW
        if not can_transition(order.status, OrderStatus.NEW):
            await consumer.ack(STREAM_ORDERS, mid); continue
        order = order.model_copy(update={"status": OrderStatus.NEW, "updated_at": now_ns()})
        await audit.log_state(order, prev=OrderStatus.PENDING_NEW)
        # fill
        mid_px = prices.get(order.symbol)
        if mid_px is None:
            log.warning("oms.no_price", symbol=order.symbol)
            order = order.model_copy(update={"status": OrderStatus.REJECTED, "updated_at": now_ns()})
            await audit.log_state(order, prev=OrderStatus.NEW)
            await consumer.ack(STREAM_ORDERS, mid); continue
        fill = filler.fill(order, mid_px)
        await producer.publish(STREAM_FILLS, fill)
        await audit.log_fill(fill)
        order = order.model_copy(update={
            "status": OrderStatus.FILLED, "filled_qty": fill.quantity,
            "avg_fill_price": fill.price, "updated_at": now_ns(),
        })
        await audit.log_state(order, prev=OrderStatus.NEW)
        log.info("oms.filled", order_id=order.order_id, price=str(fill.price), qty=str(fill.quantity))
        await consumer.ack(STREAM_ORDERS, mid)

async def run() -> None:
    s = get_settings()
    if s.trading_mode != "paper":
        raise RuntimeError("Live mode not enabled in this build")
    redis = Redis.from_url(s.redis_url)
    leader = Leader(redis, role="oms")
    await leader.start()
    try:
        while not leader.is_leader:
            await asyncio.sleep(0.5)
        producer = Producer(redis)
        prices = LivePrices(redis)
        filler = PaperFiller()
        cid = f"oms-{socket.gethostname()}"
        c_md = Consumer(redis, "oms", f"{cid}-md")
        c_or = Consumer(redis, "oms", f"{cid}-or")
        await asyncio.gather(
            price_updater(c_md, prices),
            order_processor(c_or, producer, prices, filler),
        )
    finally:
        await leader.stop()
        await redis.aclose()

def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
```

## Tests

### `tests/test_state.py`

```python
from fincept_core.schemas import OrderStatus
from oms.state import can_transition

def test_transitions():
    assert can_transition(OrderStatus.NEW, OrderStatus.FILLED)
    assert not can_transition(OrderStatus.FILLED, OrderStatus.NEW)
    assert can_transition(OrderStatus.PENDING_NEW, OrderStatus.REJECTED)
```

### `tests/test_paper.py`

```python
from decimal import Decimal
from fincept_core.schemas import (
    Order, OrderType, Side, OrderStatus, Venue, TimeInForce
)
from oms.paper import PaperFiller

def test_paper_fill_market_buy():
    f = PaperFiller(mean_latency_ms=0, std_latency_ms=0, spread_bps=10)
    o = Order(order_id="1", decision_id="d", ts_event=0, strategy_id="s", symbol="BTC-USD",
              venue=Venue.PAPER, side=Side.BUY, order_type=OrderType.MARKET,
              quantity=Decimal("1"), time_in_force=TimeInForce.IOC,
              status=OrderStatus.NEW, filled_qty=Decimal(0), created_at=0, updated_at=0)
    fill = f.fill(o, mid=Decimal("100"))
    assert fill.price > Decimal("100")  # buyer pays half-spread above mid
    assert fill.quantity == Decimal("1")
```

## Out of scope

- Partial fills — Phase H refinement
- Live venue routing — TASK-075
- Cancel/replace — Phase H

## Done when

- [ ] Files exist, tests green
- [ ] Manual: end-to-end — publish OrderIntent on `ord.orders` while ingestor publishes prices → observe Fill on `ord.fills` within 1s
