"""
jobs.daily_eod_load — once-a-day orchestrator for EOD equity bars.

Wires the pieces:

  1. Resolve the target trading date (defaults to "yesterday").
  2. Skip if the target is a weekend (``is_us_trading_day``).
  3. Fetch the active equity universe.
  4. Hand off to the configured ``Loader`` for that date range.

Designed to be invoked from ``jobs.main`` (APScheduler) or directly
from a one-shot CLI (``python -m jobs.daily_eod_load``).  Both the loader
and the universe-fetch are injectable so unit tests don't touch the
network or the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from fincept_core.logging import configure_logging, get_logger
from ingestor.eod_equity import (
    Loader,
    get_equity_universe,
    get_loader,
    is_us_trading_day,
)

log = get_logger(__name__)

UniverseFn = Callable[[], Awaitable[list[str]]]
LoaderFactory = Callable[[], Loader]


async def run_daily(
    target: date | None = None,
    *,
    loader_factory: LoaderFactory = get_loader,
    universe_fn: UniverseFn = get_equity_universe,
) -> int:
    """Run one EOD load.  Returns the number of rows written (0 on skip).

    *target* defaults to ``date.today() - 1 day`` so a 22:30 ET schedule
    captures the trading day that just closed.  The scheduler runs Mon-Fri,
    so the previous calendar day is the target.
    """
    if target is None:
        target = date.today() - timedelta(days=1)

    if not is_us_trading_day(target):
        log.info("eod.skip_non_trading_day", target=target.isoformat())
        return 0

    universe = await universe_fn()
    if not universe:
        log.warning("eod.empty_universe", target=target.isoformat())
        return 0

    loader = loader_factory()
    rows = await loader.load_for_date_range(universe, target, target)
    log.info(
        "eod.run.complete",
        target=target.isoformat(),
        symbols=len(universe),
        rows=rows,
    )
    return rows


def main() -> None:
    """CLI shim: ``python -m jobs.daily_eod_load`` runs once for yesterday."""
    configure_logging()
    asyncio.run(run_daily())


if __name__ == "__main__":
    main()
