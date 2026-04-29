"""
fincept_db.universe — read helpers for the ``universe`` table.

The ``universe`` table is operator-curated (managed via migrations or an
admin tool) — there's no ingestor that writes to it, so this module is
read-only.  ``read_universe`` returns the active rows; consumers filter
by asset class as needed.
"""

from __future__ import annotations

from sqlalchemy import select

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
