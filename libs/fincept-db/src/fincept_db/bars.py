from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fincept_core.schemas import AssetClass, BarEvent, Venue

from .engine import session_scope
from .models import Bar


async def write_bars(events: Iterable[BarEvent]) -> int:
    rows = [
        {
            "venue": event.venue.value,
            "symbol": event.symbol,
            "freq": event.freq,
            "ts_event": event.ts_event,
            "asset_class": event.asset_class.value,
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "trades": event.trades,
            "vwap": event.vwap,
        }
        for event in events
    ]
    if not rows:
        return 0
    async with session_scope() as session:
        stmt = pg_insert(Bar).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["venue", "symbol", "freq", "ts_event"],
            set_={
                "asset_class": stmt.excluded.asset_class,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "trades": stmt.excluded.trades,
                "vwap": stmt.excluded.vwap,
            },
        )
        result = cast("CursorResult[Any]", await session.execute(stmt))
        return int(result.rowcount or 0)


async def read_bars(
    symbol: str,
    freq: str,
    start_ns: int,
    end_ns: int,
    venue: str | None = None,
) -> list[BarEvent]:
    async with session_scope() as session:
        query = (
            select(Bar)
            .where(Bar.symbol == symbol)
            .where(Bar.freq == freq)
            .where(Bar.ts_event >= start_ns)
            .where(Bar.ts_event < end_ns)
        )
        if venue is not None:
            query = query.where(Bar.venue == venue)
        query = query.order_by(Bar.ts_event)
        rows = (await session.execute(query)).scalars().all()
        return [
            BarEvent(
                venue=Venue(row.venue),
                symbol=row.symbol,
                asset_class=AssetClass(row.asset_class),
                ts_event=row.ts_event,
                ts_recv=row.ts_event,
                freq=row.freq,
                open=Decimal(row.open),
                high=Decimal(row.high),
                low=Decimal(row.low),
                close=Decimal(row.close),
                volume=Decimal(row.volume),
                trades=row.trades,
                vwap=Decimal(row.vwap) if row.vwap is not None else None,
            )
            for row in rows
        ]
