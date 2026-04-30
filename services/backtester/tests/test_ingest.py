"""Tests for ``backtester.ingest`` — pure parsers + parquet writer.

No network, no yfinance, no httpx.  We hand-build the exact payload
shapes Alpaca / yfinance produce and assert on the resulting BarEvents
and on the on-disk parquet schema.
"""

from __future__ import annotations

import math
import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest

from backtester.ingest import (
    _trading_day_close_to_ns,
    assert_parquet_matches_runner_schema,
    bars_to_parquet,
    parse_alpaca_bars_payload,
    parse_yfinance_daily_frame,
)
from backtester.runner import load_bars_from_parquet
from fincept_core.schemas import AssetClass, BarEvent, Venue

# --------------------------------------------------------------------------- #
# Alpaca parser                                                               #
# --------------------------------------------------------------------------- #


def _alpaca_bar(
    *,
    t: str = "2024-07-15T14:30:00Z",
    o: float = 100.0,
    h: float = 101.0,
    low: float = 99.5,
    c: float = 100.5,
    v: int = 12_345,
    n: int = 50,
    vw: float | None = 100.25,
) -> dict[str, object]:
    bar: dict[str, object] = {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v, "n": n}
    if vw is not None:
        bar["vw"] = vw
    return bar


def test_alpaca_parser_basic_two_symbols() -> None:
    payload = {
        "bars": {
            "AAPL": [
                _alpaca_bar(t="2024-07-15T14:30:00Z", c=189.1),
                _alpaca_bar(t="2024-07-15T14:31:00Z", c=189.4),
            ],
            "MSFT": [_alpaca_bar(t="2024-07-15T14:30:00Z", c=440.2, vw=None)],
        },
        "next_page_token": None,
    }
    bars = parse_alpaca_bars_payload(payload, freq="1m")
    assert len(bars) == 3
    by_sym = {b.symbol: [b for b in bars if b.symbol == b.symbol] for b in bars}
    assert "AAPL" in {b.symbol for b in bars}
    assert "MSFT" in {b.symbol for b in bars}
    aapl = [b for b in bars if b.symbol == "AAPL"]
    assert aapl[0].close == Decimal("189.1")
    assert aapl[1].ts_event > aapl[0].ts_event
    msft = next(b for b in bars if b.symbol == "MSFT")
    assert msft.vwap is None
    assert all(b.freq == "1m" for b in bars)
    assert all(b.venue == Venue.NASDAQ for b in bars)
    # use by_sym to silence "unused variable" while still asserting it's well-formed
    assert by_sym


