"""Tests for ``backtester.gbm_features`` — pure feature computation."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from backtester.gbm_features import (
    compute_features,
    parse_feature_name,
    require_supported,
    required_window_bars,
)
from fincept_core.schemas import AssetClass, BarEvent, Venue


def _bar(ts_ns: int, close: float) -> BarEvent:
    return BarEvent(
        venue=Venue.PAPER,
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        ts_event=ts_ns,
        ts_recv=ts_ns,
        freq="1m",
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=Decimal("100"),
        trades=1,
        vwap=None,
    )


# --------------------------------------------------------------------------- #
# parse_feature_name                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Minute-suffix (existing behaviour, unchanged)
        ("ret_1m", ("ret", 1)),
        ("ret_60m", ("ret", 60)),
        ("rv_5m", ("rv", 5)),
        ("rv_30m", ("rv", 30)),
        ("mom_z_30m", ("mom_z", 30)),
        ("mom_z_240m", ("mom_z", 240)),
        # Hour-suffix (new): 1h = 60m
        ("ret_1h", ("ret", 60)),
        ("ret_2h", ("ret", 120)),
        ("rv_4h", ("rv", 240)),
        ("mom_z_24h", ("mom_z", 1440)),
        # Day-suffix (new): 1d = 1440m (calendar-day convention)
        ("ret_1d", ("ret", 1440)),
        ("ret_5d", ("ret", 7200)),
        ("rv_20d", ("rv", 28800)),
        ("mom_z_60d", ("mom_z", 86400)),
    ],
)
def test_parse_feature_name_supported(name: str, expected: tuple[str, int]) -> None:
    assert parse_feature_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "book_imbalance_1",
        "spread_bps",
        "ret_5",  # no unit suffix
        "ret_0m",  # zero count
        "ret_0d",  # zero count, day suffix
        "ret_0h",  # zero count, hour suffix
        "ret_-1m",  # negative
        "ret_5w",  # 'w' is not a supported unit
        "ret_5min",  # multi-char unit not allowed
        "RET_5m",  # case-sensitive
        "garbage",
        "",
    ],
)
def test_parse_feature_name_unsupported(name: str) -> None:
    with pytest.raises(ValueError):
        parse_feature_name(name)


def test_require_supported_aggregates_all_failures() -> None:
    with pytest.raises(ValueError) as exc:
        require_supported(["ret_5m", "book_imbalance_1", "spread_bps"])
    msg = str(exc.value)
    assert "book_imbalance_1" in msg
    assert "spread_bps" in msg
    # Mention only the two unsupported ones, not ret_5m
    assert "ret_5m" not in msg.split("[", 1)[1].split("]", 1)[0]


def test_required_window_bars_takes_max() -> None:
    # 1m bars: max requested is 60m -> 60 bars + 1 buffer = 61
    assert required_window_bars(["ret_5m", "rv_30m", "mom_z_60m"], bar_minutes=1) == 61
    # 5m bars: 60m / 5 = 12 bars + 1 = 13
    assert required_window_bars(["ret_5m", "mom_z_60m"], bar_minutes=5) == 13
    # daily bars: 60m / (60*24) -> ceil(1/1440) = 1 + 1 = 2
    assert required_window_bars(["ret_60m"], bar_minutes=60 * 24) == 2


def test_required_window_bars_day_suffix_on_daily_bars() -> None:
    # The whole point of the d-suffix: on daily bars, ret_5d should
    # require 5 lookback bars + 1 buffer = 6, NOT collapse to 2 like
    # ret_60m would.
    assert required_window_bars(["ret_5d"], bar_minutes=60 * 24) == 6
    assert required_window_bars(["mom_z_20d"], bar_minutes=60 * 24) == 21
    # Mixed bag on daily bars: max should be the 20d feature
    assert required_window_bars(["ret_1d", "rv_5d", "mom_z_20d"], bar_minutes=60 * 24) == 21


def test_required_window_bars_hour_suffix_on_minute_bars() -> None:
    # ret_2h on 1m bars = ceil(120/1) + 1 = 121 bars
    assert required_window_bars(["ret_2h"], bar_minutes=1) == 121
    # rv_4h on 5m bars = ceil(240/5) + 1 = 49 bars
    assert required_window_bars(["rv_4h"], bar_minutes=5) == 49


def test_required_window_bars_unit_equivalence() -> None:
    # ``ret_60m`` and ``ret_1h`` must produce identical window sizes;
    # any drift would mean parse_feature_name's normalisation is
    # inconsistent.
    for bar_minutes in (1, 5, 15, 60):
        assert required_window_bars(["ret_60m"], bar_minutes=bar_minutes) == required_window_bars(
            ["ret_1h"], bar_minutes=bar_minutes
        )
    # And ``ret_1440m`` should equal ``ret_1d``
    for bar_minutes in (1, 60, 60 * 24):
        assert required_window_bars(["ret_1440m"], bar_minutes=bar_minutes) == required_window_bars(
            ["ret_1d"], bar_minutes=bar_minutes
        )


def test_required_window_bars_rejects_bad_bar_minutes() -> None:
    with pytest.raises(ValueError, match="bar_minutes must be positive"):
        required_window_bars(["ret_5m"], bar_minutes=0)


# --------------------------------------------------------------------------- #
# compute_features                                                            #
# --------------------------------------------------------------------------- #


def test_compute_features_returns_none_for_empty_window() -> None:
    assert compute_features([], feature_names=["ret_1m"], bar_minutes=1) is None


def test_compute_features_returns_none_when_too_short() -> None:
    # Need 2 closes for ret_1m on 1m bars; supply 1.
    window = [_bar(0, 100.0)]
    assert compute_features(window, feature_names=["ret_1m"], bar_minutes=1) is None


def test_compute_features_ret_is_log_return() -> None:
    # close 100 -> 110 over 1 bar; ret_1m = log(1.1)
    window = [_bar(0, 100.0), _bar(60_000_000_000, 110.0)]
    feats = compute_features(window, feature_names=["ret_1m"], bar_minutes=1)
    assert feats is not None
    assert feats["ret_1m"] == pytest.approx(math.log(1.1), rel=1e-12)


def test_compute_features_ret_5m_on_1m_bars_uses_5_bars_back() -> None:
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]
    window = [_bar(i * 60_000_000_000, c) for i, c in enumerate(closes)]
    feats = compute_features(window, feature_names=["ret_5m"], bar_minutes=1)
    assert feats is not None
    # 5 bars back from index 5 (close=110) is index 0 (close=100).
    assert feats["ret_5m"] == pytest.approx(math.log(110 / 100), rel=1e-12)


def test_compute_features_rv_is_population_stdev_of_log_returns() -> None:
    # Three log returns: log(101/100), log(102/101), log(103/102).
    closes = [100.0, 101.0, 102.0, 103.0]
    window = [_bar(i * 60_000_000_000, c) for i, c in enumerate(closes)]
    feats = compute_features(window, feature_names=["rv_3m"], bar_minutes=1)
    assert feats is not None
    rets = [
        math.log(101 / 100),
        math.log(102 / 101),
        math.log(103 / 102),
    ]
    mean = sum(rets) / 3
    expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / 3)
    assert feats["rv_3m"] == pytest.approx(expected, rel=1e-12)


def test_compute_features_mom_z_zero_when_constant() -> None:
    # Constant close => zero log returns => stdev=0 => mom_z forced to 0.0
    window = [_bar(i * 60_000_000_000, 100.0) for i in range(5)]
    feats = compute_features(window, feature_names=["mom_z_4m"], bar_minutes=1)
    assert feats is not None
    assert feats["mom_z_4m"] == 0.0


def test_compute_features_mom_z_positive_for_uptrend() -> None:
    # Strict uptrend => mom_z > 0
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    window = [_bar(i * 60_000_000_000, c) for i, c in enumerate(closes)]
    feats = compute_features(window, feature_names=["mom_z_4m"], bar_minutes=1)
    assert feats is not None
    assert feats["mom_z_4m"] > 0


def test_compute_features_handles_multiple_features() -> None:
    closes = [100.0, 99.0, 101.0, 100.0, 102.0]
    window = [_bar(i * 60_000_000_000, c) for i, c in enumerate(closes)]
    feats = compute_features(
        window,
        feature_names=["ret_1m", "ret_4m", "rv_4m", "mom_z_4m"],
        bar_minutes=1,
    )
    assert feats is not None
    assert set(feats) == {"ret_1m", "ret_4m", "rv_4m", "mom_z_4m"}
    assert feats["ret_1m"] == pytest.approx(math.log(102 / 100), rel=1e-12)
    assert feats["ret_4m"] == pytest.approx(math.log(102 / 100), rel=1e-12)


def test_compute_features_returns_none_on_non_positive_close() -> None:
    window = [_bar(0, 100.0), _bar(60_000_000_000, 0.0)]
    assert compute_features(window, feature_names=["ret_1m"], bar_minutes=1) is None


def test_compute_features_rejects_unsupported_feature() -> None:
    window = [_bar(0, 100.0), _bar(60_000_000_000, 101.0)]
    with pytest.raises(ValueError):
        compute_features(window, feature_names=["book_imbalance_1"], bar_minutes=1)


# --------------------------------------------------------------------------- #
# Day-suffix on daily bars (the whole point of this feature)                  #
# --------------------------------------------------------------------------- #


DAY_NS = 60 * 60 * 24 * 1_000_000_000
DAILY_BAR_MINUTES = 60 * 24


def _daily_bar(day_index: int, close: float) -> BarEvent:
    return _bar(day_index * DAY_NS, close)


def test_compute_features_ret_5d_on_daily_bars_uses_5_bars_back() -> None:
    # 6 daily bars; close goes 100 -> 105 over 5 days.  ret_5d should
    # take the 5-bar lookback ratio, NOT collapse to ret_1d as the old
    # m-only convention forced.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    window = [_daily_bar(i, c) for i, c in enumerate(closes)]
    feats = compute_features(
        window,
        feature_names=["ret_5d"],
        bar_minutes=DAILY_BAR_MINUTES,
    )
    assert feats is not None
    assert feats["ret_5d"] == pytest.approx(math.log(105 / 100), rel=1e-12)


def test_compute_features_ret_5d_distinct_from_ret_1d() -> None:
    # Strict regression check: with non-trivial path, ret_1d (1-day)
    # must differ from ret_5d (5-day cumulative) under the new convention.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    window = [_daily_bar(i, c) for i, c in enumerate(closes)]
    feats = compute_features(
        window,
        feature_names=["ret_1d", "ret_5d"],
        bar_minutes=DAILY_BAR_MINUTES,
    )
    assert feats is not None
    assert feats["ret_1d"] == pytest.approx(math.log(105 / 104), rel=1e-12)
    assert feats["ret_5d"] == pytest.approx(math.log(105 / 100), rel=1e-12)
    assert feats["ret_1d"] != pytest.approx(feats["ret_5d"])


def test_compute_features_rv_3d_on_daily_bars() -> None:
    # rv_3d on daily bars = stdev of the most recent 3 daily log returns.
    closes = [100.0, 101.0, 102.5, 103.0]
    window = [_daily_bar(i, c) for i, c in enumerate(closes)]
    feats = compute_features(window, feature_names=["rv_3d"], bar_minutes=DAILY_BAR_MINUTES)
    assert feats is not None
    rets = [
        math.log(101 / 100),
        math.log(102.5 / 101),
        math.log(103 / 102.5),
    ]
    mean = sum(rets) / 3
    expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / 3)
    assert feats["rv_3d"] == pytest.approx(expected, rel=1e-12)


def test_compute_features_mom_z_3d_positive_for_uptrend_on_daily_bars() -> None:
    closes = [100.0, 101.0, 102.0, 103.0]
    window = [_daily_bar(i, c) for i, c in enumerate(closes)]
    feats = compute_features(
        window,
        feature_names=["mom_z_3d"],
        bar_minutes=DAILY_BAR_MINUTES,
    )
    assert feats is not None
    assert feats["mom_z_3d"] > 0


def test_compute_features_returns_none_when_daily_window_too_short() -> None:
    # Need at least 6 daily closes for ret_5d; supply 5.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    window = [_daily_bar(i, c) for i, c in enumerate(closes)]
    assert (
        compute_features(
            window,
            feature_names=["ret_5d"],
            bar_minutes=DAILY_BAR_MINUTES,
        )
        is None
    )


def test_compute_features_unit_equivalence_on_minute_bars() -> None:
    # ``ret_2h`` and ``ret_120m`` must produce identical values when
    # given the same window of minute bars.  Any drift would imply
    # the unit normalisation leaks into compute_features's bar-count math.
    closes = [100.0 + i * 0.1 for i in range(150)]
    window = [_bar(i * 60_000_000_000, c) for i, c in enumerate(closes)]
    feats_h = compute_features(window, feature_names=["ret_2h"], bar_minutes=1)
    feats_m = compute_features(window, feature_names=["ret_120m"], bar_minutes=1)
    assert feats_h is not None and feats_m is not None
    assert feats_h["ret_2h"] == pytest.approx(feats_m["ret_120m"], rel=1e-15)
