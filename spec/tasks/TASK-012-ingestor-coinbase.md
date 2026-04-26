# TASK-012 · Coinbase Advanced Trade WS adapter

**Phase:** D · **Depends on:** TASK-010 · **Blocks:** TASK-014 (quality monitor uses cross-venue spread)

## Goal

Coinbase Advanced Trade WebSocket adapter implementing `VenueAdapter`, normalizing market_trades + level2 channels to canonical `TradeEvent` and `BookDeltaEvent` / `BookSnapshotEvent`. Reuses `services/ingestor/{base,normalizer,writer,quality}.py` from TASK-010.

## Files to create

```
services/ingestor/src/ingestor/coinbase.py
services/ingestor/tests/test_coinbase_normalize.py
```

(No new pyproject; extends the `ingestor` service.)

## Contracts

### `coinbase.py`

```python
import asyncio, hmac, hashlib, time
from decimal import Decimal
from typing import AsyncIterator
import orjson, websockets
from pydantic import BaseModel
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    Venue, AssetClass, TradeEvent, BookDeltaEvent, BookSnapshotEvent, BookLevel, Side,
)
from .base import VenueAdapter

log = get_logger(__name__)
WS_URL = "wss://advanced-trade-ws.coinbase.com"

def to_canonical(sym: str) -> str:
    """Coinbase already uses BTC-USD format. Pass through."""
    return sym.upper()

def to_venue(sym: str) -> str:
    return sym.upper()

class CoinbaseAdapter(VenueAdapter):
    venue = Venue.COINBASE

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(WS_URL, ping_interval=10, ping_timeout=10, max_size=8 * 1024 * 1024)
        sub_trades = {
            "type": "subscribe",
            "product_ids": [to_venue(s) for s in self.symbols],
            "channel": "market_trades",
        }
        sub_l2 = {
            "type": "subscribe",
            "product_ids": [to_venue(s) for s in self.symbols],
            "channel": "level2",
        }
        # Public channels do not require auth; auth path documented but unused in v1.
        await self._ws.send(orjson.dumps(sub_trades).decode())
        await self._ws.send(orjson.dumps(sub_l2).decode())
        log.info("coinbase.connected", symbols=self.symbols)

    async def stream(self) -> AsyncIterator[BaseModel]:
        assert self._ws is not None
        async for raw in self._ws:
            msg = orjson.loads(raw)
            ch = msg.get("channel")
            ts_recv = now_ns()
            if ch == "market_trades":
                for ev in self._parse_trades(msg, ts_recv):
                    yield ev
            elif ch == "l2_data":
                for ev in self._parse_l2(msg, ts_recv):
                    yield ev
            # else: heartbeat / subscriptions confirmation — drop silently.

    def _parse_trades(self, msg: dict, ts_recv: int) -> list[TradeEvent]:
        out: list[TradeEvent] = []
        for evt in msg.get("events", []):
            for tr in evt.get("trades", []):
                out.append(TradeEvent(
                    venue=Venue.COINBASE,
                    symbol=to_canonical(tr["product_id"]),
                    asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=int(int(tr["time"].replace("Z", "").replace("-","").replace(":","").replace("T","").replace(".",""))[:13]) * 1_000_000  # ISO → ns; replace with proper parser
                              if False else self._iso_to_ns(tr["time"]),
                    ts_recv=ts_recv,
                    seq=int(tr["trade_id"]),
                    price=Decimal(tr["price"]),
                    size=Decimal(tr["size"]),
                    side=Side.BUY if tr["side"].lower() == "buy" else Side.SELL,
                ))
        return out

    def _parse_l2(self, msg: dict, ts_recv: int) -> list[BaseModel]:
        out: list[BaseModel] = []
        for evt in msg.get("events", []):
            etype = evt.get("type")  # "snapshot" | "update"
            symbol = to_canonical(evt["product_id"])
            ts_event = self._iso_to_ns(msg.get("timestamp", evt.get("time", "")))
            if etype == "snapshot":
                bids = [BookLevel(price=Decimal(u["price_level"]), size=Decimal(u["new_quantity"]))
                        for u in evt["updates"] if u["side"] == "bid"]
                asks = [BookLevel(price=Decimal(u["price_level"]), size=Decimal(u["new_quantity"]))
                        for u in evt["updates"] if u["side"] == "offer"]
                out.append(BookSnapshotEvent(
                    venue=Venue.COINBASE, symbol=symbol, asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=ts_event, ts_recv=ts_recv, bids=bids, asks=asks,
                ))
            elif etype == "update":
                bids_add: list[BookLevel] = []
                bids_rm: list[Decimal] = []
                asks_add: list[BookLevel] = []
                asks_rm: list[Decimal] = []
                for u in evt["updates"]:
                    p = Decimal(u["price_level"])
                    q = Decimal(u["new_quantity"])
                    target_add = bids_add if u["side"] == "bid" else asks_add
                    target_rm = bids_rm if u["side"] == "bid" else asks_rm
                    if q == 0:
                        target_rm.append(p)
                    else:
                        target_add.append(BookLevel(price=p, size=q))
                out.append(BookDeltaEvent(
                    venue=Venue.COINBASE, symbol=symbol, asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=ts_event, ts_recv=ts_recv,
                    bids_add=bids_add, bids_remove=bids_rm,
                    asks_add=asks_add, asks_remove=asks_rm,
                ))
        return out

    @staticmethod
    def _iso_to_ns(s: str) -> int:
        from datetime import datetime
        if not s:
            return now_ns()
        # Coinbase: "2024-12-01T12:00:00.123456Z"
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1e9)

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
```

