"""
ingestor.kraken — Kraken v2 WebSocket adapter.

Subscribes to the public ``trade`` and ``book`` channels on
``wss://ws.kraken.com/v2``.  Yields canonical ``TradeEvent``,
``BookDeltaEvent``, and ``BookSnapshotEvent`` instances.

Important Kraken-specific quirks:

  - **Symbol format:** Kraken uses ``XBT/USD`` for Bitcoin (not ``BTC/USD``).
    Conversion is centralised in :func:`ingestor.normalizer.to_kraken_symbol`
    and :func:`ingestor.normalizer.from_kraken_symbol` so the BTC ↔ XBT
    swap happens in exactly one place.

  - **Prices as JSON numbers:** Kraken sends prices/quantities as JSON
    numbers, not strings.  We always go through ``Decimal(str(x))`` to
    avoid binary-float artefacts (e.g. ``Decimal(0.1)`` ≠ ``Decimal("0.1")``).

  - **Snapshot vs update:** the message-level ``type`` field decides;
    each ``data`` entry carries the symbol and bids/asks.  Snapshot →
    ``BookSnapshotEvent``, update → ``BookDeltaEvent`` with size==0
    levels routed to the removal lists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast

import orjson
import websockets

from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    BookLevel,
    BookSnapshotEvent,
    Side,
    TradeEvent,
    Venue,
)
from ingestor.base import VenueAdapter
from ingestor.normalizer import from_kraken_symbol, iso8601_to_ns, to_kraken_symbol

log = get_logger(__name__)

WS_URL = "wss://ws.kraken.com/v2"
PING_INTERVAL_S = 10.0
PING_TIMEOUT_S = 10.0
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB; book snapshots can be sizable.
DEFAULT_BOOK_DEPTH = 100


class KrakenAdapter(VenueAdapter):
    """Kraken v2 adapter for public market-data channels."""

    venue = Venue.KRAKEN

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: Any = None  # websockets.WebSocketClientProtocol; Any avoids stub drift.

    async def connect(self) -> None:
        if not self.symbols:
            raise ValueError("KrakenAdapter requires at least one symbol")
        self._ws = await websockets.connect(
            WS_URL,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            max_size=MAX_FRAME_BYTES,
        )
        venue_symbols = [to_kraken_symbol(s) for s in self.symbols]
        await self._ws.send(
            orjson.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "trade", "symbol": venue_symbols},
                }
            ).decode()
        )
        await self._ws.send(
            orjson.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol": venue_symbols,
                        "depth": DEFAULT_BOOK_DEPTH,
                        "snapshot": True,
                    },
                }
            ).decode()
        )
        log.info("kraken.connected", symbols=len(self.symbols), channels=2)

    async def stream(self) -> AsyncIterator[TradeEvent | BookDeltaEvent | BookSnapshotEvent]:
        if self._ws is None:
            raise RuntimeError("KrakenAdapter.connect() must be awaited before stream()")
        async for raw in self._ws:
            ts_recv = now_ns()
            try:
                msg = cast(dict[str, Any], orjson.loads(raw))
            except orjson.JSONDecodeError:
                log.warning("kraken.json_decode_failed", raw_len=len(raw) if raw else 0)
                continue
            for event in self._parse_envelope(msg, ts_recv):
                yield event

    # ------------------------------------------------------------------
    # Static parsers — extracted so tests can exercise them without a WS.
    # ------------------------------------------------------------------

    @classmethod
    def _parse_envelope(
        cls,
        msg: dict[str, Any],
        ts_recv: int,
    ) -> list[TradeEvent | BookDeltaEvent | BookSnapshotEvent]:
        """Dispatch by ``channel``.  Subscription acks / heartbeats return []."""
        channel = msg.get("channel")
        mtype = msg.get("type")
        # Subscription acks (``method: "subscribe"``) and ``status``/``heartbeat``
        # frames have no ``channel``+``type`` pair — ignore them.
        if mtype not in ("snapshot", "update"):
            return []
        if channel == "trade":
            return list(cls._parse_trades(msg, ts_recv))
        if channel == "book":
            return list(cls._parse_book(msg, ts_recv))
        return []

    @staticmethod
    def _parse_trades(
        msg: dict[str, Any],
        ts_recv: int,
    ) -> list[TradeEvent]:
        """Kraken ``trade`` → one ``TradeEvent`` per entry in ``data``."""
        out: list[TradeEvent] = []
        for tr in msg.get("data", []) or []:
            trade_id = tr.get("trade_id")
            out.append(
                TradeEvent(
                    venue=Venue.KRAKEN,
                    symbol=from_kraken_symbol(str(tr["symbol"])),
                    asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=iso8601_to_ns(str(tr.get("timestamp", ""))),
                    ts_recv=ts_recv,
                    seq=int(trade_id) if trade_id else None,
                    # Kraken sends numbers as JSON floats; str() first to keep
                    # decimals exact.
                    price=Decimal(str(tr["price"])),
                    size=Decimal(str(tr["qty"])),
                    side=Side.BUY if str(tr["side"]).lower() == "buy" else Side.SELL,
                )
            )
        return out

    @staticmethod
    def _parse_book(
        msg: dict[str, Any],
        ts_recv: int,
    ) -> list[BookDeltaEvent | BookSnapshotEvent]:
        """Kraken ``book`` → snapshot (full bids/asks) or delta (upserts+removals)."""
        out: list[BookDeltaEvent | BookSnapshotEvent] = []
        is_snapshot = msg.get("type") == "snapshot"
        for entry in msg.get("data", []) or []:
            symbol = from_kraken_symbol(str(entry["symbol"]))
            ts_event = iso8601_to_ns(str(entry["timestamp"])) if entry.get("timestamp") else ts_recv

            if is_snapshot:
                bids = [
                    BookLevel(
                        price=Decimal(str(b["price"])),
                        size=Decimal(str(b["qty"])),
                    )
                    for b in entry.get("bids", []) or []
                ]
                asks = [
                    BookLevel(
                        price=Decimal(str(a["price"])),
                        size=Decimal(str(a["qty"])),
                    )
                    for a in entry.get("asks", []) or []
                ]
                out.append(
                    BookSnapshotEvent(
                        venue=Venue.KRAKEN,
                        symbol=symbol,
                        asset_class=AssetClass.CRYPTO_SPOT,
                        ts_event=ts_event,
                        ts_recv=ts_recv,
                        bids=bids,
                        asks=asks,
                    )
                )
            else:  # update
                bids_add: list[BookLevel] = []
                bids_remove: list[Decimal] = []
                asks_add: list[BookLevel] = []
                asks_remove: list[Decimal] = []
                for b in entry.get("bids", []) or []:
                    p = Decimal(str(b["price"]))
                    q = Decimal(str(b["qty"]))
                    if q == 0:
                        bids_remove.append(p)
                    else:
                        bids_add.append(BookLevel(price=p, size=q))
                for a in entry.get("asks", []) or []:
                    p = Decimal(str(a["price"]))
                    q = Decimal(str(a["qty"]))
                    if q == 0:
                        asks_remove.append(p)
                    else:
                        asks_add.append(BookLevel(price=p, size=q))
                out.append(
                    BookDeltaEvent(
                        venue=Venue.KRAKEN,
                        symbol=symbol,
                        asset_class=AssetClass.CRYPTO_SPOT,
                        ts_event=ts_event,
                        ts_recv=ts_recv,
                        bids_add=bids_add,
                        bids_remove=bids_remove,
                        asks_add=asks_add,
                        asks_remove=asks_remove,
                    )
                )
        return out

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
