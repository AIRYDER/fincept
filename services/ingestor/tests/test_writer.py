"""Tests for ingestor.writer — fan-out to Redis + batched DB writes.

Uses ``fakeredis.aioredis`` for the Redis side and patches
``write_trades`` / ``write_book_deltas`` to avoid Postgres.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    BookLevel,
    Side,
    TradeEvent,
    Venue,
)
from ingestor.writer import Writer


def _trade(seq: int, ts: int = 1_700_000_000_000_000_000) -> TradeEvent:
    return TradeEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USDT",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts + 1,
        seq=seq,
        price=Decimal("30000.50"),
        size=Decimal("0.01"),
        side=Side.BUY,
    )


def _book(seq: int, ts: int = 1_700_000_000_000_000_000) -> BookDeltaEvent:
    return BookDeltaEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USDT",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts + 1,
        seq=seq,
        bids_add=[BookLevel(price=Decimal("30000"), size=Decimal("0.5"))],
    )


@pytest.mark.asyncio
async def test_writer_publishes_trade_to_redis_stream() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=10, persist_to_db=False)

    await writer.handle(_trade(1))

    msgs = await redis.xrange("md.trades")  # type: ignore[attr-defined]
    assert len(msgs) == 1
    fields = msgs[0][1]
    # ``serialize`` puts the event_id, published_at, type and payload fields.
    keys = {k.decode() if isinstance(k, bytes) else k for k in fields}
    assert {"event_id", "published_at", "type", "payload"} <= keys


@pytest.mark.asyncio
async def test_writer_publishes_book_delta_to_books_stream() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=10, persist_to_db=False)

    await writer.handle(_book(1))

    trades = await redis.xrange("md.trades")  # type: ignore[attr-defined]
    books = await redis.xrange("md.books")  # type: ignore[attr-defined]
    assert trades == []
    assert len(books) == 1


@pytest.mark.asyncio
async def test_writer_flushes_trade_buffer_at_batch_size() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=3)

    with patch(
        "ingestor.writer.write_trades", new_callable=AsyncMock, return_value=3
    ) as mock_write:
        for i in range(2):
            await writer.handle(_trade(i))
        # Buffer at 2/3 — no flush yet.
        assert writer.pending == (2, 0)
        mock_write.assert_not_awaited()

        await writer.handle(_trade(2))
        # Threshold reached — flush triggered, buffer drained.
        assert writer.pending == (0, 0)
        mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_writer_flushes_book_buffer_at_batch_size() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=2)

    with patch(
        "ingestor.writer.write_book_deltas", new_callable=AsyncMock, return_value=2
    ) as mock_write:
        await writer.handle(_book(1))
        await writer.handle(_book(2))
        mock_write.assert_awaited_once()
        assert writer.pending == (0, 0)


@pytest.mark.asyncio
async def test_writer_flush_drains_partial_buffers() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=100)  # high threshold so nothing auto-flushes

    with (
        patch(
            "ingestor.writer.write_trades", new_callable=AsyncMock, return_value=2
        ) as mock_trades,
        patch(
            "ingestor.writer.write_book_deltas", new_callable=AsyncMock, return_value=1
        ) as mock_books,
    ):
        await writer.handle(_trade(1))
        await writer.handle(_trade(2))
        await writer.handle(_book(1))
        assert writer.pending == (2, 1)

        await writer.flush()
        mock_trades.assert_awaited_once()
        mock_books.assert_awaited_once()
        assert writer.pending == (0, 0)


@pytest.mark.asyncio
async def test_writer_persist_to_db_false_skips_db() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=1, persist_to_db=False)

    with (
        patch("ingestor.writer.write_trades", new_callable=AsyncMock) as mock_trades,
        patch("ingestor.writer.write_book_deltas", new_callable=AsyncMock) as mock_books,
    ):
        await writer.handle(_trade(1))
        await writer.handle(_book(1))
        await writer.flush()
        mock_trades.assert_not_awaited()
        mock_books.assert_not_awaited()


def test_writer_batch_size_must_be_positive() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    with pytest.raises(ValueError, match="batch_size"):
        Writer(redis, batch_size=0)
