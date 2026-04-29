"""
ingestor.base — VenueAdapter ABC.

Every venue adapter (Binance, Coinbase, Kraken, ...) implements this
interface.  ``stream()`` yields canonical Pydantic events
(``TradeEvent | BookDeltaEvent | BookSnapshotEvent``).  The orchestration in
``ingestor.main`` consumes the stream and routes events to the ``Writer``.

Adapters MUST:
  - keep ``self.symbols`` as the canonical list (e.g. ``["BTC-USDT"]``);
  - convert to venue format internally;
  - tag every yielded event with ``ts_recv = now_ns()`` so the quality
    monitor can compute exchange-to-process latency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic import BaseModel

from fincept_core.schemas import Venue


class VenueAdapter(ABC):
    """Abstract base for per-venue WebSocket adapters."""

    venue: Venue

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    @abstractmethod
    async def connect(self) -> None:
        """Open the WebSocket and subscribe to streams for ``self.symbols``."""

    @abstractmethod
    def stream(self) -> AsyncIterator[BaseModel]:
        """Async generator that yields canonical events.

        Implementations are sync ``def stream``-and-return-async-iterator OR
        ``async def stream`` with ``yield``; both satisfy the protocol because
        we only require ``__aiter__`` on the returned object.  Concrete
        adapters use ``async def`` + ``yield``.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the WebSocket cleanly.  Must be idempotent."""
