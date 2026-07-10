"""Tests for settlements.market_data_bridge.

Verifies that ``make_async_market_data_source`` wraps a sync bar
adapter (exposing either ``get_close`` or ``get_prices``) into the
async ``market_data_source(symbol, ts1, ts2) -> float | None`` contract
expected by ``settlements.worker.tick``.
"""

from __future__ import annotations

from dataclasses import dataclass

from settlements.market_data_bridge import make_async_market_data_source


@dataclass(frozen=True)
class _PricePoint:
    ts_ns: int
    close: float


class _CloseAdapter:
    """Fake adapter exposing a sync ``get_close`` (single-bar lookup)."""

    def __init__(self, prices: dict[tuple[str, int], _PricePoint]) -> None:
        self._prices = prices
        self.calls: list[tuple[str, int]] = []

    def get_close(self, symbol: str, ts_ns: int) -> _PricePoint | None:
        self.calls.append((symbol, ts_ns))
        return self._prices.get((symbol, ts_ns))


class _PricesAdapter:
    """Fake adapter exposing a sync ``get_prices`` (list lookup)."""

    def __init__(self, prices: dict[tuple[str, int], _PricePoint]) -> None:
        self._prices = prices
        self.calls: list[tuple[str, int, int]] = []

    def get_prices(self, symbol: str, start_ns: int, end_ns: int) -> list[_PricePoint]:
        self.calls.append((symbol, start_ns, end_ns))
        return [
            pp
            for (sym, ts), pp in self._prices.items()
            if sym == symbol and start_ns <= ts < end_ns
        ]


async def test_bridge_wraps_sync_get_close_into_async_callable() -> None:
    adapter = _CloseAdapter({("AAPL", 100): _PricePoint(100, 150.25)})
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result == 150.25
    # The bridge must query at ts2 (the later timestamp).
    assert adapter.calls == [("AAPL", 100)]


async def test_bridge_returns_none_when_get_close_returns_none() -> None:
    adapter = _CloseAdapter({})
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result is None


async def test_bridge_returns_close_as_float() -> None:
    adapter = _CloseAdapter({("AAPL", 100): _PricePoint(100, 150)})
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result == 150.0
    assert isinstance(result, float)


async def test_bridge_falls_back_to_get_prices_and_picks_latest_bar() -> None:
    # Two bars in the lookback window; the bridge must pick the latest
    # (highest ts_ns <= ts2).
    adapter = _PricesAdapter(
        {
            ("AAPL", 40): _PricePoint(40, 100.0),
            ("AAPL", 100): _PricePoint(100, 200.0),
        }
    )
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result == 200.0
    assert len(adapter.calls) == 1
    symbol, _start_ns, end_ns = adapter.calls[0]
    assert symbol == "AAPL"
    # end_ns is exclusive and must include ts2.
    assert end_ns == 101


async def test_bridge_get_prices_returns_none_when_empty() -> None:
    adapter = _PricesAdapter({})
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result is None


async def test_bridge_get_prices_returns_close_as_float() -> None:
    adapter = _PricesAdapter({("AAPL", 100): _PricePoint(100, 200)})
    source = make_async_market_data_source(adapter)

    result = await source("AAPL", 50, 100)

    assert result == 200.0
    assert isinstance(result, float)


def test_make_async_market_data_source_returns_callable() -> None:
    adapter = _CloseAdapter({})
    source = make_async_market_data_source(adapter)
    assert callable(source)
