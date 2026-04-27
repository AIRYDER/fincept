from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fincept_core.schemas import (
    AssetClass,
    BookDeltaEvent,
    BookLevel,
    Side,
    TradeEvent,
    Venue,
)

from .engine import session_scope
from .models import BookDelta, Trade


async def write_trades(events: Iterable[TradeEvent]) -> int:
    rows = [
        {
            "venue": event.venue.value,
            "symbol": event.symbol,
            "ts_event": event.ts_event,
            "seq": event.seq if event.seq is not None else 0,
            "asset_class": event.asset_class.value,
            "ts_recv": event.ts_recv,
            "price": event.price,
            "size": event.size,
            "side": event.side.value if event.side is not None else None,
        }
        for event in events
    ]
    if not rows:
        return 0
    async with session_scope() as session:
        stmt = pg_insert(Trade).values(rows).on_conflict_do_nothing(
            index_elements=["venue", "symbol", "ts_event", "seq"]
        )
        result = cast("CursorResult[Any]", await session.execute(stmt))
        return int(result.rowcount or 0)


async def read_trades(
    symbol: str,
    start_ns: int,
    end_ns: int,
    venue: str | None = None,
) -> list[TradeEvent]:
    async with session_scope() as session:
        query = (
            select(Trade)
            .where(Trade.symbol == symbol)
            .where(Trade.ts_event >= start_ns)
            .where(Trade.ts_event < end_ns)
        )
        if venue is not None:
            query = query.where(Trade.venue == venue)
        query = query.order_by(Trade.ts_event, Trade.seq)
        rows = (await session.execute(query)).scalars().all()
        return [
            TradeEvent(
                venue=Venue(row.venue),
                symbol=row.symbol,
                asset_class=AssetClass(row.asset_class),
                ts_event=row.ts_event,
                ts_recv=row.ts_recv,
                seq=row.seq,
                price=Decimal(row.price),
                size=Decimal(row.size),
                side=Side(row.side) if row.side is not None else None,
            )
            for row in rows
        ]


def _payload_to_jsonable(event: BookDeltaEvent) -> dict[str, Any]:
    return {
        "bids_add": [{"price": str(level.price), "size": str(level.size)} for level in event.bids_add],
        "bids_remove": [str(price) for price in event.bids_remove],
        "asks_add": [{"price": str(level.price), "size": str(level.size)} for level in event.asks_add],
        "asks_remove": [str(price) for price in event.asks_remove],
    }


def _payload_from_jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "bids_add": [BookLevel(price=Decimal(level["price"]), size=Decimal(level["size"])) for level in payload.get("bids_add", [])],
        "bids_remove": [Decimal(price) for price in payload.get("bids_remove", [])],
        "asks_add": [BookLevel(price=Decimal(level["price"]), size=Decimal(level["size"])) for level in payload.get("asks_add", [])],
        "asks_remove": [Decimal(price) for price in payload.get("asks_remove", [])],
    }


async def write_book_deltas(events: Iterable[BookDeltaEvent]) -> int:
    rows = [
        {
            "venue": event.venue.value,
            "symbol": event.symbol,
            "ts_event": event.ts_event,
            "seq": event.seq if event.seq is not None else 0,
            "asset_class": event.asset_class.value,
            "ts_recv": event.ts_recv,
            "payload": _payload_to_jsonable(event),
        }
        for event in events
    ]
    if not rows:
        return 0
    async with session_scope() as session:
        stmt = pg_insert(BookDelta).values(rows).on_conflict_do_nothing(
            index_elements=["venue", "symbol", "ts_event", "seq"]
        )
        result = cast("CursorResult[Any]", await session.execute(stmt))
        return int(result.rowcount or 0)


async def read_book_deltas(
    symbol: str,
    start_ns: int,
    end_ns: int,
    venue: str | None = None,
) -> list[BookDeltaEvent]:
    async with session_scope() as session:
        query = (
            select(BookDelta)
            .where(BookDelta.symbol == symbol)
            .where(BookDelta.ts_event >= start_ns)
            .where(BookDelta.ts_event < end_ns)
        )
        if venue is not None:
            query = query.where(BookDelta.venue == venue)
        query = query.order_by(BookDelta.ts_event, BookDelta.seq)
        rows = (await session.execute(query)).scalars().all()
        return [
            BookDeltaEvent(
                venue=Venue(row.venue),
                symbol=row.symbol,
                asset_class=AssetClass(row.asset_class),
                ts_event=row.ts_event,
                ts_recv=row.ts_recv,
                seq=row.seq,
                **_payload_from_jsonable(row.payload),
            )
            for row in rows
        ]
