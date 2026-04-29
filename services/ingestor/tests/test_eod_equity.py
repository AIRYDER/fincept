"""
Tests for ingestor.eod_equity — pure parsing + injected loader paths.

No live network or database: ``YFinanceLoader`` accepts an injectable
``download_fn`` and ``write_fn`` so we hand-build DataFrames and capture
writes in-memory.  This is deterministic, fast, and runs anywhere.

The actual yfinance round-trip is exercised by the ``@pytest.mark.live``
test in this module's tail, only run when ``pytest -m live`` is invoked.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from fincept_core.schemas import AssetClass, BarEvent, Venue
from ingestor.eod_equity import (
    PolygonLoader,
    YFinanceLoader,
    _parse_yfinance_frame,
    is_us_trading_day,
    trading_day_close_to_ns,
)

# ---------------------------------------------------------------------------
# Trading-day timestamps + calendar
# ---------------------------------------------------------------------------


def test_trading_day_close_in_est_is_21_utc() -> None:
    """Nov 5 2024 (Tue) is in EST (UTC-5); NYSE close = 16:00 ET = 21:00 UTC."""
    ns = trading_day_close_to_ns(date(2024, 11, 5))
    expected_iso = "2024-11-05T21:00:00+00:00"
    iso = pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()
    assert iso == expected_iso


def test_trading_day_close_in_edt_is_20_utc() -> None:
    """Jul 5 2024 (Fri) is in EDT (UTC-4); NYSE close = 16:00 ET = 20:00 UTC."""
    ns = trading_day_close_to_ns(date(2024, 7, 5))
    expected_iso = "2024-07-05T20:00:00+00:00"
    iso = pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()
    assert iso == expected_iso


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2024, 11, 5), True),  # Tuesday
        (date(2024, 11, 9), False),  # Saturday
        (date(2024, 11, 10), False),  # Sunday
        (date(2024, 11, 8), True),  # Friday
    ],
)
def test_is_us_trading_day(d: date, expected: bool) -> None:
    assert is_us_trading_day(d) is expected


# ---------------------------------------------------------------------------
# DataFrame parsing
# ---------------------------------------------------------------------------


def _single_ticker_frame() -> pd.DataFrame:
    """A 3-day flat DataFrame as ``yf.download(tickers="AAPL")`` would return."""
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-11-04"),
            pd.Timestamp("2024-11-05"),
            pd.Timestamp("2024-11-06"),
        ],
        name="Date",
    )
    return pd.DataFrame(
        {
            "Open": [220.10, 222.00, 221.50],
            "High": [223.00, 224.50, 222.80],
            "Low": [219.50, 221.10, 220.00],
            "Close": [222.50, 223.20, 221.90],
            "Volume": [50_000_000, 55_000_000, 48_000_000],
        },
        index=idx,
    )


def test_parse_single_ticker_frame_returns_one_bar_per_row() -> None:
    bars = _parse_yfinance_frame("AAPL", _single_ticker_frame())
    assert len(bars) == 3
    assert all(isinstance(b, BarEvent) for b in bars)


def test_parsed_bar_fields_are_canonical() -> None:
    bars = _parse_yfinance_frame("AAPL", _single_ticker_frame())
    bar = bars[0]
    assert bar.venue == Venue.NASDAQ
    assert bar.symbol == "AAPL"
    assert bar.asset_class == AssetClass.EQUITY
    assert bar.freq == "1d"
    assert bar.trades == 0
    assert bar.vwap is None
    assert bar.open == Decimal("220.10")
    assert bar.high == Decimal("223.00")
    assert bar.low == Decimal("219.50")
    assert bar.close == Decimal("222.50")
    assert bar.volume == Decimal("50000000")
    # Nov 4 2024 is EST → 21:00 UTC.
    assert bar.ts_event == trading_day_close_to_ns(date(2024, 11, 4))
    assert bar.ts_recv == bar.ts_event


def test_parse_empty_frame_returns_empty_list() -> None:
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert _parse_yfinance_frame("AAPL", empty) == []


def test_parse_skips_rows_with_nan_ohlcv() -> None:
    idx = pd.DatetimeIndex([pd.Timestamp("2024-11-04"), pd.Timestamp("2024-11-05")], name="Date")
    frame = pd.DataFrame(
        {
            "Open": [220.0, float("nan")],
            "High": [221.0, 222.0],
            "Low": [219.0, 220.0],
            "Close": [220.5, 221.5],
            "Volume": [1_000_000, 1_500_000],
        },
        index=idx,
    )
    bars = _parse_yfinance_frame("AAPL", frame)
    assert len(bars) == 1
    assert bars[0].ts_event == trading_day_close_to_ns(date(2024, 11, 4))


# ---------------------------------------------------------------------------
# YFinanceLoader.load_for_date_range with injected fakes
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[Sequence[BarEvent]] = []

    async def __call__(self, bars: Sequence[BarEvent]) -> int:
        captured = list(bars)
        self.calls.append(captured)
        return len(captured)


def _multi_ticker_grouped_frame(symbols: Sequence[str], days: int = 2) -> pd.DataFrame:
    """Build a multi-level column DataFrame the way ``group_by="ticker"`` returns."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"2024-11-{4 + i:02d}") for i in range(days)], name="Date")
    cols = pd.MultiIndex.from_product([list(symbols), ["Open", "High", "Low", "Close", "Volume"]])
    rows = []
    for i in range(days):
        row: list[float] = []
        for j, _sym in enumerate(symbols):
            base = 100 + 10 * j + i
            row.extend(
                [
                    float(base),
                    float(base + 1),
                    float(base - 1),
                    float(base + 0.5),
                    float(1_000_000 + i * 100),
                ]
            )
        rows.append(row)
    return pd.DataFrame(rows, index=idx, columns=cols)