def test_alpaca_parser_skips_malformed_rows() -> None:
    payload = {
        "bars": {
            "AAPL": [
                _alpaca_bar(),  # ok
                {"t": "not-a-timestamp", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                {"o": 1, "h": 1, "l": 1, "c": 1, "v": 1},  # missing t
                "garbage-string",
                _alpaca_bar(t="2024-07-15T14:31:00Z"),
            ],
        }
    }
    bars = parse_alpaca_bars_payload(payload)
    assert len(bars) == 2  # the two well-formed entries


def test_alpaca_parser_handles_empty_or_missing_bars_field() -> None:
    assert parse_alpaca_bars_payload({}) == []
    assert parse_alpaca_bars_payload({"bars": {}}) == []
    assert parse_alpaca_bars_payload({"bars": None}) == []
    assert parse_alpaca_bars_payload({"bars": {"AAPL": "not a list"}}) == []


def test_alpaca_parser_preserves_decimal_precision() -> None:
    payload = {
        "bars": {
            "AAPL": [
                {
                    "t": "2024-07-15T14:30:00Z",
                    "o": "189.123456789",
                    "h": "189.999999999",
                    "l": "189.000000001",
                    "c": "189.555555555",
                    "v": "1234567",
                    "n": 100,
                    "vw": "189.5",
                }
            ]
        }
    }
    bars = parse_alpaca_bars_payload(payload)
    assert bars[0].close == Decimal("189.555555555")
    assert bars[0].open == Decimal("189.123456789")
    assert bars[0].vwap == Decimal("189.5")


def test_alpaca_parser_iso_z_and_offset_both_work() -> None:
    payload = {
        "bars": {
            "AAPL": [
                {"t": "2024-07-15T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                {
                    "t": "2024-07-15T14:31:00+00:00",
                    "o": 1,
                    "h": 1,
                    "l": 1,
                    "c": 1,
                    "v": 1,
                },
            ]
        }
    }
    bars = parse_alpaca_bars_payload(payload)
    assert len(bars) == 2
    # 60s apart in UTC
    assert bars[1].ts_event - bars[0].ts_event == 60 * 1_000_000_000


# --------------------------------------------------------------------------- #
# yfinance parser                                                             #
# --------------------------------------------------------------------------- #


def _yf_row(d: date, close: float = 100.0) -> dict[str, object]:
    return {
        "Date": d,
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": 1_000_000,
    }


def test_yfinance_daily_pins_to_nyse_close_dst_aware() -> None:
    rows = [_yf_row(date(2024, 7, 5), close=190.0)]  # EDT, 16:00 ET = 20:00 UTC
    bars = parse_yfinance_daily_frame("AAPL", rows)
    assert len(bars) == 1
    expected = _trading_day_close_to_ns(date(2024, 7, 5))
    assert bars[0].ts_event == expected
    # Sanity: 2024-07-05 16:00 ET == 2024-07-05 20:00 UTC
    dt = datetime.fromtimestamp(bars[0].ts_event / 1e9, tz=UTC)
    assert dt.hour == 20


def test_yfinance_daily_handles_est_dst_transition() -> None:
    # Nov 5 2024 (Tue) is EST (UTC-5); 16:00 ET -> 21:00 UTC
    rows = [_yf_row(date(2024, 11, 5))]
    bars = parse_yfinance_daily_frame("AAPL", rows)
    dt = datetime.fromtimestamp(bars[0].ts_event / 1e9, tz=UTC)
    assert dt.hour == 21


def test_yfinance_daily_skips_nan_rows() -> None:
    rows = [
        _yf_row(date(2024, 7, 5), close=100.0),
        {
            "Date": date(2024, 7, 8),
            "Open": math.nan,
            "High": 1,
            "Low": 1,
            "Close": 1,
            "Volume": 1,
        },
        {"Date": date(2024, 7, 9), "Open": 1, "High": 1},  # missing close/low/volume
        _yf_row(date(2024, 7, 10), close=101.0),
    ]
    bars = parse_yfinance_daily_frame("AAPL", rows)
    assert [b.ts_event for b in bars] == [
        _trading_day_close_to_ns(date(2024, 7, 5)),
        _trading_day_close_to_ns(date(2024, 7, 10)),
    ]


def test_yfinance_daily_accepts_iso_string_date() -> None:
    rows = [{**_yf_row(date(2024, 7, 5)), "Date": "2024-07-05"}]
    bars = parse_yfinance_daily_frame("AAPL", rows)
    assert len(bars) == 1
    assert bars[0].ts_event == _trading_day_close_to_ns(date(2024, 7, 5))


def test_yfinance_daily_uses_equity_asset_class() -> None:
    rows = [_yf_row(date(2024, 7, 5))]
    bars = parse_yfinance_daily_frame("AAPL", rows)
    assert bars[0].asset_class == AssetClass.EQUITY
    assert bars[0].freq == "1d"


# --------------------------------------------------------------------------- #
# Parquet writer + runner round-trip                                          #
# --------------------------------------------------------------------------- #


def _bar(symbol: str, ts_ns: int, close: float = 100.0) -> BarEvent:
    return BarEvent(
        venue=Venue.NASDAQ,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        ts_event=ts_ns,
        ts_recv=ts_ns,
        freq="1m",
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.5)),
        low=Decimal(str(close - 0.5)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
        trades=10,
        vwap=Decimal(str(close)),
    )


def test_bars_to_parquet_writes_required_columns(tmp_path: pathlib.Path) -> None:
    bars = [
        _bar("AAPL", 1_000_000_000, close=190.0),
        _bar("MSFT", 1_000_000_000, close=440.0),
        _bar("AAPL", 1_060_000_000, close=190.5),
    ]
    out = tmp_path / "bars.parquet"
    n = bars_to_parquet(bars, out)
    assert n == 3
    assert_parquet_matches_runner_schema(out)
    df = pl.read_parquet(out)
    # Sorted by (ts_event, symbol)
    rows = df.to_dicts()
    assert rows[0]["ts_event"] <= rows[1]["ts_event"] <= rows[2]["ts_event"]
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}


def test_bars_to_parquet_round_trips_through_runner_loader(
    tmp_path: pathlib.Path,
) -> None:
    bars_in = [_bar("AAPL", 1_000_000_000, close=190.0)]
    out = tmp_path / "bars.parquet"
    bars_to_parquet(bars_in, out)
    by_symbol = load_bars_from_parquet(out, venue=Venue.NASDAQ, freq="1m")
    assert "AAPL" in by_symbol
    bar_out = by_symbol["AAPL"][0]
    assert bar_out.close == Decimal("190.0")
    assert bar_out.ts_event == 1_000_000_000
    assert bar_out.symbol == "AAPL"


def test_bars_to_parquet_empty_writes_valid_empty_file(
    tmp_path: pathlib.Path,
) -> None:
    out = tmp_path / "empty.parquet"
    n = bars_to_parquet([], out)
    assert n == 0
    df = pl.read_parquet(out)
    assert df.height == 0
    # Schema must still satisfy the runner's pre-flight
    assert_parquet_matches_runner_schema(out)


def test_assert_parquet_schema_raises_on_missing_columns(
    tmp_path: pathlib.Path,
) -> None:
    bad = tmp_path / "bad.parquet"
    pl.DataFrame({"symbol": ["AAPL"], "close": [100.0]}).write_parquet(bad)
    with pytest.raises(ValueError, match="missing required columns"):
        assert_parquet_matches_runner_schema(bad)