## Tests

### `tests/test_coinbase_normalize.py`

```python
from decimal import Decimal
from ingestor.coinbase import CoinbaseAdapter
from fincept_core.schemas import TradeEvent, Side

def test_parse_trades_basic():
    a = CoinbaseAdapter(["BTC-USD"])
    msg = {
        "channel": "market_trades",
        "events": [{
            "trades": [{
                "trade_id": "12345",
                "product_id": "BTC-USD",
                "price": "100000.50",
                "size": "0.123",
                "side": "BUY",
                "time": "2024-12-01T12:00:00.123Z",
            }]
        }],
    }
    out = a._parse_trades(msg, ts_recv=1_700_000_000_000_000_000)
    assert len(out) == 1
    assert out[0].price == Decimal("100000.50")
    assert out[0].symbol == "BTC-USD"
    assert out[0].side == Side.BUY

def test_parse_l2_snapshot_then_update():
    a = CoinbaseAdapter(["BTC-USD"])
    snap = {
        "channel": "l2_data",
        "timestamp": "2024-12-01T12:00:00Z",
        "events": [{
            "type": "snapshot",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100000", "new_quantity": "1.0"},
                {"side": "offer", "price_level": "100100", "new_quantity": "0.5"},
            ],
        }],
    }
    out = a._parse_l2(snap, ts_recv=0)
    assert len(out) == 1
    assert len(out[0].bids) == 1 and len(out[0].asks) == 1
```

## Landmines

- **Time format:** Coinbase emits ISO-8601 UTC with `Z` suffix. `datetime.fromisoformat` requires `+00:00` in Python <3.11 mode; we standardize via the `_iso_to_ns` helper.
- **`level2` rate-of-change:** Coinbase L2 deltas can be very large during volatility. `max_size=8MB` on the WS connection prevents disconnects.
- **Heartbeat:** subscribe to `heartbeats` channel separately if you need explicit liveness; otherwise rely on WS ping/pong.
- **Auth:** public channels (market_trades, level2) work without auth. Authenticated channels (e.g., `user`) require ECDSA signing — defer to Phase H.
- **Symbol mapping:** Coinbase uses `BTC-USD` natively, identical to canonical. No transformation needed; do not double-transform.

## Out of scope

- Authenticated channels (orders, balances) — Phase H (TASK-075).
- Coinbase Exchange (legacy pro) WS protocol — different format, not needed.
- Backfill via REST — Coinbase Advanced Trade has limited public history; rely on TASK-015 (EOD) for daily coverage.

## Done when

- [ ] `services/ingestor/src/ingestor/coinbase.py` exists
- [ ] `pytest services/ingestor/tests/test_coinbase_normalize.py` is green
- [ ] `mypy services/ingestor` is green
- [ ] Manual smoke: `python -m ingestor.main --venue coinbase --symbols BTC-USD` produces ≥1 trade and ≥1 book event in stdout within 30s
- [ ] Cross-venue spread (Binance vs Coinbase) computable in TASK-014 once both adapters run
