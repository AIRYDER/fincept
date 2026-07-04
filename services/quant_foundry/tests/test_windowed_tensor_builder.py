"""Tests for quant_foundry.windowed_tensor_builder (T-10.2).

Tests verify:
- WindowedTensorConfig construction, defaults, and validation.
- WindowedTensor construction, shape validation, and leakage checks.
- WindowedTensorReceipt construction and consistency checks.
- WindowedTensorBuilder.build with synthetic data.
- NPZ output format (write + reload).
- Parquet output format (skip if pyarrow unavailable).
- validate_output (valid, missing file, hash mismatch, count mismatch).
- compute_tensor_hash determinism and order-independence.
- validate_no_label_in_features (valid, leakage detected).
- Fail-closed: future leakage, label in features.
- Deterministic output for same fixture.
- Edge cases: single symbol, single window, stride == window_length.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from quant_foundry.sequence_manifest import (
    SequenceChannel,
    SequenceDatasetManifest,
    SequenceManifestBuilder,
    compute_sequence_data_hash,
)
from quant_foundry.windowed_tensor_builder import (
    WindowedTensor,
    WindowedTensorBuilder,
    WindowedTensorConfig,
    WindowedTensorReceipt,
    compute_tensor_hash,
    validate_no_label_in_features,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> WindowedTensorConfig:
    """Build a valid WindowedTensorConfig with defaults."""
    base = dict(
        window_length=3,
        stride=1,
        horizons=[1],
        channels=["close", "volume"],
        output_format="npz",
        include_symbol=True,
        include_timestamp=True,
        include_window_id=True,
    )
    base.update(overrides)
    return WindowedTensorConfig(**base)


def _make_window_kwargs(**overrides) -> dict:
    """Build kwargs for a valid WindowedTensor (window_length=3, 2 channels)."""
    base = dict(
        window_id="AAPL_2024-01-01T00:00:00Z_2024-01-03T00:00:00Z_1",
        symbol="AAPL",
        start_timestamp="2024-01-01T00:00:00Z",
        end_timestamp="2024-01-03T00:00:00Z",
        label_timestamp="2024-01-04T00:00:00Z",
        horizon=1,
        data=[[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]],
        label=0.05,
        weight=1.0,
    )
    base.update(overrides)
    return base


def _make_window(**overrides) -> WindowedTensor:
    """Build a valid WindowedTensor."""
    return WindowedTensor(**_make_window_kwargs(**overrides))


def _make_manifest(**overrides) -> SequenceDatasetManifest:
    """Build a valid SequenceDatasetManifest for receipts."""
    builder = (
        SequenceManifestBuilder("seq_test_001")
        .with_symbols(["AAPL", "MSFT"])
        .with_channels(
            [
                SequenceChannel(name="close", dtype="float32"),
                SequenceChannel(name="volume", dtype="float64"),
            ]
        )
        .with_window(length=3, stride=1)
        .with_horizons([1])
        .with_time_range(
            start="2024-01-01T00:00:00Z",
            end="2024-06-01T00:00:00Z",
            label_ts="2024-06-02T00:00:00Z",
            avail_cutoff="2024-06-02T00:00:00Z",
        )
        .with_data(
            uri="s3://bucket/seq_test_001.npy",
            data_hash=compute_sequence_data_hash(np.zeros((10, 3, 2), dtype=np.float32)),
        )
        .with_created_at("2024-01-01T00:00:00Z")
    )
    manifest = builder.build()
    return manifest


def _make_synthetic_df(
    n_symbols: int = 2,
    n_rows: int = 10,
    start_date: str = "2024-01-01",
) -> Any:
    """Build a synthetic daily-bar dataframe.

    Columns: symbol, timestamp, close, volume. The close price increases
    by 1.0 per row per symbol (so future returns are deterministic and
    non-zero), and volume is constant.
    """
    import pandas as pd

    rows = []
    symbols = ["AAPL", "MSFT", "GOOG", "AMZN"][:n_symbols]
    for sym in symbols:
        for i in range(n_rows):
            ts = f"2024-01-{i + 1:02d}T00:00:00Z"
            rows.append(
                {
                    "symbol": sym,
                    "timestamp": ts,
                    "close": float(100 + i),  # 100, 101, 102, ...
                    "volume": float(1000 + i),
                }
            )
    return pd.DataFrame(rows)


def _has_pyarrow() -> bool:
    """Check whether pyarrow is importable."""
    try:
        importlib.import_module("pyarrow")
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# WindowedTensorConfig
# ---------------------------------------------------------------------------


class TestWindowedTensorConfig:
    """Tests for WindowedTensorConfig construction and validation."""

    def test_default_construction(self) -> None:
        cfg = _make_config()
        assert cfg.window_length == 3
        assert cfg.stride == 1
        assert cfg.horizons == [1]
        assert cfg.channels == ["close", "volume"]
        assert cfg.output_format == "npz"
        assert cfg.include_symbol is True
        assert cfg.include_timestamp is True
        assert cfg.include_window_id is True

    def test_frozen(self) -> None:
        cfg = _make_config()
        with pytest.raises(Exception):
            cfg.window_length = 5  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            WindowedTensorConfig(
                window_length=3,
                stride=1,
                horizons=[1],
                channels=["close"],
                bogus_field=42,
            )

    def test_window_length_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            _make_config(window_length=0)
        with pytest.raises(Exception):
            _make_config(window_length=-1)

    def test_stride_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            _make_config(stride=0)
        with pytest.raises(Exception):
            _make_config(stride=-1)

    def test_horizons_at_least_one(self) -> None:
        with pytest.raises(Exception):
            _make_config(horizons=[])

    def test_horizons_each_ge_one(self) -> None:
        with pytest.raises(Exception):
            _make_config(horizons=[0])
        with pytest.raises(Exception):
            _make_config(horizons=[1, -1])

    def test_channels_at_least_one(self) -> None:
        with pytest.raises(Exception):
            _make_config(channels=[])

    def test_channels_nonempty_strings(self) -> None:
        with pytest.raises(Exception):
            _make_config(channels=["close", ""])
        with pytest.raises(Exception):
            _make_config(channels=["close", "   "])

    def test_no_duplicate_channels(self) -> None:
        with pytest.raises(Exception):
            _make_config(channels=["close", "close"])

    def test_no_duplicate_horizons(self) -> None:
        with pytest.raises(Exception):
            _make_config(horizons=[1, 1])

    def test_output_format_default_npz(self) -> None:
        cfg = WindowedTensorConfig(window_length=3, stride=1, horizons=[1], channels=["close"])
        assert cfg.output_format == "npz"

    def test_output_format_parquet_allowed(self) -> None:
        cfg = _make_config(output_format="parquet")
        assert cfg.output_format == "parquet"

    def test_output_format_invalid(self) -> None:
        with pytest.raises(Exception):
            _make_config(output_format="csv")
        with pytest.raises(Exception):
            _make_config(output_format="")

    def test_equality(self) -> None:
        cfg1 = _make_config()
        cfg2 = _make_config()
        assert cfg1 == cfg2


# ---------------------------------------------------------------------------
# WindowedTensor
# ---------------------------------------------------------------------------


class TestWindowedTensor:
    """Tests for WindowedTensor construction and validation."""

    def test_default_construction(self) -> None:
        w = _make_window()
        assert w.window_id.startswith("AAPL_")
        assert w.symbol == "AAPL"
        assert w.horizon == 1
        assert len(w.data) == 3
        assert all(len(row) == 2 for row in w.data)
        assert w.label == 0.05
        assert w.weight == 1.0

    def test_frozen(self) -> None:
        w = _make_window()
        with pytest.raises(Exception):
            w.label = 0.99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            WindowedTensor(**{**_make_window_kwargs(), "bogus": 1})

    def test_window_id_nonempty(self) -> None:
        with pytest.raises(Exception):
            _make_window(window_id="")

    def test_symbol_nonempty(self) -> None:
        with pytest.raises(Exception):
            _make_window(symbol="")

    def test_temporal_parseable(self) -> None:
        with pytest.raises(Exception):
            _make_window(start_timestamp="not-a-date")
        with pytest.raises(Exception):
            _make_window(end_timestamp="")
        with pytest.raises(Exception):
            _make_window(label_timestamp="2024-99-99")

    def test_horizon_positive(self) -> None:
        with pytest.raises(Exception):
            _make_window(horizon=0)
        with pytest.raises(Exception):
            _make_window(horizon=-1)

    def test_data_nonempty(self) -> None:
        with pytest.raises(Exception):
            _make_window(data=[])

    def test_data_rows_same_length(self) -> None:
        with pytest.raises(Exception):
            _make_window(data=[[1.0, 2.0], [3.0], [4.0, 5.0]])

    def test_data_rows_at_least_one_column(self) -> None:
        with pytest.raises(Exception):
            _make_window(data=[[], [], []])

    def test_label_timestamp_after_end(self) -> None:
        with pytest.raises(Exception):
            _make_window(
                end_timestamp="2024-01-05T00:00:00Z",
                label_timestamp="2024-01-04T00:00:00Z",
            )

    def test_label_timestamp_equal_end_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_window(
                end_timestamp="2024-01-03T00:00:00Z",
                label_timestamp="2024-01-03T00:00:00Z",
            )

    def test_weight_default(self) -> None:
        w = WindowedTensor(
            window_id="AAPL_s_e_1",
            symbol="AAPL",
            start_timestamp="2024-01-01T00:00:00Z",
            end_timestamp="2024-01-03T00:00:00Z",
            label_timestamp="2024-01-04T00:00:00Z",
            horizon=1,
            data=[[1.0], [2.0], [3.0]],
            label=0.1,
        )
        assert w.weight == 1.0

    def test_window_length_property(self) -> None:
        w = _make_window()
        assert w.window_length == 3

    def test_n_channels_property(self) -> None:
        w = _make_window()
        assert w.n_channels == 2


# ---------------------------------------------------------------------------
# WindowedTensorReceipt
# ---------------------------------------------------------------------------


class TestWindowedTensorReceipt:
    """Tests for WindowedTensorReceipt construction and validation."""

    def test_valid_construction(self) -> None:
        manifest = _make_manifest()
        receipt = WindowedTensorReceipt(
            manifest=manifest,
            n_windows=2,
            n_symbols=1,
            output_path="/tmp/out.npz",
            output_hash="a" * 64,
            created_at="2024-01-01T00:00:00Z",
            window_ids=["w1", "w2"],
        )
        assert receipt.n_windows == 2
        assert receipt.n_symbols == 1
        assert receipt.output_hash == "a" * 64

    def test_frozen(self) -> None:
        manifest = _make_manifest()
        receipt = WindowedTensorReceipt(
            manifest=manifest,
            n_windows=1,
            n_symbols=1,
            output_path="/tmp/out.npz",
            output_hash="a" * 64,
            created_at="2024-01-01T00:00:00Z",
            window_ids=["w1"],
        )
        with pytest.raises(Exception):
            receipt.n_windows = 5  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=1,
                n_symbols=1,
                output_path="/tmp/out.npz",
                output_hash="a" * 64,
                created_at="2024-01-01T00:00:00Z",
                window_ids=["w1"],
                bogus=1,
            )

    def test_n_windows_nonnegative(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=-1,
                n_symbols=1,
                output_path="/tmp/out.npz",
                output_hash="a" * 64,
                created_at="2024-01-01T00:00:00Z",
                window_ids=[],
            )

    def test_n_symbols_positive(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=1,
                n_symbols=0,
                output_path="/tmp/out.npz",
                output_hash="a" * 64,
                created_at="2024-01-01T00:00:00Z",
                window_ids=["w1"],
            )

    def test_output_hash_must_be_hex64(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=1,
                n_symbols=1,
                output_path="/tmp/out.npz",
                output_hash="xyz",
                created_at="2024-01-01T00:00:00Z",
                window_ids=["w1"],
            )

    def test_window_count_matches_window_ids(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=3,
                n_symbols=1,
                output_path="/tmp/out.npz",
                output_hash="a" * 64,
                created_at="2024-01-01T00:00:00Z",
                window_ids=["w1", "w2"],
            )

    def test_output_path_nonempty(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=1,
                n_symbols=1,
                output_path="",
                output_hash="a" * 64,
                created_at="2024-01-01T00:00:00Z",
                window_ids=["w1"],
            )

    def test_created_at_parseable(self) -> None:
        manifest = _make_manifest()
        with pytest.raises(Exception):
            WindowedTensorReceipt(
                manifest=manifest,
                n_windows=1,
                n_symbols=1,
                output_path="/tmp/out.npz",
                output_hash="a" * 64,
                created_at="not-a-date",
                window_ids=["w1"],
            )


# ---------------------------------------------------------------------------
# compute_tensor_hash
# ---------------------------------------------------------------------------


class TestComputeTensorHash:
    """Tests for compute_tensor_hash determinism."""

    def test_deterministic_same_windows(self) -> None:
        w1 = _make_window()
        w2 = _make_window()
        assert compute_tensor_hash([w1]) == compute_tensor_hash([w2])

    def test_deterministic_order_independent(self) -> None:
        w1 = _make_window(window_id="AAA_s_e_1", symbol="AAA")
        w2 = _make_window(window_id="BBB_s_e_1", symbol="BBB")
        h1 = compute_tensor_hash([w1, w2])
        h2 = compute_tensor_hash([w2, w1])
        assert h1 == h2

    def test_different_data_different_hash(self) -> None:
        w1 = _make_window(label=0.05)
        w2 = _make_window(label=0.10)
        assert compute_tensor_hash([w1]) != compute_tensor_hash([w2])

    def test_different_window_id_different_hash(self) -> None:
        w1 = _make_window(window_id="AAA_s_e_1")
        w2 = _make_window(window_id="BBB_s_e_1")
        assert compute_tensor_hash([w1]) != compute_tensor_hash([w2])

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_tensor_hash([])

    def test_returns_64_char_hex(self) -> None:
        h = compute_tensor_hash([_make_window()])
        assert len(h) == 64
        int(h, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# validate_no_label_in_features
# ---------------------------------------------------------------------------


class TestValidateNoLabelInFeatures:
    """Tests for validate_no_label_in_features."""

    def test_no_leakage_returns_true(self) -> None:
        w = _make_window(label=999.0, data=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        assert validate_no_label_in_features(w) is True

    def test_label_in_features_raises(self) -> None:
        w = _make_window(
            label=3.0,
            data=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
        )
        with pytest.raises(ValueError, match="label leakage"):
            validate_no_label_in_features(w)

    def test_label_in_second_channel_raises(self) -> None:
        w = _make_window(
            label=6.0,
            data=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
        )
        with pytest.raises(ValueError, match="label leakage"):
            validate_no_label_in_features(w)

    def test_label_zero_not_flagged_if_not_present(self) -> None:
        w = _make_window(
            label=0.0,
            data=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
        )
        assert validate_no_label_in_features(w) is True

    def test_label_zero_flagged_if_present(self) -> None:
        w = _make_window(
            label=0.0,
            data=[[1.0, 0.0], [3.0, 4.0], [5.0, 6.0]],
        )
        with pytest.raises(ValueError, match="label leakage"):
            validate_no_label_in_features(w)


# ---------------------------------------------------------------------------
# WindowedTensorBuilder.build
# ---------------------------------------------------------------------------


class TestWindowedTensorBuilderBuild:
    """Tests for WindowedTensorBuilder.build with synthetic data."""

    def test_build_basic_npz(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=2, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.n_windows > 0
        assert receipt.n_symbols == 2
        assert Path(out).exists()
        assert receipt.output_hash != ""

    def test_build_window_count_matches(self, tmp_path: Path) -> None:
        # window_length=3, stride=1, horizons=[1], n_rows=10
        # For each symbol: windows where label_idx < 10.
        # i ranges 0..7 (n - wl = 7), label_idx = i+2+1 = i+3 < 10 => i < 7
        # So i in 0..6 => 7 windows per symbol, 2 symbols => 14 windows.
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=2, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.n_windows == 14
        assert len(receipt.window_ids) == 14

    def test_build_window_ids_sorted(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=2, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.window_ids == sorted(receipt.window_ids)

    def test_build_deterministic_hash(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        manifest = _make_manifest()
        df = _make_synthetic_df(n_symbols=2, n_rows=10)
        out1 = str(tmp_path / "out1.npz")
        out2 = str(tmp_path / "out2.npz")
        r1 = WindowedTensorBuilder(cfg).build(df, manifest, out1)
        r2 = WindowedTensorBuilder(cfg).build(df, manifest, out2)
        assert r1.output_hash == r2.output_hash
        assert r1.window_ids == r2.window_ids
        assert r1.n_windows == r2.n_windows

    def test_build_different_data_different_hash(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        manifest = _make_manifest()
        df1 = _make_synthetic_df(n_symbols=2, n_rows=10)
        df2 = _make_synthetic_df(n_symbols=2, n_rows=10)
        # Modify df2 close prices.
        df2.loc[df2["symbol"] == "AAPL", "close"] = 999.0
        out1 = str(tmp_path / "out1.npz")
        out2 = str(tmp_path / "out2.npz")
        r1 = WindowedTensorBuilder(cfg).build(df1, manifest, out1)
        r2 = WindowedTensorBuilder(cfg).build(df2, manifest, out2)
        assert r1.output_hash != r2.output_hash

    def test_build_no_future_leakage(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=2, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # Reload windows and verify label_timestamp > end_timestamp.
        loaded = builder._load_windows(out)
        for w in loaded:
            from quant_foundry.dataset_manifest import _parse_temporal

            assert _parse_temporal(w.label_timestamp) > _parse_temporal(w.end_timestamp)

    def test_build_missing_columns_raises(self, tmp_path: Path) -> None:
        import pandas as pd

        cfg = _make_config(channels=["close", "volume"])
        builder = WindowedTensorBuilder(cfg)
        df = pd.DataFrame(
            {"symbol": ["AAPL"], "timestamp": ["2024-01-01T00:00:00Z"], "close": [1.0]}
        )
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        with pytest.raises(ValueError, match="missing required columns"):
            builder.build(df, manifest, out)

    def test_build_no_windows_raises(self, tmp_path: Path) -> None:
        # Only 2 rows, but window_length=3 + horizon=1 => need >= 4.
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=2)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        with pytest.raises(ValueError, match="no windows"):
            builder.build(df, manifest, out)

    def test_build_multiple_horizons(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1, 2])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # For horizon=1: i in 0..6 (label_idx=i+3<10 => i<7) => 7 windows
        # For horizon=2: i in 0..5 (label_idx=i+4<10 => i<6) => 6 windows
        assert receipt.n_windows == 13

    def test_build_stride_equals_window_length(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=3, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # i in {0, 3, 6}; label_idx = i+3 < 10 => all three valid => 3 windows
        assert receipt.n_windows == 3

    def test_build_single_symbol(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.n_symbols == 1

    def test_build_single_window(self, tmp_path: Path) -> None:
        # n_rows=4, window_length=3, stride=1, horizon=1 => only i=0 valid.
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=4)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.n_windows == 1

    def test_build_label_not_in_features(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        loaded = builder._load_windows(out)
        for w in loaded:
            # The label is a return ratio (small float); features are
            # close prices (100+) and volume (1000+). Should never match.
            assert validate_no_label_in_features(w) is True

    def test_build_label_leakage_fail_closed(self, tmp_path: Path) -> None:
        """If the label value appears in the features, build must fail."""
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        # Patch _extract_windows to inject a label-equal value.
        original = builder._extract_windows

        def patched(d):
            windows = original(d)
            for w in windows:
                # Inject the label into the feature data.
                w.data[0][0] = w.label  # type: ignore[misc]
            return windows

        builder._extract_windows = patched  # type: ignore[assignment]
        with pytest.raises(ValueError, match="label leakage"):
            builder.build(df, manifest, out)


# ---------------------------------------------------------------------------
# NPZ output
# ---------------------------------------------------------------------------


class TestNpzOutput:
    """Tests for the NPZ output format."""

    def test_npz_file_exists(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        assert Path(out).exists()

    def test_npz_contains_data_array(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        archive = np.load(out, allow_pickle=True)
        assert "data" in archive.files
        assert archive["data"].shape[0] == receipt.n_windows
        assert archive["data"].shape[1] == 3  # window_length
        assert archive["data"].shape[2] == 2  # n_channels

    def test_npz_contains_metadata(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        archive = np.load(out, allow_pickle=True)
        for key in [
            "data",
            "label",
            "weight",
            "horizon",
            "symbol",
            "start_timestamp",
            "end_timestamp",
            "label_timestamp",
            "window_id",
        ]:
            assert key in archive.files, f"missing {key}"

    def test_npz_exclude_symbol(self, tmp_path: Path) -> None:
        cfg = _make_config(include_symbol=False)
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        archive = np.load(out, allow_pickle=True)
        assert "symbol" not in archive.files

    def test_npz_exclude_timestamp(self, tmp_path: Path) -> None:
        cfg = _make_config(include_timestamp=False)
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        archive = np.load(out, allow_pickle=True)
        assert "start_timestamp" not in archive.files
        assert "end_timestamp" not in archive.files
        assert "label_timestamp" not in archive.files

    def test_npz_exclude_window_id(self, tmp_path: Path) -> None:
        cfg = _make_config(include_window_id=False)
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        builder.build(df, manifest, out)
        archive = np.load(out, allow_pickle=True)
        assert "window_id" not in archive.files

    def test_npz_build_npz_direct(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        windows = [_make_window(), _make_window(window_id="AAPL_b_e_1")]
        out = str(tmp_path / "direct.npz")
        result = builder.build_npz(windows, out)
        assert result == out
        assert Path(out).exists()
        archive = np.load(out, allow_pickle=True)
        assert archive["data"].shape[0] == 2


# ---------------------------------------------------------------------------
# Parquet output
# ---------------------------------------------------------------------------


class TestParquetOutput:
    """Tests for the Parquet output format (skipped if no pyarrow)."""

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not available",
    )
    def test_parquet_file_exists(self, tmp_path: Path) -> None:
        cfg = _make_config(output_format="parquet")
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.parquet")
        builder.build(df, manifest, out)
        assert Path(out).exists()

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not available",
    )
    def test_parquet_row_count(self, tmp_path: Path) -> None:
        import pandas as pd

        cfg = _make_config(output_format="parquet", window_length=3, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.parquet")
        receipt = builder.build(df, manifest, out)
        loaded = pd.read_parquet(out)
        assert len(loaded) == receipt.n_windows

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not available",
    )
    def test_parquet_build_direct(self, tmp_path: Path) -> None:
        cfg = _make_config(output_format="parquet")
        builder = WindowedTensorBuilder(cfg)
        windows = [_make_window(), _make_window(window_id="AAPL_b_e_1")]
        out = str(tmp_path / "direct.parquet")
        result = builder.build_parquet(windows, out)
        assert result == out
        assert Path(out).exists()

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not available",
    )
    def test_parquet_deterministic_hash(self, tmp_path: Path) -> None:
        cfg = _make_config(output_format="parquet", window_length=3, horizons=[1])
        manifest = _make_manifest()
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        out1 = str(tmp_path / "out1.parquet")
        out2 = str(tmp_path / "out2.parquet")
        r1 = WindowedTensorBuilder(cfg).build(df, manifest, out1)
        r2 = WindowedTensorBuilder(cfg).build(df, manifest, out2)
        assert r1.output_hash == r2.output_hash


# ---------------------------------------------------------------------------
# validate_output
# ---------------------------------------------------------------------------


class TestValidateOutput:
    """Tests for WindowedTensorBuilder.validate_output."""

    def test_validate_output_valid(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert builder.validate_output(receipt, out) is True

    def test_validate_output_missing_file(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        manifest = _make_manifest()
        receipt = WindowedTensorReceipt(
            manifest=manifest,
            n_windows=1,
            n_symbols=1,
            output_path="/nonexistent/out.npz",
            output_hash="a" * 64,
            created_at="2024-01-01T00:00:00Z",
            window_ids=["w1"],
        )
        with pytest.raises(ValueError, match="does not exist"):
            builder.validate_output(receipt, str(tmp_path / "nope.npz"))

    def test_validate_output_hash_mismatch(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        bad_receipt = receipt.model_copy(update={"output_hash": "b" * 64})
        with pytest.raises(ValueError, match="hash mismatch"):
            builder.validate_output(bad_receipt, out)

    def test_validate_output_count_mismatch(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        bad_receipt = receipt.model_copy(
            update={
                "n_windows": receipt.n_windows + 100,
                "window_ids": receipt.window_ids + ["fake"] * 100,
            }
        )
        with pytest.raises(ValueError, match="window count mismatch"):
            builder.validate_output(bad_receipt, out)

    @pytest.mark.skipif(
        not _has_pyarrow(),
        reason="pyarrow not available",
    )
    def test_validate_output_parquet(self, tmp_path: Path) -> None:
        cfg = _make_config(output_format="parquet")
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.parquet")
        receipt = builder.build(df, manifest, out)
        assert builder.validate_output(receipt, out) is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge-case tests for the builder."""

    def test_single_symbol_single_window(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=4)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.n_windows == 1
        assert receipt.n_symbols == 1

    def test_stride_larger_than_window(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=3, stride=5, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # i in {0, 5}; label_idx = i+3 < 10 => both valid => 2 windows
        assert receipt.n_windows == 2

    def test_large_horizon_skips_windows(self, tmp_path: Path) -> None:
        """When the horizon is too large for the data, no windows can be
        built and build fail-closes with 'no windows'."""
        cfg = _make_config(window_length=3, stride=1, horizons=[8])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        # n_rows=10, wl=3, max_horizon=8. Need n >= wl + max_horizon = 11.
        # n=10 < 11 => the symbol is skipped entirely => no windows => raises.
        with pytest.raises(ValueError, match="no windows"):
            builder.build(df, manifest, out)

    def test_large_horizon_with_enough_data(self, tmp_path: Path) -> None:
        """When the horizon is large but enough data exists, windows are
        built only for positions where the label index is in range."""
        cfg = _make_config(window_length=3, stride=1, horizons=[8])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=12)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # n=12, wl=3, max_horizon=8. n >= wl+max_horizon=11 => ok.
        # i in 0..9 (n-wl=9). label_idx = i+2+8 = i+10 < 12 => i < 2.
        # So i in {0, 1} => 2 windows.
        assert receipt.n_windows == 2

    def test_window_length_one(self, tmp_path: Path) -> None:
        cfg = _make_config(window_length=1, stride=1, horizons=[1])
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=5)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        # i in 0..4; label_idx = i+0+1 = i+1 < 5 => i < 4 => 4 windows
        assert receipt.n_windows == 4

    def test_deterministic_across_builders(self, tmp_path: Path) -> None:
        """Two separate builder instances produce the same hash."""
        cfg = _make_config(window_length=3, stride=2, horizons=[1, 2])
        manifest = _make_manifest()
        df = _make_synthetic_df(n_symbols=2, n_rows=12)
        out1 = str(tmp_path / "o1.npz")
        out2 = str(tmp_path / "o2.npz")
        r1 = WindowedTensorBuilder(cfg).build(df, manifest, out1)
        r2 = WindowedTensorBuilder(cfg).build(df, manifest, out2)
        assert r1.output_hash == r2.output_hash
        assert r1.window_ids == r2.window_ids

    def test_receipt_contains_manifest(self, tmp_path: Path) -> None:
        cfg = _make_config()
        builder = WindowedTensorBuilder(cfg)
        df = _make_synthetic_df(n_symbols=1, n_rows=10)
        manifest = _make_manifest()
        out = str(tmp_path / "out.npz")
        receipt = builder.build(df, manifest, out)
        assert receipt.manifest is manifest or receipt.manifest == manifest
        assert receipt.manifest.dataset_id == manifest.dataset_id
