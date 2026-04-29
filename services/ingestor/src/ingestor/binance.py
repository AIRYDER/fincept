"""
ingestor.binance — Binance spot WebSocket adapter.

Subscribes to ``<sym>@trade`` and ``<sym>@depth@100ms`` streams via the
combined-stream endpoint.  Yields canonical ``TradeEvent`` and
``BookDeltaEvent`` instances with ``ts_recv`` set at the moment the message
is decoded.

Reconnect, backoff, and snapshot-sync are out of scope for this module —
the orchestrator (``ingestor.main``) wraps ``connect``/``stream`` in a
retry loop.
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
    Side,
    TradeEvent,
    Venue,
)
from ingestor.base import VenueAdapter
from ingestor.normalizer import to_binance_symbol, to_canonical

log = get_logger(__name__)

WS_URL = "wss://stream.binance.com:9443/stream"
PING_INTERVAL_S = 15.0
PING_TIMEOUT_S = 10.0
MAX_FRAME_BYTES = 2**22  # 4 MiB; book updates can be large


class BinanceAdapter(VenueAdapter):
    """Binance spot adapter.  See module docstring for protocol."""

    venue = Venue.BINANCE

    def __init__(self, symbols: list[str]) -> None:
        super().__init__(symbols)
        self._ws: Any = None  # websockets.WebSocketClientProtocol; Any avoids stub drift

    async def connect(self) -> None:
        """Open the combined-stream WebSocket and subscribe to all configured symbols."""
        if not self.symbols:
            raise ValueError("BinanceAdapter requires at least one symbol")
        streams: list[str] = []
        for canonical in self.symbols:
            v = to_binance_symbol(canonical)
            streams.append(f"{v}@trade")
            streams.append(f"{v}@depth@100ms")
        url = f"{WS_URL}?streams={'/'.join(streams)}"
        self._ws = await websockets.connect(
            url,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            max_size=MAX_FRAME_BYTES,
        )
        log.info("binance.connected", streams=len(streams), symbols=len(self.symbols))

    async def stream(self) -> AsyncIterator[TradeEvent | BookDeltaEvent]:
        """Yield canonical events as raw frames arrive."""
        if self._ws is None:
            raise RuntimeError("BinanceAdapter.connect() must be awaited before stream()")

        async for raw in self._ws:
            ts_recv = now_ns()
            try:
                envelope = cast(dict[str, Any], orjson.loads(raw))
            except orjson.JSONDecodeError:
                log.warning("binance.json_decode_failed", raw_len=len(raw) if raw else 0)
                continue
            payload = cast(dict[str, Any], envelope.get("data") or {})
            event = self._parse_event(payload, ts_recv)
            if event is not None:
                yield event

    @staticmethod
    def _parse_event(
        payload: dict[str, Any],
        ts_recv: int,
    ) -> TradeEvent | BookDeltaEvent | None:
        """Convert a single Binance event dict to canonical form, or None to skip."""
        etype = payload.get("e")
        if etype == "trade":
            return _parse_trade(payload, ts_recv)
        if etype == "depthUpdate":
            return _parse_depth_update(payload, ts_recv)
        # Heartbeats and unknown event types are silently skipped — Binance
        # also sends a top-level ``stream`` envelope without an "e" field
        # for the combined-stream wrapper, which is fine.
        return None

    async def close(self) -> None:
        """Close the WS connection.  Idempotent: safe to call after a partial failure."""
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None


def _parse_trade(payload: dict[str, Any], ts_recv: int) -> TradeEvent:
    """Binance ``@trade`` envelope → canonical ``TradeEvent``."""
    # Binance: m=True iff the buyer was the market-maker, i.e. the taker SOLD.
    is_maker = bool(payload.get("m"))
    return TradeEvent(
        venue=Venue.BINANCE,
        symbol=to_canonical(str(payload["s"])),
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=int(payload["T"]) * 1_000_000,  # exchange ms → ns
        ts_recv=ts_recv,
        seq=int(payload.get("t", 0)) or None,
        price=Decimal(str(payload["p"])),
        size=Decimal(str(payload["q"])),
        side=Side.SELL if is_maker else Side.BUY,
    )


def _parse_depth_update(payload: dict[str, Any], ts_recv: int) -> BookDeltaEvent:
    """Binance ``@depth`` envelope → canonical ``BookDeltaEvent``.

    Levels with size ``0`` are removals (price-only); positive sizes are
    upserts (price + size).
    """
    bids_raw: list[list[str]] = payload.get("b", []) or []
    asks_raw: list[list[str]] = payload.get("a", []) or []
    bids_add = [
        BookLevel(price=Decimal(str(p)), size=Decimal(str(q)))
        for p, q in bids_raw
        if Decimal(str(q)) > 0
    ]
    bids_remove = [Decimal(str(p)) for p, q in bids_raw if Decimal(str(q)) == 0]
    asks_add = [
        BookLevel(price=Decimal(str(p)), size=Decimal(str(q)))
        for p, q in asks_raw
        if Decimal(str(q)) > 0
    ]
    asks_remove = [Decimal(str(p)) for p, q in asks_raw if Decimal(str(q)) == 0]
    return BookDeltaEvent(
        venue=Venue.BINANCE,
        symbol=to_canonical(str(payload["s"])),
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=int(payload["E"]) * 1_000_000,  # exchange ms → ns
        ts_recv=ts_recv,
        seq=int(payload.get("u", 0)) or None,
        bids_add=bids_add,
        bids_remove=bids_remove,
        asks_add=asks_add,
        asks_remove=asks_remove,
    )
