"""
sync_alpaca.py - one-shot import of Alpaca paper account state.

Pulls live positions (and optionally recent orders) from your Alpaca
paper account and writes them into our local stores so the dashboard
shows them immediately.  Intended for first-time setup and ad-hoc
reconciliation; a permanent background sync is a future task.

Run from the repo root:

    uv run python scripts/sync_alpaca.py

Required env vars (or .env):

    FINCEPT_ALPACA_API_KEY
    FINCEPT_ALPACA_API_SECRET
    FINCEPT_ALPACA_BASE_URL    optional; defaults to paper-api.alpaca.markets

Positions are written under ``strategy_id = "alpaca.live"`` so they
appear as a distinct strategy on the dashboard, alongside any positions
created by our own orchestrator + OMS pipeline.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from redis.asyncio import Redis

from fincept_core.config import get_settings
from oms.alpaca.client import AlpacaError
from oms.alpaca.marks import read_marks
from oms.alpaca.sync_runner import STRATEGY_ID, sync_positions_and_marks
from portfolio.store import PositionStore


async def main() -> int:
    settings = get_settings()
    api_key = settings.ALPACA_API_KEY
    api_secret = settings.ALPACA_API_SECRET
    base_url = settings.ALPACA_BASE_URL

    if not api_key or not api_secret:
        print(
            "ERROR: FINCEPT_ALPACA_API_KEY / FINCEPT_ALPACA_API_SECRET not set.\n"
            "       Put them in the .env file at the repo root (never commit it).",
            file=sys.stderr,
        )
        return 2

    print(f"Syncing Alpaca state from {base_url}...")

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    try:
        try:
            summary = await sync_positions_and_marks(
                redis=redis,
                api_key=api_key,
                api_secret=api_secret,
                base_url=base_url,
            )
        except AlpacaError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

        print(
            f"Account status={summary['account_status']} "
            f"equity=${summary['equity']} cash=${summary['cash']} "
            f"buying_power=${summary['buying_power']}"
        )
        print(
            f"Fetched {summary['fetched']} position(s); "
            f"wrote {summary['written']}, skipped {summary['skipped']}."
        )

        # Echo the per-position table so operators have instant feedback.
        store = PositionStore(redis)
        positions = await store.get_all(STRATEGY_ID)
        marks = await read_marks(redis, [p.symbol for p in positions.values()])
        for pos in positions.values():
            mark = marks.get(pos.symbol)
            mark_str = f"${mark}" if mark else "n/a"
            print(
                f"  {pos.symbol:<12} qty={pos.quantity!s:<8} "
                f"avg=${pos.avg_cost!s:<10} "
                f"mark={mark_str:<12} "
                f"unrealized=${pos.unrealized_pnl}"
            )
    finally:
        await redis.aclose()  # type: ignore[attr-defined]

    print("Done.  Refresh the dashboard - strategy 'alpaca.live'.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
