"""
oms.alpaca.sync_runner — shared Alpaca → Redis position sync logic.

Used by two entrypoints:

  - ``scripts/sync_alpaca.py`` (one-shot CLI)
  - ``api.background.AlpacaScheduler`` (periodic background task)

Keeping the logic here lets both callers share one implementation and
one set of unit tests.  The function writes:

  1. ``Position`` rows to the PortfolioStore under strategy_id
     ``alpaca.live`` (so the dashboard's "Strategies" view surfaces the
     Alpaca book alongside our own).
  2. Mark prices per symbol via :mod:`oms.alpaca.marks`.

Alpaca's ``/v2/positions`` response already carries ``current_price``,
so a second data-API call is unnecessary for v1.  If we ever need sub-
minute marks (trade ticks or quote midpoints), plug in a WebSocket
subscription alongside this poller.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from redis.asyncio import Redis

from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from fincept_core.schemas import Position
from oms.alpaca.client import AlpacaClient
from oms.alpaca.marks import write_mark
from oms.alpaca.symbols import from_alpaca_symbol
from portfolio.store import PositionStore

STRATEGY_ID = "alpaca.live"

log = get_logger(__name__)


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _to_position_and_mark(raw: dict[str, Any]) -> tuple[Position, Decimal]:
    """Translate one Alpaca position payload → (Position, mark_price)."""
    symbol = from_alpaca_symbol(raw["symbol"])
    qty = _decimal(raw.get("qty"))
    # Alpaca returns positive qty + side='short' for shorts; normalise.
    if raw.get("side") == "short" and qty > 0:
        qty = -qty
    position = Position(
        strategy_id=STRATEGY_ID,
        symbol=symbol,
        quantity=qty,
        avg_cost=_decimal(raw.get("avg_entry_price")),
        realized_pnl=Decimal(0),  # not exposed on /v2/positions
        unrealized_pnl=_decimal(raw.get("unrealized_pl")),
        updated_at=now_ns(),
    )
    mark_px = _decimal(raw.get("current_price"))
    return position, mark_px


async def sync_positions_and_marks(
    *,
    redis: Redis[Any],
    api_key: str,
    api_secret: str,
    base_url: str,
) -> dict[str, Any]:
    """Pull positions from Alpaca and upsert into Redis (position + mark).

    Raises whatever ``AlpacaClient`` raises (AlpacaError, httpx errors);
    callers decide whether to log-and-continue or abort.  Returns a
    summary dict with account status, counts, and equity so operators
    can sanity-check the sync visually.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
        client = AlpacaClient(
            http=http, api_key=api_key, api_secret=api_secret
        )
        account = await client.get_account()
        positions_raw = await client.list_positions()

    store = PositionStore(redis)
    written = 0
    skipped = 0
    for raw in positions_raw:
        try:
            position, mark_px = _to_position_and_mark(raw)
        except Exception as exc:
            log.warning(
                "alpaca.sync.skip",
                symbol=raw.get("symbol"),
                error=str(exc),
            )
            skipped += 1
            continue
        await store.put(position)
        if mark_px > 0:
            await write_mark(redis, position.symbol, mark_px)
        written += 1

    return {
        "account_status": account.get("status"),
        "equity": account.get("equity"),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "fetched": len(positions_raw),
        "written": written,
        "skipped": skipped,
    }
