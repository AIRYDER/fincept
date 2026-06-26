"""Bridge from quant_foundry.BarDataAdapter to settlements.worker market_data_source contract.

The settlement worker (``settlements.worker.tick``) expects an async
callable ``market_data_source(symbol, ts1, ts2) -> float | None`` that
returns the close price at ``ts2`` (the later of the two timestamps),
or ``None`` when no bar is available.

The production price feed is ``quant_foundry.market_data_adapter.BarDataAdapter``,
a *sync* adapter whose ``get_prices(symbol, start_ns, end_ns)`` reads
from ``fincept_db.bars`` with an optional Alpaca fallback and returns a
sorted list of ``PricePoint`` objects.  This module wraps that sync
adapter into the worker's async contract using ``asyncio.to_thread`` so
the blocking DB / HTTP calls run off the event loop.

Adapter protocol
~~~~~~~~~~~~~~~~~

``make_async_market_data_source`` accepts any object exposing EITHER:

  * ``get_close(symbol, ts_ns) -> PricePoint | None`` — a single-bar
    lookup (used by tests / fakes), OR
  * ``get_prices(symbol, start_ns, end_ns) -> list[PricePoint]`` — the
    real ``BarDataAdapter`` method.  When only ``get_prices`` is
    available, the bridge selects the latest bar whose ``ts_ns`` falls
    within the minute leading up to ``ts2`` (bars are 1-min freq by
    default), which is the bar whose minute contains ``ts2``.

Keeping the bridge in the ``settlements`` package (rather than in
``quant_foundry``) means the worker never imports ``quant_foundry`` —
the dependency direction stays one-way (api → settlements →
quant_foundry is wired only at the api layer).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# One minute in nanoseconds — the default bar frequency for
# ``BarDataAdapter``.  Used only by the ``get_prices`` fallback path to
# locate the bar whose minute contains ``ts2``.
_MINUTE_NS = 60_000_000_000


def make_async_market_data_source(
    bar_adapter: Any,
) -> Callable[[str, int, int], Awaitable[float | None]]:
    """Wrap a sync bar adapter into the async ``market_data_source`` contract.

    ``bar_adapter`` is a ``quant_foundry.market_data_adapter.BarDataAdapter``
    (typed as ``Any`` here to avoid importing ``quant_foundry`` from the
    ``settlements`` package).  The returned async callable satisfies the
    ``market_data_source(symbol, ts1, ts2) -> float | None`` contract
    used by ``settlements.worker.tick``: it looks up the close at
    ``ts2`` (the later timestamp) and returns it as a ``float``, or
    ``None`` when no bar is available.
    """
    get_close = getattr(bar_adapter, "get_close", None)

    async def source(symbol: str, ts1: int, ts2: int) -> float | None:
        # The worker wants the close at ts2 (the later timestamp).
        # ts1 is accepted for contract stability but not used here.
        if get_close is not None:
            pp = await asyncio.to_thread(get_close, symbol, ts2)
        else:
            # Fall back to get_prices: pick the latest bar with
            # ts_ns <= ts2 within a one-minute lookback window (the bar
            # whose minute contains ts2 for 1-min freq bars).
            points = await asyncio.to_thread(
                bar_adapter.get_prices, symbol, ts2 - _MINUTE_NS, ts2 + 1
            )
            pp = points[-1] if points else None
        if pp is None:
            return None
        return float(pp.close)

    return source


__all__ = ["make_async_market_data_source"]
