# TASK-013 · Kraken WebSocket adapter

**Phase:** D · **Depends on:** TASK-010 · **Blocks:** TASK-014 (cross-venue quality checks)

## Goal

Kraken v2 WebSocket adapter implementing `VenueAdapter`, normalizing trade + book channels to canonical `TradeEvent` and `BookDeltaEvent` / `BookSnapshotEvent`.

## Files to create

```
services/ingestor/src/ingestor/kraken.py
services/ingestor/tests/test_kraken_normalize.py
```

## Contracts

### `kraken.py`

```python
import orjson, websockets
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator
from pydantic import BaseModel
from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    Venue, AssetClass, TradeEvent, BookDeltaEvent, BookSnapshotEvent, BookLevel, Side,
)
from .base import VenueAdapter

log = get_logger(__name__)
WS_URL = "wss://ws.kraken.com/v2"

# Kraken uses XBT for BTC and slashes for the pair separator: "BTC/USD" → canonical "BTC-USD".
# Some legacy pairs use XBT; the normalizer maps them.
def to_canonical(sym: str) -> str:
    s = sym.upper().replace("XBT", "BTC")
    return s.replace("/", "-")

def to_venue(sym: str) -> str:
    return sym.upper().replace("BTC", "XBT").replace("-", "/")

class KrakenAdapter(VenueAdapter):
    venue = Venue.KRAKEN

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(WS_URL, ping_interval=10, ping_timeout=10, max_size=8 * 1024 * 1024)
        sub_trade = {
            "method": "subscribe",
            "params": {"channel": "trade", "symbol": [to_venue(s) for s in self.symbols]},
        }
        sub_book = {
            "method": "subscribe",
            "params": {"channel": "book", "symbol": [to_venue(s) for s in self.symbols], "depth": 100, "snapshot": True},
        }
        await self._ws.send(orjson.dumps(sub_trade).decode())
        await self._ws.send(orjson.dumps(sub_book).decode())
        log.info("kraken.connected", symbols=self.symbols)

    async def stream(self) -> AsyncIterator[BaseModel]:
        assert self._ws is not None
        async for raw in self._ws:
            msg = orjson.loads(raw)
            ch = msg.get("channel")
            mtype = msg.get("type")
            ts_recv = now_ns()
            if ch == "trade" and mtype in ("snapshot", "update"):
                for ev in self._parse_trades(msg, ts_recv):
                    yield ev
            elif ch == "book":
                for ev in self._parse_book(msg, ts_recv):
                    yield ev

    def _parse_trades(self, msg: dict, ts_recv: int) -> list[TradeEvent]:
        out: list[TradeEvent] = []
        for tr in msg.get("data", []):
            out.append(TradeEvent(
                venue=Venue.KRAKEN,
                symbol=to_canonical(tr["symbol"]),
                asset_class=AssetClass.CRYPTO_SPOT,
                ts_event=int(datetime.fromisoformat(tr["timestamp"].replace("Z", "+00:00")).timestamp() * 1e9),
                ts_recv=ts_recv,
                seq=int(tr.get("trade_id", 0)),
                price=Decimal(str(tr["price"])),
                size=Decimal(str(tr["qty"])),
                side=Side.BUY if str(tr["side"]).lower() == "buy" else Side.SELL,
            ))
        return out

    def _parse_book(self, msg: dict, ts_recv: int) -> list[BaseModel]:
        out: list[BaseModel] = []
        mtype = msg.get("type")
        for entry in msg.get("data", []):
            symbol = to_canonical(entry["symbol"])
            ts_event = int(datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp() * 1e9) \
                if "timestamp" in entry else ts_recv
            if mtype == "snapshot":
                bids = [BookLevel(price=Decimal(str(b["price"])), size=Decimal(str(b["qty"])))
                        for b in entry.get("bids", [])]
                asks = [BookLevel(price=Decimal(str(a["price"])), size=Decimal(str(a["qty"])))
                        for a in entry.get("asks", [])]
                out.append(BookSnapshotEvent(
                    venue=Venue.KRAKEN, symbol=symbol, asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=ts_event, ts_recv=ts_recv, bids=bids, asks=asks,
                ))
            elif mtype == "update":
                bids_add: list[BookLevel] = []
                bids_rm: list[Decimal] = []
                asks_add: list[BookLevel] = []
                asks_rm: list[Decimal] = []
                for b in entry.get("bids", []):
                    p, q = Decimal(str(b["price"])), Decimal(str(b["qty"]))
                    if q == 0:
                        bids_rm.append(p)
                    else:
                        bids_add.append(BookLevel(price=p, size=q))
                for a in entry.get("asks", []):
                    p, q = Decimal(str(a["price"])), Decimal(str(a["qty"]))
                    if q == 0:
                        asks_rm.append(p)
                    else:
                        asks_add.append(BookLevel(price=p, size=q))
                out.append(BookDeltaEvent(
                    venue=Venue.KRAKEN, symbol=symbol, asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=ts_event, ts_recv=ts_recv,
                    bids_add=bids_add, bids_remove=bids_rm,
                    asks_add=asks_add, asks_remove=asks_rm,
                ))
        return out

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
```

## Tests

### `tests/test_kraken_normalize.py`

```python
from decimal import Decimal
from ingestor.kraken import KrakenAdapter, to_canonical, to_venue
from fincept_core.schemas import Side

def test_xbt_to_btc():
    assert to_canonical("XBT/USD") == "BTC-USD"
    assert to_venue("BTC-USD") == "XBT/USD"

def test_parse_trades():
    a = KrakenAdapter(["BTC-USD"])
    msg = {
        "channel": "trade",
        "type": "update",
        "data": [{
            "symbol": "XBT/USD",
            "side": "buy",
            "price": 100000.5,
            "qty": 0.123,
            "ord_type": "market",
            "trade_id": 12345,
            "timestamp": "2024-12-01T12:00:00.123Z",
        }],
    }
    out = a._parse_trades(msg, ts_recv=0)
    assert len(out) == 1
    assert out[0].symbol == "BTC-USD"
    assert out[0].price == Decimal("100000.5")
    assert out[0].side == Side.BUY
```

## Landmines

- **`XBT` vs `BTC`:** Kraken's legacy ticker for Bitcoin is `XBT`. Always normalize through `to_canonical`. Tests must explicitly cover this.
- **Decimal-from-float:** Kraken sends prices as JSON numbers. `Decimal(0.1)` is **wrong** (binary float artifact); always go through `Decimal(str(x))`.
- **Snapshot ordering:** Kraken sends snapshot first, then deltas. The writer (TASK-010) must handle them in order; do not parallelize parsing across messages of the same symbol.
- **Connection limits:** Kraken caps subscriptions per connection. With >50 symbols, shard across multiple connections.
- **Pair format:** must use `XBT/USD` not `XBTUSD`.

## Out of scope

- Kraken Futures (separate WS endpoint) — covered by a future TASK if/when crypto perp is needed.
- Authenticated channels — Phase H.
- Historical OHLC backfill via REST — TASK-015 covers EOD; intraday backfill is venue-specific and deferred.

## Done when

- [ ] `services/ingestor/src/ingestor/kraken.py` exists
- [ ] `pytest services/ingestor/tests/test_kraken_normalize.py` is green
- [ ] `mypy services/ingestor` is green
- [ ] Manual smoke: `python -m ingestor.main --venue kraken --symbols BTC-USD` produces trades + book events
- [ ] Cross-venue spread (Binance/Coinbase/Kraken) becomes computable in TASK-014
