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


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def test_evict_stale_removes_inactive_symbols() -> None:
    """Symbols inactive beyond retention should be evicted."""
    # Use a small retention for testing: 1000 ns
    c = FeatureComputer(benchmark_symbol="BTC-USD", state_retention_ns=1000)

    # Feed two symbols at ts=0.
    c.compute(_bar("BTC-USD", ts_event=0, close="100"))
    c.compute(_bar("ETH-USD", ts_event=0, close="50"))
    assert c.cached_symbols == 2

    # Feed BTC again at ts=2000 (active), ETH stays at ts=0 (inactive).
    c.compute(_bar("BTC-USD", ts_event=2000, close="105"))
    # Evict with now=2000.  ETH's last_seen=0, 2000-0=2000 > 1000 retention.
    evicted = c.evict_stale(now_ns=2000)
    assert evicted == 1
    assert c.cached_symbols == 1
    assert "ETH-USD" not in c._price
    assert "ETH-USD" not in c._vol
    assert c.total_evicted == 1


def test_evict_stale_preserves_active_symbols() -> None:
    """Active symbols should not be evicted."""
    c = FeatureComputer(benchmark_symbol="BTC-USD", state_retention_ns=10_000)

    c.compute(_bar("BTC-USD", ts_event=0, close="100"))
    c.compute(_bar("ETH-USD", ts_event=5000, close="50"))

    # now=8000: BTC silent=8000 < 10000, ETH silent=3000 < 10000
    evicted = c.evict_stale(now_ns=8000)
    assert evicted == 0
    assert c.cached_symbols == 2


def test_evict_stale_no_entries_returns_zero() -> None:
    c = FeatureComputer()
    assert c.evict_stale(now_ns=1_000_000) == 0
    assert c.total_evicted == 0


def test_evicted_symbol_re_added_on_activity() -> None:
    """If a symbol becomes active again after eviction, it's re-added."""
    c = FeatureComputer(benchmark_symbol="BTC-USD", state_retention_ns=1000)

    c.compute(_bar("ETH-USD", ts_event=0, close="50"))
    c.evict_stale(now_ns=2000)
    assert c.cached_symbols == 0

    # New bar for ETH re-adds the entry.
    c.compute(_bar("ETH-USD", ts_event=2000, close="55"))
    assert c.cached_symbols == 1
    assert "ETH-USD" in c._price


def test_evict_stale_uses_max_ts_when_now_ns_is_none() -> None:
    """When now_ns is None, use the max ts_event as reference (for offline)."""
    c = FeatureComputer(benchmark_symbol="BTC-USD", state_retention_ns=1000)

    c.compute(_bar("BTC-USD", ts_event=0, close="100"))
    c.compute(_bar("ETH-USD", ts_event=0, close="50"))
    c.compute(_bar("BTC-USD", ts_event=2000, close="105"))

    # now=None -> ref=max(last_seen)=2000.  ETH at 0, 2000-0=2000 > 1000.
    evicted = c.evict_stale()
    assert evicted == 1
    assert "ETH-USD" not in c._price


def test_evict_stale_accumulates_total_count() -> None:
    """total_evicted should accumulate across multiple calls."""
    c = FeatureComputer(benchmark_symbol="BTC-USD", state_retention_ns=1000)

    c.compute(_bar("ETH-USD", ts_event=0, close="50"))
    c.evict_stale(now_ns=2000)
    assert c.total_evicted == 1

    c.compute(_bar("SOL-USD", ts_event=2000, close="30"))
    c.evict_stale(now_ns=4000)
    assert c.total_evicted == 2


def test_cached_symbols_property() -> None:
    c = FeatureComputer(benchmark_symbol="BTC-USD")
    assert c.cached_symbols == 0

    c.compute(_bar("BTC-USD", ts_event=0, close="100"))
    assert c.cached_symbols == 1

    c.compute(_bar("ETH-USD", ts_event=0, close="50"))
    assert c.cached_symbols == 2
