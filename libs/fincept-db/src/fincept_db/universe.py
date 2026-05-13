"""
fincept_db.universe — read helpers for the ``universe`` table.

The ``universe`` table is operator-curated (managed via migrations or an
admin tool) — there's no ingestor that writes to it, so this module is
read-only.  ``read_universe`` returns the active rows; consumers filter
by asset class as needed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .engine import session_scope
from .models import UniverseSymbol


async def read_universe(
    *,
    asset_class: str | None = None,
    active_only: bool = True,
) -> list[dict[str, object]]:
    """Return universe rows as plain dicts (API-friendly).

    Returning dicts (rather than ORM rows) keeps the API layer free of
    SQLAlchemy imports and lets the caller serialise to JSON without
    extra mapping.  Each dict has ``symbol``, ``asset_class``,
    ``venue_default``, ``active`` keys.
    """
    async with session_scope() as session:
        query = select(UniverseSymbol)
        if asset_class is not None:
            query = query.where(UniverseSymbol.asset_class == asset_class)
        if active_only:
            query = query.where(UniverseSymbol.active.is_(True))
        query = query.order_by(UniverseSymbol.symbol)
        rows = (await session.execute(query)).scalars().all()
        return [
            {
                "symbol": row.symbol,
                "asset_class": row.asset_class,
                "venue_default": row.venue_default,
                "active": row.active,
            }
            for row in rows
        ]


async def upsert_universe_symbols(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not rows:
        return []
    values_by_symbol: dict[str, dict[str, object]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        values_by_symbol[symbol] = {
            "symbol": symbol,
            "asset_class": str(row.get("asset_class") or "equity"),
            "venue_default": str(row.get("venue_default") or "alpaca"),
            "active": bool(row.get("active", True)),
        }
    values = list(values_by_symbol.values())
    if not values:
        return []
    async with session_scope() as session:
        stmt = pg_insert(UniverseSymbol).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "active": stmt.excluded.active,
            },
        )
        await session.execute(stmt)
    return await read_universe(active_only=False)
