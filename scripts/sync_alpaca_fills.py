"""
sync_alpaca_fills.py - pull realised FILL activities from Alpaca.

Fetches the most recent fills from ``/v2/account/activities``,
computes today's volume / notional / realised P&L (FIFO on average-
cost basis), and caches the raw activities in Redis under
``fills:alpaca.live`` so a future dashboard view can surface them.

Run from the repo root::

    uv run python scripts/sync_alpaca_fills.py            # last 100 fills
    uv run python scripts/sync_alpaca_fills.py --date 2026-04-29

The realised P&L math uses a simple running average-cost model:

  - ``buy`` at price ``p`` qty ``q`` : avg = (avg*held + p*q) / (held+q)
  - ``sell`` at price ``p`` qty ``q`` : realised += (p - avg) * q

This matches how most retail brokers display realised P&L and avoids
FIFO lot tracking.  When the OMS audit log gets populated with our own
fills, that becomes the canonical source; for the Alpaca mirror this
simple projection is enough.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from decimal import Decimal
from typing import Any

import httpx
from redis.asyncio import Redis

from fincept_core.config import get_settings
from oms.alpaca.client import AlpacaClient, AlpacaError

STRATEGY_ID = "alpaca.live"
FILLS_KEY = f"fills:{STRATEGY_ID}"


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def compute_realised(
    activities: list[dict[str, Any]],
) -> dict[str, dict[str, Decimal]]:
    """Project realised P&L per symbol using running average cost.

    Alpaca returns newest-first; reverse to chronological so the running
    cost evolves correctly.
    """
    chronological = list(reversed(activities))
    state: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"qty": Decimal(0), "avg": Decimal(0), "realised": Decimal(0)}
    )
    for act in chronological:
        if act.get("activity_type") != "FILL":
            continue
        symbol = str(act.get("symbol", ""))
        side = str(act.get("side", ""))
        qty = _dec(act.get("qty"))
        price = _dec(act.get("price"))
        if qty <= 0 or price <= 0 or not symbol:
            continue
        s = state[symbol]
        if side == "buy":
            total_cost = s["avg"] * s["qty"] + price * qty
            s["qty"] += qty
            s["avg"] = total_cost / s["qty"] if s["qty"] > 0 else Decimal(0)
        elif side == "sell":
            close_qty = min(qty, s["qty"])
            if close_qty > 0:
                s["realised"] += (price - s["avg"]) * close_qty
                s["qty"] -= close_qty
            # Residual sell past flat opens a short; tracked symmetrically.
            leftover = qty - close_qty
            if leftover > 0:
                # Flip to short book: avg becomes sell price.
                s["avg"] = price
                s["qty"] = -leftover
    return dict(state)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="ISO YYYY-MM-DD; omit for recent")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
        print("ERROR: Alpaca credentials not set in .env", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=settings.ALPACA_BASE_URL, timeout=30.0) as http:
        client = AlpacaClient(
            http=http,
            api_key=settings.ALPACA_API_KEY,
            api_secret=settings.ALPACA_API_SECRET,
        )
        try:
            activities = await client.list_activities(
                activity_types="FILL",
                date=args.date,
                page_size=args.limit,
            )
        except AlpacaError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

    if not activities:
        print("No fills returned.")
        return 0

    print(f"Fetched {len(activities)} FILL activity record(s).")
    realised = compute_realised(activities)

    # Persist raw activities in Redis (newest-first, capped) for future UI.
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    try:
        pipe = redis.pipeline()
        pipe.delete(FILLS_KEY)
        for act in activities:
            pipe.rpush(FILLS_KEY, json.dumps(act))
        pipe.expire(FILLS_KEY, 86400)
        await pipe.execute()
    finally:
        await redis.aclose()  # type: ignore[attr-defined]

    # Print realised P&L summary.
    print("\nRealised P&L by symbol (running avg-cost projection):")
    print(f"  {'SYMBOL':<10} {'NET QTY':>10} {'AVG COST':>12} {'REALISED':>14}")
    total = Decimal(0)
    for symbol in sorted(realised):
        row = realised[symbol]
        total += row["realised"]
        sign = "+" if row["realised"] >= 0 else ""
        print(
            f"  {symbol:<10} {row['qty']!s:>10} "
            f"{'$' + str(row['avg']):>12} "
            f"{sign + '$' + str(row['realised']):>14}"
        )
    sign = "+" if total >= 0 else ""
    print(f"  {'TOTAL':<10} {'':>10} {'':>12} {sign + '$' + str(total):>14}")
    print(f"\nCached raw fills at Redis key '{FILLS_KEY}' (TTL 24h).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
