"""
oms.prices — in-memory latest-price cache.

The OMS needs a "current price" to fill MARKET orders against.  Rather
than query Redis or Timescale on every fill (slow + introduces an
unnecessary dependency), we keep a per-symbol Decimal in-memory and
update it from the ``md.trades`` stream in a background task.

This is intentionally simple:
  - Last-write-wins; no time-weighting or VWAP.
  - No persistence; on restart the cache is empty until trades flow in.
  - No locking; updates and reads are atomic at the Python-dict level
    in CPython for single-key access, which is good enough for v1.
"""

from __future__ import annotations

from decimal import Decimal


class LivePrices:
    """Per-symbol latest trade price cache."""

    def __init__(self) -> None:
        self._cache: dict[str, Decimal] = {}

    def update(self, symbol: str, price: Decimal) -> None:
        self._cache[symbol] = price

    def get(self, symbol: str) -> Decimal | None:
        return self._cache.get(symbol)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._cache