async def test_loader_writes_bars_for_single_symbol() -> None:
    writer = _FakeWriter()

    def fake_download(**kwargs: Any) -> pd.DataFrame:
        return _single_ticker_frame()

    loader = YFinanceLoader(download_fn=fake_download, write_fn=writer)
    written = await loader.load_for_date_range(["AAPL"], date(2024, 11, 4), date(2024, 11, 6))

    assert written == 3
    assert len(writer.calls) == 1
    [batch] = writer.calls
    assert {bar.symbol for bar in batch} == {"AAPL"}


async def test_loader_writes_bars_for_multi_symbol_group_by() -> None:
    writer = _FakeWriter()

    def fake_download(**kwargs: Any) -> pd.DataFrame:
        return _multi_ticker_grouped_frame(["AAPL", "MSFT"], days=2)

    loader = YFinanceLoader(download_fn=fake_download, write_fn=writer)
    written = await loader.load_for_date_range(
        ["AAPL", "MSFT"], date(2024, 11, 4), date(2024, 11, 5)
    )

    assert written == 4  # 2 symbols x 2 days
    [batch] = writer.calls
    assert {bar.symbol for bar in batch} == {"AAPL", "MSFT"}


async def test_loader_logs_shortfall_when_below_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expected 4 rows (2 sym x 2 days), fetched 1 -> 25% -> triggers shortfall.

    structlog's ``PrintLoggerFactory`` writes to stdout, not stdlib logging,
    so we capture via ``capsys`` rather than ``caplog``.
    """
    writer = _FakeWriter()
    sparse = _single_ticker_frame().iloc[:1]  # 1 row only

    def fake_download(**kwargs: Any) -> pd.DataFrame:
        # Return 1 row even though caller expects 4 (multi-symbol won't see
        # MSFT; we deliberately misshape to trigger shortfall).
        return sparse

    loader = YFinanceLoader(download_fn=fake_download, write_fn=writer)
    await loader.load_for_date_range(["AAPL", "MSFT"], date(2024, 11, 4), date(2024, 11, 5))
    captured = capsys.readouterr().out
    assert "eod.shortfall" in captured


async def test_loader_with_empty_symbols_is_noop() -> None:
    writer = _FakeWriter()

    def fake_download(**kwargs: Any) -> pd.DataFrame:
        raise AssertionError("download_fn should not be called for empty symbols")

    loader = YFinanceLoader(download_fn=fake_download, write_fn=writer)
    written = await loader.load_for_date_range([], date(2024, 11, 4), date(2024, 11, 6))
    assert written == 0
    assert writer.calls == []


async def test_loader_rejects_inverted_date_range() -> None:
    loader = YFinanceLoader(download_fn=lambda **_: _single_ticker_frame())
    with pytest.raises(ValueError, match="after end"):
        await loader.load_for_date_range(["AAPL"], date(2024, 11, 6), date(2024, 11, 4))


async def test_loader_passes_inclusive_end_date_to_yfinance() -> None:
    """yfinance treats `end` exclusively; we must pass end+1 to include it."""
    captured: dict[str, str] = {}

    def fake_download(**kwargs: Any) -> pd.DataFrame:
        captured["start"] = kwargs["start"]
        captured["end"] = kwargs["end"]
        return _single_ticker_frame()

    loader = YFinanceLoader(download_fn=fake_download, write_fn=_FakeWriter())
    await loader.load_for_date_range(["AAPL"], date(2024, 11, 4), date(2024, 11, 6))

    assert captured["start"] == "2024-11-04"
    assert captured["end"] == "2024-11-07"  # end + 1


# ---------------------------------------------------------------------------
# PolygonLoader stub
# ---------------------------------------------------------------------------


async def test_polygon_loader_raises_not_implemented() -> None:
    loader = PolygonLoader()
    with pytest.raises(NotImplementedError, match="Phase H"):
        await loader.load_for_date_range(["AAPL"], date(2024, 11, 4), date(2024, 11, 4))


# ---------------------------------------------------------------------------
# Live network round-trip — only run with `pytest -m live`
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_live_yfinance_load_aapl_round_trip() -> None:
    """End-to-end smoke test against real yfinance.  Skipped in default CI."""
    writer = _FakeWriter()
    loader = YFinanceLoader(write_fn=writer)
    written = await loader.load_for_date_range(["AAPL"], date(2024, 11, 4), date(2024, 11, 8))
    assert written >= 3  # at least 3 trading days in the range
    [batch] = writer.calls
    assert all(b.symbol == "AAPL" and b.close > Decimal(0) for b in batch)
