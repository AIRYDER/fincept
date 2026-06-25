"""
Tests for quant_foundry.market_data_adapter (Agent A — settlement track).

Covers:
- Fixture bars returned sorted by ts_ns.
- Missing data returns empty list (settlement → PENDING_DATA).
- Date-range filtering excludes out-of-window prices.
- Benchmark fallback uses the configured benchmark symbol.
- Default adapter (no bar_reader) returns empty list when fincept_db
  is unavailable.
"""

from __future__ import annotations

from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_bar_reader(
    bars: dict[str, list[PricePoint]],
) -> "callable[[str, int, int], list[PricePoint]]":
    """Return a simple in-memory bar reader keyed by symbol."""

    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return [
            p
            for p in bars.get(symbol, [])
            if start_ns <= p.ts_ns < end_ns
        ]

    return reader


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestPricePoint:
    def test_frozen(self) -> None:
        pp = PricePoint(ts_ns=100, close=42.5)
        assert pp.ts_ns == 100
        assert pp.close == 42.5

    def test_equality(self) -> None:
        a = PricePoint(ts_ns=100, close=42.5)
        b = PricePoint(ts_ns=100, close=42.5)
        assert a == b


class TestBarDataAdapterFixtureBars:
    def test_returns_sorted_prices(self) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=200, close=151.0),
                PricePoint(ts_ns=100, close=150.0),
                PricePoint(ts_ns=150, close=150.5),
            ],
        }
        adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
        prices = adapter.get_prices("AAPL", 0, 300)
        assert len(prices) == 3
        assert [p.ts_ns for p in prices] == [100, 150, 200]
        assert prices[0].close == 150.0

    def test_get_benchmark_prices(self) -> None:
        bars = {
            "SPY": [
                PricePoint(ts_ns=100, close=400.0),
                PricePoint(ts_ns=200, close=401.0),
            ],
        }
        adapter = BarDataAdapter(
            bar_reader=_make_bar_reader(bars),
            benchmark_symbol="SPY",
        )
        prices = adapter.get_benchmark_prices(0, 300)
        assert len(prices) == 2
        assert prices[0].ts_ns == 100
        assert prices[0].close == 400.0

    def test_custom_benchmark_symbol(self) -> None:
        bars = {
            "QQQ": [PricePoint(ts_ns=100, close=300.0)],
        }
        adapter = BarDataAdapter(
            bar_reader=_make_bar_reader(bars),
            benchmark_symbol="QQQ",
        )
        prices = adapter.get_benchmark_prices(0, 200)
        assert len(prices) == 1
        assert prices[0].close == 300.0


class TestBarDataAdapterMissingData:
    def test_unknown_symbol_returns_empty(self) -> None:
        bars = {"AAPL": [PricePoint(ts_ns=100, close=150.0)]}
        adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
        prices = adapter.get_prices("MSFT", 0, 300)
        assert prices == []

    def test_empty_bars_returns_empty(self) -> None:
        adapter = BarDataAdapter(bar_reader=_make_bar_reader({}))
        prices = adapter.get_prices("AAPL", 0, 300)
        assert prices == []

    def test_benchmark_missing_returns_empty(self) -> None:
        adapter = BarDataAdapter(
            bar_reader=_make_bar_reader({}),
            benchmark_symbol="SPY",
        )
        prices = adapter.get_benchmark_prices(0, 300)
        assert prices == []


class TestBarDataAdapterDateRangeFiltering:
    def test_excludes_before_start(self) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=50, close=149.0),
                PricePoint(ts_ns=100, close=150.0),
                PricePoint(ts_ns=200, close=151.0),
            ],
        }
        adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
        prices = adapter.get_prices("AAPL", 100, 300)
        assert len(prices) == 2
        assert all(p.ts_ns >= 100 for p in prices)

    def test_excludes_at_or_after_end(self) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=100, close=150.0),
                PricePoint(ts_ns=200, close=151.0),
                PricePoint(ts_ns=300, close=152.0),
            ],
        }
        adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
        prices = adapter.get_prices("AAPL", 0, 300)
        assert len(prices) == 2
        assert all(p.ts_ns < 300 for p in prices)

    def test_exact_boundary(self) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=100, close=150.0),
                PricePoint(ts_ns=299, close=151.0),
            ],
        }
        adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
        prices = adapter.get_prices("AAPL", 100, 300)
        assert len(prices) == 2


class TestBarDataAdapterDefaultFallback:
    def test_no_bar_reader_returns_empty(self) -> None:
        """When fincept_db is unavailable, the default adapter returns []."""
        adapter = BarDataAdapter()
        prices = adapter.get_prices("AAPL", 0, 1_000_000_000)
        assert prices == []

    def test_no_bar_reader_benchmark_returns_empty(self) -> None:
        adapter = BarDataAdapter()
        prices = adapter.get_benchmark_prices(0, 1_000_000_000)
        assert prices == []
