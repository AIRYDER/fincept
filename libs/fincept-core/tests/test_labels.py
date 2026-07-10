"""Tests for ``fincept_core.datasets.labels`` (Tier 2.3).

Tests verify:
- Triple-barrier labeling produces correct labels for each barrier hit.
- Volatility-scaled widths adapt to changing regimes.
- Meta-labeling correctly identifies when the primary signal was right.
- Edge cases: insufficient data, zero movement, degenerate widths.
- Configuration validation.
"""

from __future__ import annotations

import pytest

from fincept_core.datasets import (
    BarrierConfig,
    MetaLabelConfig,
    meta_labels,
    triple_barrier_labels,
    volatility_scaled_widths,
)

# --------------------------------------------------------------------------- #
# Triple-barrier labeling                                                     #
# --------------------------------------------------------------------------- #


class TestTripleBarrierLabels:
    def test_upper_barrier_hit(self) -> None:
        """Profit-take barrier hit first → label = +1."""
        # Bar 0: entry at 100. Upper = 103. Bar 1 high = 102 (no hit).
        # Bar 2 high = 104 (hits upper at 103).
        highs = [100, 102, 104, 105]
        lows = [99, 99, 100, 101]
        closes = [100, 101, 103, 104]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert len(labels) == 1  # 4 bars - 3 horizon = 1 label
        assert labels[0].label == 1
        assert labels[0].barrier_hit == "upper"
        assert labels[0].hit_bar == 2

    def test_lower_barrier_hit(self) -> None:
        """Stop-loss barrier hit first → label = -1."""
        # Bar 0: entry at 100. Lower = 98. Bar 1 low = 97 (hits lower).
        highs = [100, 101, 102, 103]
        lows = [99, 97, 98, 99]
        closes = [100, 99, 100, 101]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert labels[0].label == -1
        assert labels[0].barrier_hit == "lower"
        assert labels[0].hit_bar == 1

    def test_vertical_barrier_positive(self) -> None:
        """No barrier hit before timeout, positive return → label = +1."""
        # Entry at 100, upper=103, lower=98. No bar touches either.
        # Close at timeout = 101 → positive return.
        highs = [100, 101, 101, 101]
        lows = [99, 99, 99, 99]
        closes = [100, 100.5, 100.5, 101]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert labels[0].label == 1
        assert labels[0].barrier_hit == "vertical"
        assert labels[0].hit_bar == 3

    def test_vertical_barrier_negative(self) -> None:
        """No barrier hit before timeout, negative return → label = -1."""
        highs = [100, 101, 101, 101]
        lows = [99, 99, 99, 99]
        closes = [100, 99.5, 99.5, 99]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert labels[0].label == -1
        assert labels[0].barrier_hit == "vertical"

    def test_vertical_barrier_zero(self) -> None:
        """No movement at timeout → label = 0."""
        highs = [100, 100, 100, 100]
        lows = [100, 100, 100, 100]
        closes = [100, 100, 100, 100]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert labels[0].label == 0
        assert labels[0].barrier_hit == "vertical"

    def test_return_pct_correct(self) -> None:
        """return_pct is correctly computed from entry to exit."""
        highs = [100, 105, 105, 105]
        lows = [99, 99, 99, 99]
        closes = [100, 103, 103, 103]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        # Upper barrier at 103, hit at bar 1. Return = 3%.
        assert labels[0].return_pct == pytest.approx(3.0, abs=0.01)
        assert labels[0].exit_price == 103.0

    def test_multiple_labels(self) -> None:
        """Multiple bars produce multiple labels."""
        n = 20
        highs = [100 + i for i in range(n)]
        lows = [99 + i for i in range(n)]
        closes = [100 + i for i in range(n)]
        cfg = BarrierConfig(profit_take_width=0.10, stop_loss_width=0.10, horizon_bars=5)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        assert len(labels) == n - 5

    def test_last_horizon_bars_excluded(self) -> None:
        """Bars without enough future data are excluded."""
        highs = [100, 101, 102, 103, 104, 105]
        lows = [99, 99, 99, 99, 99, 99]
        closes = [100, 100, 101, 102, 103, 104]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        # 6 bars - 3 horizon = 3 labels (bars 0, 1, 2)
        assert len(labels) == 3
        assert labels[-1].index == 2

    def test_per_bar_widths(self) -> None:
        """Per-bar volatility-scaled widths are used when provided."""
        # Bar 0: pt=0.01 (tight), bar 1: pt=0.10 (wide)
        highs = [100, 102, 102, 102]
        lows = [99, 99, 99, 99]
        closes = [100, 101, 101, 101]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        # Override with per-bar widths: bar 0 has tight 1% upper
        per_bar = [(0.01, 0.01), (0.10, 0.10), (0.10, 0.10), (0.10, 0.10)]
        labels = triple_barrier_labels(highs, lows, closes, cfg, per_bar_widths=per_bar)
        # Bar 0: upper = 101. Bar 1 high = 102 → hits upper at 101.
        assert labels[0].label == 1
        assert labels[0].barrier_hit == "upper"
        assert labels[0].exit_price == 101.0

    def test_mismatched_lengths(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            triple_barrier_labels(
                [100, 101],
                [99],
                [100, 101],
                BarrierConfig(profit_take_width=0.02, stop_loss_width=0.02, horizon_bars=1),
            )

    def test_invalid_profit_take(self) -> None:
        with pytest.raises(ValueError, match="profit_take_width"):
            triple_barrier_labels(
                [100],
                [99],
                [100],
                BarrierConfig(profit_take_width=0.0, stop_loss_width=0.02, horizon_bars=1),
            )

    def test_invalid_stop_loss(self) -> None:
        with pytest.raises(ValueError, match="stop_loss_width"):
            triple_barrier_labels(
                [100],
                [99],
                [100],
                BarrierConfig(profit_take_width=0.02, stop_loss_width=-0.01, horizon_bars=1),
            )

    def test_invalid_horizon(self) -> None:
        with pytest.raises(ValueError, match="horizon_bars"):
            triple_barrier_labels(
                [100],
                [99],
                [100],
                BarrierConfig(profit_take_width=0.02, stop_loss_width=0.02, horizon_bars=0),
            )

    def test_label_is_frozen(self) -> None:
        """TripleBarrierLabel is immutable."""
        highs = [100, 102, 102, 102]
        lows = [99, 99, 99, 99]
        closes = [100, 101, 101, 101]
        cfg = BarrierConfig(profit_take_width=0.03, stop_loss_width=0.02, horizon_bars=3)
        labels = triple_barrier_labels(highs, lows, closes, cfg)
        with pytest.raises(Exception):
            labels[0].label = 99  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Volatility-scaled widths                                                    #
# --------------------------------------------------------------------------- #


class TestVolatilityScaledWidths:
    def test_basic(self) -> None:
        """Returns one width pair per bar."""
        closes = [100, 101, 99, 102, 98, 103, 97]
        widths = volatility_scaled_widths(closes, window=3)
        assert len(widths) == len(closes)
        for pt, sl in widths:
            assert pt > 0
            assert sl > 0

    def test_higher_vol_wider_widths(self) -> None:
        """High-volatility period produces wider barriers."""
        # Low vol: small changes
        low_vol = [100, 100.1, 100.05, 100.1, 100.05, 100.1, 100.05, 100.1]
        # High vol: large swings
        high_vol = [100, 105, 95, 105, 95, 105, 95, 105]
        w_low = volatility_scaled_widths(low_vol, window=4)
        w_high = volatility_scaled_widths(high_vol, window=4)
        # The last width pair should be wider for high-vol
        assert w_high[-1][0] > w_low[-1][0]  # pt
        assert w_high[-1][1] > w_low[-1][1]  # sl

    def test_sigma_multipliers(self) -> None:
        """profit_take_sigma and stop_loss_sigma scale independently."""
        closes = [100, 105, 95, 105, 95, 105]
        w_default = volatility_scaled_widths(closes, window=3)
        w_pt2 = volatility_scaled_widths(closes, window=3, profit_take_sigma=2.0)
        w_sl05 = volatility_scaled_widths(closes, window=3, stop_loss_sigma=0.5)
        assert w_pt2[-1][0] == pytest.approx(2.0 * w_default[-1][0])
        assert w_sl05[-1][1] == pytest.approx(0.5 * w_default[-1][1])

    def test_min_volatility_floor(self) -> None:
        """Zero-volatility period uses the min_volatility floor."""
        closes = [100, 100, 100, 100, 100]
        widths = volatility_scaled_widths(closes, window=3, min_volatility=0.001)
        for pt, sl in widths:
            assert pt >= 0.001
            assert sl >= 0.001

    def test_single_bar(self) -> None:
        """Single bar returns one pair with min_volatility."""
        widths = volatility_scaled_widths([100], min_volatility=0.01)
        assert len(widths) == 1
        assert widths[0][0] >= 0.01


# --------------------------------------------------------------------------- #
# Meta-labeling                                                               #
# --------------------------------------------------------------------------- #


class TestMetaLabels:
    def test_correct_signal(self) -> None:
        """Primary signal matches barrier label → meta = 1."""
        sides = [1, -1, 1, -1]
        labels = [1, -1, 1, -1]
        ml = meta_labels(sides, labels)
        assert ml == [1, 1, 1, 1]

    def test_wrong_signal(self) -> None:
        """Primary signal mismatches barrier label → meta = 0."""
        sides = [1, -1, 1, -1]
        labels = [-1, 1, -1, 1]
        ml = meta_labels(sides, labels)
        assert ml == [0, 0, 0, 0]

    def test_mixed(self) -> None:
        """Mixed correct/wrong signals."""
        sides = [1, -1, 1, 1, -1]
        labels = [1, -1, -1, 1, 1]
        ml = meta_labels(sides, labels)
        assert ml == [1, 1, 0, 1, 0]

    def test_zero_barrier_label(self) -> None:
        """Zero barrier label (no movement) → meta = 0 (no edge)."""
        sides = [1, -1]
        labels = [0, 0]
        ml = meta_labels(sides, labels)
        assert ml == [0, 0]

    def test_zero_side(self) -> None:
        """Zero side (no signal) → meta = 0."""
        sides = [0, 0]
        labels = [1, -1]
        ml = meta_labels(sides, labels)
        assert ml == [0, 0]

    def test_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            meta_labels([1, -1, 1], [1, -1])

    def test_empty(self) -> None:
        assert meta_labels([], []) == []

    def test_all_correct(self) -> None:
        """All signals correct → all meta = 1."""
        sides = [1] * 10
        labels = [1] * 10
        ml = meta_labels(sides, labels)
        assert ml == [1] * 10

    def test_config_accepted(self) -> None:
        """Config object is accepted but doesn't change core logic."""
        sides = [1, -1]
        labels = [1, -1]
        cfg = MetaLabelConfig(side_column="s", label_column="l")
        ml = meta_labels(sides, labels, cfg)
        assert ml == [1, 1]
