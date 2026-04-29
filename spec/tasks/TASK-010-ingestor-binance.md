# TASK-010 · Ingestor base + Binance spot WebSocket adapter

**Phase:** D · **Depends on:** TASK-002, TASK-003, TASK-004 · **Blocks:** all features + agents

**Status:** [x] Implemented and verified (also covers TASK-011 — Binance adapter ships with the base infrastructure).

## As-built deviations from the original draft

| Spec said | We did | Why |
|---|---|---|
| `Producer.publish(stream, ev)` directly | `Producer.publish(stream, Event(type=..., payload=ev))` | `Producer.publish` is typed for `Event`; `OrderIntent` and market-data payloads must be wrapped per CONTRACTS §1. |
| `batch_insert_trades` / `batch_insert_book_deltas` | `write_trades` / `write_book_deltas` | The actual fincept-db API names ship under `write_*` (TASK-004); spec was speculative. |
| `normalizer.py` not present (logic inside `binance.py`) | Extracted as `normalizer.py` with `to_canonical`, `to_binance_symbol`, `to_coinbase_symbol`, `to_kraken_symbol` | TASK-012/013 (Coinbase/Kraken) will reuse the canonical-symbol helpers; pre-extracting prevents copy-paste drift. |
| `main.py` has no reconnect / backoff | `run_loop` has capped exponential backoff (`INITIAL_BACKOFF_S=1`, cap `MAX_BACKOFF_S=60`) and quality-monitor wiring | A bare `connect → stream` loop crashes the process on the first WS disconnect. The wrapper here is the minimum for "doesn't crash"; full snapshot-sync still belongs to TASK-014. |
| `quality.py` was a single-counter helper | `QualityMonitor` records gap counts, max latency, and rolling p99 (1024-sample window); clamps negative latencies to 0; non-monotonic seqs don't fake gaps | The richer surface area is needed by TASK-014 (`Snapshot` rows feed Prometheus / OTel). |
| No `py.typed` marker | Added | All workspace packages have `py.typed` (PEP 561) so cross-package mypy strict resolves correctly. |

## Goal

Long-running process that connects to Binance spot WebSocket, normalizes trades and L2 book updates to canonical schemas, publishes to Redis Streams, and persists to Timescale.

## Files to create

```
services/ingestor/
├── pyproject.toml
├── src/ingestor/
│   ├── __init__.py
│   ├── main.py              # entrypoint
│   ├── base.py              # VenueAdapter ABC
│   ├── binance.py           # Binance adapter
│   ├── normalizer.py        # venue → canonical (imported per-venue)
│   ├── writer.py            # fan-out to Redis + Timescale
│   └── quality.py           # latency + gap metrics
└── tests/
    ├── test_base.py
    └── test_binance_normalize.py
```

## `pyproject.toml`

```toml
[project]
name = "ingestor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fincept-core", "fincept-bus", "fincept-db",
    "websockets>=13", "orjson>=3.10", "httpx>=0.27",
]
```

## Contracts

### `base.py`

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel
from fincept_core.schemas import Venue

class VenueAdapter(ABC):
    venue: Venue

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def stream(self) -> AsyncIterator[BaseModel]:
        """Yields TradeEvent | BookDeltaEvent | BookSnapshotEvent."""

    @abstractmethod
    async def close(self) -> None: ...
```

### `binance.py`

```python
import asyncio, orjson, websockets
from decimal import Decimal
from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    Venue, AssetClass, TradeEvent, BookDeltaEvent, BookLevel, Side
)
from .base import VenueAdapter

log = get_logger(__name__)
WS_URL = "wss://stream.binance.com:9443/stream"

def to_canonical(sym: str) -> str:
    """BTCUSDT → BTC-USDT. Adjust for common quotes."""
    # simplistic; replace with pair lookup in production
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if sym.endswith(quote):
            return f"{sym[:-len(quote)]}-{quote}"
    return sym

def to_venue(sym: str) -> str:
    return sym.replace("-", "").lower()

class BinanceAdapter(VenueAdapter):
    venue = Venue.BINANCE

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self) -> None:
        streams = []
        for s in self.symbols:
            v = to_venue(s)
            streams.append(f"{v}@trade")
            streams.append(f"{v}@depth@100ms")
        url = f"{WS_URL}?streams={'/'.join(streams)}"
        self._ws = await websockets.connect(url, ping_interval=15, ping_timeout=10, max_size=2**22)
        log.info("binance.connected", streams=len(streams))

    async def stream(self):
        assert self._ws is not None
        async for msg in self._ws:
            ts_recv = now_ns()
            data = orjson.loads(msg)
            ev = data.get("data", {})
            etype = ev.get("e")
            if etype == "trade":
                yield TradeEvent(
                    venue=Venue.BINANCE,
                    symbol=to_canonical(ev["s"]),
                    asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=int(ev["T"]) * 1_000_000,  # ms → ns
                    ts_recv=ts_recv,
                    price=Decimal(ev["p"]),
                    size=Decimal(ev["q"]),
                    side=Side.SELL if ev.get("m") else Side.BUY,  # m=is_maker; buyer side when m=False
                    seq=int(ev.get("t", 0)),
                )
            elif etype == "depthUpdate":
                yield BookDeltaEvent(
                    venue=Venue.BINANCE,
                    symbol=to_canonical(ev["s"]),
                    asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=int(ev["E"]) * 1_000_000,
                    ts_recv=ts_recv,
                    seq=int(ev.get("u", 0)),
                    bids_add=[BookLevel(price=Decimal(p), size=Decimal(q)) for p, q in ev.get("b", []) if Decimal(q) > 0],
                    bids_remove=[Decimal(p) for p, q in ev.get("b", []) if Decimal(q) == 0],
                    asks_add=[BookLevel(price=Decimal(p), size=Decimal(q)) for p, q in ev.get("a", []) if Decimal(q) > 0],
                    asks_remove=[Decimal(p) for p, q in ev.get("a", []) if Decimal(q) == 0],
                )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
