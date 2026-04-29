"""
backtester.datasource — historical bar replay.

Single responsibility: produce ``BarEvent``s in strict monotonic
``ts_event`` order across one or more symbols.  Backed by
``fincept_db.bars.read_bars`` in production; tests inject in-memory
fixtures.

Multi-symbol replay strategy: read each symbol's range upfront, then
merge by ``ts_event`` via ``heapq.merge``.  This is O(N log K) where K
is the number of symbols — fine for the universes we care about
(< 50 symbols).  When K grows we'll move to a streaming heap of cursors,
but the in-memory merge is simpler and faster for now.
"""

from __future__ import annotations

import heapq
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from fincept_core.schemas import BarEvent
from fincept_db.bars import read_bars

BarReader = Callable[[str, str, int, int], Awaitable[list[BarEvent]]]


class BarsDataSource:
    """Replay historical bars in monotonic ``ts_event`` order."""

    def __init__(
        self,
        symbols: Sequence[str],
        freq: str,
        start_ns: int,
        end_ns: int,
        *,
        bar_reader: BarReader | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("BarsDataSource requires at least one symbol")
        if start_ns >= end_ns:
            raise ValueError(f"start_ns {start_ns} must be < end_ns {end_ns}")
        self._symbols = list(symbols)
        self._freq = freq
        self._start_ns = start_ns
        self._end_ns = end_ns
        self._bar_reader: BarReader = bar_reader if bar_reader is not None else read_bars

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def freq(self) -> str:
        return self._freq

    async def replay(self) -> AsyncIterator[BarEvent]:
        """Yield bars in ``ts_event`` ascending order across all symbols."""
        per_symbol: list[list[BarEvent]] = []
        for sym in self._symbols:
            bars = await self._bar_reader(sym, self._freq, self._start_ns, self._end_ns)
            per_symbol.append(bars)
        # heapq.merge requires a key for tie-breaking when ts_event collides
        # across symbols; symbol name is deterministic and stable.
        merged = heapq.merge(*per_symbol, key=lambda b: (b.ts_event, b.symbol))
        for bar in merged:
            yield bar
