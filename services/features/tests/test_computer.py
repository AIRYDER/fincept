"""
Tests for features.computer.FeatureComputer.

The bit-identical guarantee — same bar sequence, same FeatureFrames — is
what makes online and offline feature paths trustworthy.  These tests
pin it deterministically without any I/O.
"""

from __future__ import annotations

from decimal import Decimal

from features.computer import FeatureComputer
from fincept_core.schemas import AssetClass, BarEvent, Venue


def _bar(symbol: str, *, ts_event: int, close: str) -> BarEvent:
    c = Decimal(close)
    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq="1m",
        open=c,
        high=c + Decimal("1"),
        low=c - Decimal("1"),
        close=c,
        volume=Decimal("10"),
        trades=5,
        vwap=c,
    )


def test_compute_returns_feature_frame_for_each_bar() -> None:
    c = FeatureComputer(benchmark_symbol="BTC-USD")
    frame = c.compute(_bar("BTC-USD", ts_event=1_000, close="100"))
    assert frame.symbol == "BTC-USD"
    assert frame.ts_event == 1_000
    assert frame.freq == "1m"


def test_first_bar_emits_all_none_values() -> None:
    """Bootstrap path: a single close yields no return -> all features None."""
    c = FeatureComputer()
    frame = c.compute(_bar("BTC-USD", ts_event=1_000, close="100"))
    assert all(v is None for v in frame.values.values())


def test_two_computers_with_same_bar_sequence_are_bit_identical() -> None:
    """The core bit-identical guarantee: same input -> same output."""
    bars = [
        _bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(100 + (i % 2) * 10)) for i in range(20)
    ]
    a = FeatureComputer(benchmark_symbol="BTC-USD")
    b = FeatureComputer(benchmark_symbol="BTC-USD")
    for bar in bars:
        fa = a.compute(bar)
        fb = b.compute(bar)
        assert fa.values == fb.values
        assert fa.ts_event == fb.ts_event
        assert fa.symbol == fb.symbol
        assert fa.freq == fb.freq


def test_benchmark_symbol_property_exposes_configured_value() -> None:
    c = FeatureComputer(benchmark_symbol="ETH-USD")
    assert c.benchmark == "ETH-USD"


def test_two_symbols_have_independent_price_state() -> None:
    """ETH momentum should not be polluted by BTC's price history."""
    c = FeatureComputer(benchmark_symbol="BTC-USD")
    for i, px in enumerate([100, 101, 102, 103, 104, 105]):
        c.compute(_bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(px)))
    eth_first = c.compute(_bar("ETH-USD", ts_event=10_000, close="50"))
    # mom_5 needs 6 bars on the same symbol; ETH only has one.
    assert eth_first.values["mom_5"] is None


def test_benchmark_self_compare_yields_beta_one_after_warmup() -> None:
    """When the only data fed in is the benchmark, beta_BTC_60 -> 1 once
    the rolling window fills up."""
    c = FeatureComputer(benchmark_symbol="BTC-USD")
    closes = [100.0 if i % 2 == 0 else 110.0 for i in range(70)]
    last = None
    for i, close in enumerate(closes):
        last = c.compute(_bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(close)))
    assert last is not None
    beta = last.values["beta_BTC-USD_60"]
    assert beta is not None
    assert abs(beta - 1.0) < 1e-9


def test_value_keys_are_stable_across_first_and_subsequent_bars() -> None:
    """The set of keys in FeatureFrame.values must be stable from bar 0
    onwards; consumers rely on a known schema even during bootstrap."""
    c = FeatureComputer(benchmark_symbol="BTC-USD")
    f0 = c.compute(_bar("BTC-USD", ts_event=1_000, close="100"))
    f1 = c.compute(_bar("BTC-USD", ts_event=2_000, close="105"))
    assert set(f0.values) == set(f1.values)