```

### `writer.py`

```python
from redis.asyncio import Redis
from pydantic import BaseModel
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_TRADES, STREAM_MD_BOOKS
from fincept_core.schemas import TradeEvent, BookDeltaEvent, BookSnapshotEvent
from fincept_db.ticks import batch_insert_trades, batch_insert_book_deltas

class Writer:
    def __init__(self, redis: Redis, batch_size: int = 500) -> None:
        self.producer = Producer(redis)
        self.batch_size = batch_size
        self._trades: list[TradeEvent] = []
        self._books: list[BookDeltaEvent] = []

    async def handle(self, ev: BaseModel) -> None:
        if isinstance(ev, TradeEvent):
            await self.producer.publish(STREAM_MD_TRADES, ev)
            self._trades.append(ev)
            if len(self._trades) >= self.batch_size:
                await self._flush_trades()
        elif isinstance(ev, (BookDeltaEvent, BookSnapshotEvent)):
            await self.producer.publish(STREAM_MD_BOOKS, ev)
            if isinstance(ev, BookDeltaEvent):
                self._books.append(ev)
                if len(self._books) >= self.batch_size:
                    await self._flush_books()

    async def _flush_trades(self) -> None:
        if not self._trades:
            return
        await batch_insert_trades(self._trades)
        self._trades.clear()

    async def _flush_books(self) -> None:
        if not self._books:
            return
        await batch_insert_book_deltas(self._books)
        self._books.clear()

    async def flush(self) -> None:
        await self._flush_trades()
        await self._flush_books()
```

### `quality.py`

```python
from collections import defaultdict
from fincept_core.logging import get_logger

log = get_logger(__name__)

class QualityMonitor:
    def __init__(self) -> None:
        self.last_seq: dict[str, int] = {}     # key = f"{venue}:{symbol}"
        self.gaps: dict[str, int] = defaultdict(int)
        self.max_latency_ns: dict[str, int] = defaultdict(int)

    def observe(self, venue: str, symbol: str, seq: int | None, ts_event: int, ts_recv: int) -> None:
        k = f"{venue}:{symbol}"
        if seq is not None:
            last = self.last_seq.get(k)
            if last is not None and seq > last + 1:
                self.gaps[k] += (seq - last - 1)
                log.warning("md.gap", key=k, gap=seq - last - 1)
            self.last_seq[k] = seq
        latency = ts_recv - ts_event
        if latency > self.max_latency_ns[k]:
            self.max_latency_ns[k] = latency
```

### `main.py`

```python
import asyncio, signal
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing
from .binance import BinanceAdapter
from .writer import Writer
from .quality import QualityMonitor

configure_logging()
configure_tracing("ingestor")
log = get_logger(__name__)

async def run() -> None:
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    adapter = BinanceAdapter(s.universe)
    writer = Writer(redis)
    quality = QualityMonitor()
    await adapter.connect()
    try:
        async for ev in adapter.stream():
            quality.observe(ev.venue, ev.symbol, ev.seq, ev.ts_event, ev.ts_recv)
            await writer.handle(ev)
    finally:
        await writer.flush()
        await adapter.close()
        await redis.aclose()

def main() -> None:
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    loop.run_until_complete(run())

if __name__ == "__main__":
    main()
```

## Tests

### `tests/test_binance_normalize.py`

```python
from decimal import Decimal
from ingestor.binance import to_canonical, to_venue

def test_symbol_roundtrip():
    assert to_canonical("BTCUSDT") == "BTC-USDT"
    assert to_venue("BTC-USDT") == "btcusdt"
```

Integration test (requires live connection) is gated by env var and lives in a CI job tagged `network`.

## Out of scope

- No reconnect loop with exponential backoff in this task — wrap `run()` in TASK-014.
- No snapshot → delta sync protocol yet — TASK-014.
- No Coinbase / Kraken — those are TASK-012 / TASK-013 mirroring this structure.

## Done when

- [ ] Files exist
- [ ] `pytest services/ingestor/tests` green
- [ ] `python -m ingestor.main` runs without crashing and publishes to Redis within 30s of startup (validated manually)
