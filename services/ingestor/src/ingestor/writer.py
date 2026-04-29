"""
ingestor.writer — fan-out to Redis Streams + batched Timescale writes.

Every event the adapter yields is:
  1. Wrapped in a canonical ``Event`` (so consumers can branch on type).
  2. Published to the appropriate Redis stream (``md.trades``, ``md.books``).
  3. Buffered in memory; the buffer is flushed to Timescale via
     ``fincept_db.ticks.write_trades`` / ``write_book_deltas`` once it
     reaches ``batch_size`` or when ``flush()`` is called.

Why batch the DB writes?  Timescale insert throughput is dominated by
round-trip latency, not row count, so a single 500-row insert is roughly
500x cheaper than 500 single-row inserts.

Idempotency is enforced at the DB layer via ``ON CONFLICT DO NOTHING`` on
``(venue, symbol, ts_event, seq)`` — duplicate publishes from a venue
re-broadcast or a reconnect replay are silently absorbed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_BOOKS, STREAM_MD_TRADES
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import BookDeltaEvent, BookSnapshotEvent, TradeEvent
from fincept_db.ticks import write_book_deltas, write_trades

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 500


class Writer:
    """Fan-out writer with in-memory batching for Timescale persistence."""

    def __init__(
        self,
        redis: Redis[Any],
        batch_size: int = DEFAULT_BATCH_SIZE,
        *,
        persist_to_db: bool = True,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.producer = Producer(redis)
        self.batch_size = batch_size
        self.persist_to_db = persist_to_db
        self._trades: list[TradeEvent] = []
        self._books: list[BookDeltaEvent] = []

    async def handle(self, event: BaseModel) -> None:
        """Route a single canonical event to Redis + the DB buffer."""
        if isinstance(event, TradeEvent):
            await self.producer.publish(STREAM_MD_TRADES, Event(type="trade", payload=event))
            self._trades.append(event)
            if len(self._trades) >= self.batch_size:
                await self._flush_trades()
        elif isinstance(event, BookDeltaEvent):
            await self.producer.publish(STREAM_MD_BOOKS, Event(type="book_delta", payload=event))
            self._books.append(event)
            if len(self._books) >= self.batch_size:
                await self._flush_books()
        elif isinstance(event, BookSnapshotEvent):
            # Snapshots go to the same stream but aren't persisted as deltas;
            # downstream services replay snapshots into book state directly.
            await self.producer.publish(STREAM_MD_BOOKS, Event(type="book_snapshot", payload=event))
        else:
            log.warning("writer.unknown_event_type", model=type(event).__name__)

    async def _flush_trades(self) -> None:
        if not self._trades:
            return
        if self.persist_to_db:
            inserted = await write_trades(self._trades)
            log.debug(
                "writer.trades_flushed",
                attempted=len(self._trades),
                inserted=inserted,
            )
        self._trades.clear()

    async def _flush_books(self) -> None:
        if not self._books:
            return
        if self.persist_to_db:
            inserted = await write_book_deltas(self._books)
            log.debug(
                "writer.books_flushed",
                attempted=len(self._books),
                inserted=inserted,
            )
        self._books.clear()

    async def flush(self) -> None:
        """Drain both buffers.  Call before shutting the process down."""
        await self._flush_trades()
        await self._flush_books()

    @property
    def pending(self) -> tuple[int, int]:
        """``(trades_pending, books_pending)`` — exposed for QualityMonitor."""
        return len(self._trades), len(self._books)
