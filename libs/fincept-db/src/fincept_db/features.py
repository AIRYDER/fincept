"""
fincept_db.features — read/write helpers for the ``features`` table.

Mirrors the shape of ``bars.py``: idempotent ``write_features`` via
PostgreSQL's ``ON CONFLICT DO UPDATE`` (re-running backfill replaces values
for the same primary key), plus range and latest-as-of readers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from fincept_core.schemas import FeatureFrame

from .engine import session_scope
from .models import Feature


async def write_features(frames: Iterable[FeatureFrame]) -> int:
    """Upsert one row per ``(symbol, freq, ts_event)``.

    Replacing-on-conflict is intentional: if you fix a transform bug and
    re-run the backfill, the new values overwrite the old ones for the
    same input bars (spec landmine in TASK-017).  Always backfill into a
    sandbox schema first if you're unsure.
    """
    rows = []
    for frame in frames:
        payload = frame.model_dump(mode="python")
        rows.append(
            {
                "symbol": frame.symbol,
                "freq": frame.freq,
                "ts_event": frame.ts_event,
                "values": payload["values"],
                "tags": frame.tags,
            }
        )
    if not rows:
        return 0
    async with session_scope() as session:
        stmt = pg_insert(Feature).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "freq", "ts_event"],
            set_={
                "values": stmt.excluded["values"],
                "tags": stmt.excluded.tags,
            },
        )
        result = cast("CursorResult[Any]", await session.execute(stmt))
        return int(result.rowcount or 0)


async def read_features(
    symbol: str,
    freq: str,
    start_ns: int,
    end_ns: int,
) -> list[FeatureFrame]:
    """Return all FeatureFrames in the half-open ``[start_ns, end_ns)`` window."""
    async with session_scope() as session:
        query = (
            select(Feature)
            .where(Feature.symbol == symbol)
            .where(Feature.freq == freq)
            .where(Feature.ts_event >= start_ns)
            .where(Feature.ts_event < end_ns)
            .order_by(Feature.ts_event)
        )
        rows = (await session.execute(query)).scalars().all()
        return [
            FeatureFrame(
                symbol=row.symbol,
                ts_event=row.ts_event,
                freq=row.freq,
                values=row.values,
                tags=row.tags or {},
            )
            for row in rows
        ]


async def read_latest_feature(
    symbol: str,
    freq: str,
    as_of_ns: int,
) -> FeatureFrame | None:
    """Return the most recent FeatureFrame with ``ts_event <= as_of_ns``.

    This is the building block for PIT joins — strict ``<=`` semantics so a
    bar at time T sees the feature whose ``ts_event`` matches T (the bar's
    own feature) but never one in the future.
    """
    async with session_scope() as session:
        query = (
            select(Feature)
            .where(Feature.symbol == symbol)
            .where(Feature.freq == freq)
            .where(Feature.ts_event <= as_of_ns)
            .order_by(Feature.ts_event.desc())
            .limit(1)
        )
        row = (await session.execute(query)).scalars().first()
        if row is None:
            return None
        return FeatureFrame(
            symbol=row.symbol,
            ts_event=row.ts_event,
            freq=row.freq,
            values=row.values,
            tags=row.tags or {},
        )
