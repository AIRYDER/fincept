"""
ingestor.coinbase — Coinbase Advanced Trade WebSocket adapter.

Subscribes to the ``market_trades`` and ``level2`` public channels (no auth
required for market data).  Yields canonical ``TradeEvent``,
``BookDeltaEvent``, and ``BookSnapshotEvent`` instances.

The L2 channel delivers an initial ``snapshot`` followed by ``update``
messages.  Downstream (the ``Writer`` + book-state consumers) is
responsible for reconciling them in arrival order.

Reconnect, backoff, and book-state recovery are orchestrated by
``ingestor.main`` — this module stays thin.
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
from ingestor.normalizer import iso8601_to_ns, to_coinbase_symbol

log = get_logger(__name__)

WS_URL = "wss://advanced-trade-ws.coinbase.com"
PING_INTERVAL_S = 10.0
PING_TIMEOUT_S = 10.0
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB; L2 snapshots can be large.


class CoinbaseAdapter(VenueAdapter):
    """Coinbase Advanced Trade adapter for public market-data channels."""

    venue = Venue.COINBASE

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: Any = None  # websockets.WebSocketClientProtocol; Any avoids stub drift.

    async def connect(self) -> None:
        if not self.symbols:
            raise ValueError("CoinbaseAdapter requires at least one symbol")
        self._ws = await websockets.connect(
            WS_URL,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            max_size=MAX_FRAME_BYTES,
        )
        product_ids = [to_coinbase_symbol(s) for s in self.symbols]
        # Public channels do not require auth.  Each subscribe is a separate
        # frame; sending two subscribes on the same connection is the
        # documented Coinbase pattern.
        for channel in ("market_trades", "level2"):
            await self._ws.send(
                orjson.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": product_ids,
                        "channel": channel,
                    }
                ).decode()
            )
        log.info("coinbase.connected", symbols=len(self.symbols), channels=2)

    async def stream(self) -> AsyncIterator[TradeEvent | BookDeltaEvent | BookSnapshotEvent]:
        if self._ws is None:
            raise RuntimeError("CoinbaseAdapter.connect() must be awaited before stream()")
        async for raw in self._ws:
            ts_recv = now_ns()
            try:
                msg = cast(dict[str, Any], orjson.loads(raw))
            except orjson.JSONDecodeError:
                log.warning("coinbase.json_decode_failed", raw_len=len(raw) if raw else 0)
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
        """Dispatch by ``channel``.  Heartbeats / subscribe-acks return []."""
        channel = msg.get("channel")
        if channel == "market_trades":
            return list(cls._parse_trades(msg, ts_recv))
        if channel == "l2_data":
            return list(cls._parse_l2(msg, ts_recv))
        return []

    @staticmethod
    def _parse_trades(
        msg: dict[str, Any],
        ts_recv: int,
    ) -> list[TradeEvent]:
        """Coinbase ``market_trades`` → one ``TradeEvent`` per trade in the batch."""
        out: list[TradeEvent] = []
        for evt in msg.get("events", []) or []:
            for tr in evt.get("trades", []) or []:
                out.append(
                    TradeEvent(
                        venue=Venue.COINBASE,
                        symbol=str(tr["product_id"]).upper(),
                        asset_class=AssetClass.CRYPTO_SPOT,
                        ts_event=iso8601_to_ns(str(tr.get("time", ""))),
                        ts_recv=ts_recv,
                        seq=int(tr["trade_id"]) if tr.get("trade_id") else None,
                        price=Decimal(str(tr["price"])),
                        size=Decimal(str(tr["size"])),
                        side=Side.BUY if str(tr["side"]).upper() == "BUY" else Side.SELL,
                    )
                )
        return out

    @staticmethod
    def _parse_l2(
        msg: dict[str, Any],
        ts_recv: int,
    ) -> list[BookDeltaEvent | BookSnapshotEvent]:
        """Coinbase ``l2_data`` → snapshot (full book) or delta (upserts+removals)."""
        out: list[BookDeltaEvent | BookSnapshotEvent] = []
        envelope_ts = iso8601_to_ns(str(msg.get("timestamp", "")))
        for evt in msg.get("events", []) or []:
            etype = evt.get("type")
            symbol = str(evt["product_id"]).upper()
            ts_event = iso8601_to_ns(str(evt["time"])) if evt.get("time") else envelope_ts
            updates = evt.get("updates", []) or []

            if etype == "snapshot":
                bids: list[BookLevel] = []
                asks: list[BookLevel] = []
                for u in updates:
                    level = BookLevel(
                        price=Decimal(str(u["price_level"])),
                        size=Decimal(str(u["new_quantity"])),
                    )
                    # Coinbase uses "bid" and "offer" (not "ask").
                    if str(u["side"]).lower() == "bid":
                        bids.append(level)
                    else:
                        asks.append(level)
                out.append(
                    BookSnapshotEvent(
                        venue=Venue.COINBASE,
                        symbol=symbol,
                        asset_class=AssetClass.CRYPTO_SPOT,
                        ts_event=ts_event,
                        ts_recv=ts_recv,
                        bids=bids,
                        asks=asks,
                    )
                )
            elif etype == "update":
                bids_add: list[BookLevel] = []
                bids_remove: list[Decimal] = []
                asks_add: list[BookLevel] = []
                asks_remove: list[Decimal] = []
                for u in updates:
                    p = Decimal(str(u["price_level"]))
                    q = Decimal(str(u["new_quantity"]))
                    is_bid = str(u["side"]).lower() == "bid"
                    if q == 0:
                        (bids_remove if is_bid else asks_remove).append(p)
                    else:
                        (bids_add if is_bid else asks_add).append(BookLevel(price=p, size=q))
                out.append(
                    BookDeltaEvent(
                        venue=Venue.COINBASE,
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
            # Other event types (subscriptions, heartbeats) are dropped silently.
        return out

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
