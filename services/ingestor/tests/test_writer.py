"""Tests for ingestor.writer — fan-out to Redis + batched DB writes.

Uses ``fakeredis.aioredis`` for the Redis side and patches
``write_trades`` / ``write_book_deltas`` to avoid Postgres.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from fincept_core.events import deserialize
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
async def test_writer_publishes_closed_one_minute_bar_from_trades() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=10, persist_to_db=False)
    base = 1_700_000_000_000_000_000
    next_minute = base + 60_000_000_000

    await writer.handle(
        _trade(1, ts=base).model_copy(
            update={"price": Decimal("100"), "size": Decimal("2")}
        )
    )
    await writer.handle(
        _trade(2, ts=base + 1_000_000_000).model_copy(
            update={"price": Decimal("105"), "size": Decimal("1")}
        )
    )
    # The minute closes when the first trade from a later minute arrives.
    await writer.handle(
        _trade(3, ts=next_minute).model_copy(
            update={"price": Decimal("99"), "size": Decimal("3")}
        )
    )

    msgs = await redis.xrange("md.bars.1m")  # type: ignore[attr-defined]
    assert len(msgs) == 1
    event = deserialize(msgs[0][1])
    bar = event.payload
    assert event.type == "bar"
    assert bar.symbol == "BTC-USDT"
    assert bar.freq == "1m"
    assert bar.open == Decimal("100")
    assert bar.high == Decimal("105")
    assert bar.low == Decimal("100")
    assert bar.close == Decimal("105")
    assert bar.volume == Decimal("3")
    assert bar.trades == 2
    assert bar.vwap == Decimal("101.6666666666666666666666667")


@pytest.mark.asyncio
async def test_writer_persists_closed_one_minute_bar_to_db() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=10)
    base = 1_700_000_000_000_000_000
    next_minute = base + 60_000_000_000

    with patch(
        "ingestor.writer.write_bars", new_callable=AsyncMock, return_value=1
    ) as mock_write_bars:
        await writer.handle(_trade(1, ts=base))
        await writer.handle(_trade(2, ts=next_minute))

        mock_write_bars.assert_awaited_once()
        bars = mock_write_bars.await_args.args[0]
        assert len(bars) == 1
        assert bars[0].symbol == "BTC-USDT"
        assert bars[0].freq == "1m"


@pytest.mark.asyncio
async def test_writer_flush_publishes_open_bar() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=10, persist_to_db=False)

    await writer.handle(_trade(1))
    assert await redis.xrange("md.bars.1m") == []  # type: ignore[attr-defined]

    await writer.flush()

    msgs = await redis.xrange("md.bars.1m")  # type: ignore[attr-defined]
    assert len(msgs) == 1
    event = deserialize(msgs[0][1])
    assert event.type == "bar"
    assert event.payload.close == Decimal("30000.50")


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
        patch(
            "ingestor.writer.write_bars", new_callable=AsyncMock, return_value=1
        ) as mock_bars,
    ):
        await writer.handle(_trade(1))
        await writer.handle(_trade(2))
        await writer.handle(_book(1))
        assert writer.pending == (2, 1)

        await writer.flush()
        mock_trades.assert_awaited_once()
        mock_books.assert_awaited_once()
        mock_bars.assert_awaited_once()
        assert writer.pending == (0, 0)


@pytest.mark.asyncio
async def test_writer_persist_to_db_false_skips_db() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    writer = Writer(redis, batch_size=1, persist_to_db=False)

    with (
        patch("ingestor.writer.write_trades", new_callable=AsyncMock) as mock_trades,
        patch("ingestor.writer.write_book_deltas", new_callable=AsyncMock) as mock_books,
        patch("ingestor.writer.write_bars", new_callable=AsyncMock) as mock_bars,
    ):
        await writer.handle(_trade(1))
        await writer.handle(_book(1))
        await writer.flush()
        mock_trades.assert_not_awaited()
        mock_books.assert_not_awaited()
        mock_bars.assert_not_awaited()


def test_writer_batch_size_must_be_positive() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    with pytest.raises(ValueError, match="batch_size"):
        Writer(redis, batch_size=0)
