"""
ingestor.writer — fan-out to Redis Streams + batched Timescale writes.

Every event the adapter yields is:
  1. Wrapped in a canonical ``Event`` (so consumers can branch on type).
  2. Published to the appropriate Redis stream (``md.trades``, ``md.books``).
  3. Trades are also rolled into 1-minute OHLCV bars and published to
     ``md.bars.1m`` once the minute closes.  The online feature runner
     consumes that stream.
  4. Buffered in memory; the buffer is flushed to Timescale via
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

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_BARS_1M, STREAM_MD_BOOKS, STREAM_MD_TRADES
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    BookDeltaEvent,
    BookSnapshotEvent,
    TradeEvent,
    Venue,
)
from fincept_db.bars import write_bars
from fincept_db.ticks import write_book_deltas, write_trades

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 500
# Maximum buffer size before backpressure kicks in.  When the buffer
# exceeds this threshold, new events are dropped (oldest-first for
# trades/books) and a warning is logged.  This prevents OOM when the
# downstream DB is slower than the upstream feed.
DEFAULT_MAX_BUFFER_SIZE = 10_000
BAR_FREQ = "1m"
NS_PER_MINUTE = 60_000_000_000


@dataclass
class _MinuteBar:
    """Mutable accumulator for one (venue, symbol, minute) bucket."""

    venue: Venue
    symbol: str
    asset_class: AssetClass
    minute_start_ns: int
    ts_recv: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    notional: Decimal
    trades: int
    last_seq: int | None

    @classmethod
    def from_trade(cls, trade: TradeEvent, minute_start_ns: int) -> _MinuteBar:
        notional = trade.price * trade.size
        return cls(
            venue=trade.venue,
            symbol=trade.symbol,
            asset_class=trade.asset_class,
            minute_start_ns=minute_start_ns,
            ts_recv=trade.ts_recv,
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.size,
            notional=notional,
            trades=1,
            last_seq=trade.seq,
        )

    def add(self, trade: TradeEvent) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += trade.size
        self.notional += trade.price * trade.size
        self.trades += 1
        self.ts_recv = max(self.ts_recv, trade.ts_recv)
        self.last_seq = trade.seq

    def to_event(self) -> BarEvent:
        vwap = self.notional / self.volume if self.volume > 0 else None
        return BarEvent(
            venue=self.venue,
            symbol=self.symbol,
            asset_class=self.asset_class,
            ts_event=self.minute_start_ns,
            ts_recv=self.ts_recv,
            seq=self.last_seq,
            freq=BAR_FREQ,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            trades=self.trades,
            vwap=vwap,
        )


class Writer:
    """Fan-out writer with in-memory batching for Timescale persistence.

    Backpressure: if the downstream DB is slower than the upstream feed,
    the in-memory buffers grow.  When a buffer exceeds ``max_buffer_size``,
    the oldest events are dropped and a warning is logged.  This prevents
    OOM while preserving the most recent events (which are more likely
    to be relevant for real-time strategies).  The ``dropped`` property
    exposes the total drop count for monitoring.
    """

    def __init__(
        self,
        redis: Redis[Any],
        batch_size: int = DEFAULT_BATCH_SIZE,
        *,
        persist_to_db: bool = True,
        max_buffer_size: int = DEFAULT_MAX_BUFFER_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if max_buffer_size < batch_size:
            raise ValueError("max_buffer_size must be >= batch_size")
        self.producer = Producer(redis)
        self.batch_size = batch_size
        self.persist_to_db = persist_to_db
        self.max_buffer_size = max_buffer_size
        self._trades: list[TradeEvent] = []
        self._books: list[BookDeltaEvent] = []
        self._bars: dict[tuple[str, str], _MinuteBar] = {}
        self._dropped_trades = 0
        self._dropped_books = 0

    async def handle(self, event: BaseModel) -> None:
        """Route a single canonical event to Redis + the DB buffer."""
        if isinstance(event, TradeEvent):
            await self.producer.publish(STREAM_MD_TRADES, Event(type="trade", payload=event))
            await self._observe_trade_for_bar(event)
            self._trades.append(event)
            if len(self._trades) >= self.batch_size:
                await self._flush_trades()
            elif len(self._trades) > self.max_buffer_size:
                self._drop_oldest_trades()
        elif isinstance(event, BookDeltaEvent):
            await self.producer.publish(STREAM_MD_BOOKS, Event(type="book_delta", payload=event))
            self._books.append(event)
            if len(self._books) >= self.batch_size:
                await self._flush_books()
            elif len(self._books) > self.max_buffer_size:
                self._drop_oldest_books()
        elif isinstance(event, BookSnapshotEvent):
            # Snapshots go to the same stream but aren't persisted as deltas;
            # downstream services replay snapshots into book state directly.
            await self.producer.publish(STREAM_MD_BOOKS, Event(type="book_snapshot", payload=event))
        else:
            log.warning("writer.unknown_event_type", model=type(event).__name__)

    def _drop_oldest_trades(self) -> None:
        """Drop the oldest trades to bring the buffer under max_buffer_size.

        We drop down to ``batch_size`` so the next handle() call triggers
        a flush, draining the buffer quickly.
        """
        excess = len(self._trades) - self.batch_size
        if excess <= 0:
            return
        self._trades = self._trades[excess:]
        self._dropped_trades += excess
        log.warning(
            "writer.backpressure_dropped_trades",
            dropped=excess,
            buffer_size=len(self._trades),
            total_dropped=self._dropped_trades,
        )

    def _drop_oldest_books(self) -> None:
        """Drop the oldest book deltas to bring the buffer under max_buffer_size."""
        excess = len(self._books) - self.batch_size
        if excess <= 0:
            return
        self._books = self._books[excess:]
        self._dropped_books += excess
        log.warning(
            "writer.backpressure_dropped_books",
            dropped=excess,
            buffer_size=len(self._books),
            total_dropped=self._dropped_books,
        )

    async def _observe_trade_for_bar(self, trade: TradeEvent) -> None:
        """Roll trades into minute bars and publish closed buckets.

        The current minute remains open until the first trade from a
        later minute arrives.  ``flush()`` publishes any still-open bars
        during graceful shutdown, which is useful for tests and short
        dev runs.
        """
        minute_start = (trade.ts_event // NS_PER_MINUTE) * NS_PER_MINUTE
        key = (str(trade.venue.value), trade.symbol)
        current = self._bars.get(key)
        if current is None:
            self._bars[key] = _MinuteBar.from_trade(trade, minute_start)
            return
        if minute_start == current.minute_start_ns:
            current.add(trade)
            return
        if minute_start > current.minute_start_ns:
            await self._publish_bar(current.to_event())
            self._bars[key] = _MinuteBar.from_trade(trade, minute_start)
            return
        # Late trade for an already-closed minute.  We leave historical
        # repair to the EOD/backfill path rather than mutate emitted bars.
        log.debug(
            "writer.late_trade_ignored_for_bar",
            venue=str(trade.venue.value),
            symbol=trade.symbol,
            trade_ts=trade.ts_event,
            open_minute=current.minute_start_ns,
        )

    async def _publish_bar(self, bar: BarEvent) -> None:
        await self.producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=bar))
        if self.persist_to_db:
            inserted = await write_bars([bar])
            log.debug("writer.bars_flushed", attempted=1, inserted=inserted)

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
        for bar in list(self._bars.values()):
            await self._publish_bar(bar.to_event())
        self._bars.clear()
        await self._flush_trades()
        await self._flush_books()

    @property
    def pending(self) -> tuple[int, int]:
        """``(trades_pending, books_pending)`` — exposed for QualityMonitor."""
        return len(self._trades), len(self._books)

    @property
    def dropped(self) -> tuple[int, int]:
        """``(trades_dropped, books_dropped)`` — total events dropped by backpressure."""
        return self._dropped_trades, self._dropped_books
