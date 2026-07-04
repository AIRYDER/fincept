"""Tests for quant_foundry.sequence_manifest (T-10.1 SequenceDatasetManifest).

Tests verify:
- SequenceChannel construction, defaults, and validation.
- SequenceDatasetManifest construction, leakage-safe invariants, duplicate
  detection, time ordering.
- WindowSpec construction and ordering validators.
- validate_no_future_leakage (valid, invalid, edge cases).
- validate_fold_assignment (valid, mismatched, invalid fold_ids).
- compute_sequence_data_hash determinism.
- create_windows window generation.
- SequenceManifestBuilder fluent API.
- Fail-closed: future leakage, duplicate symbols/channels, invalid time
  ranges.
- Edge cases: single symbol, single channel, single horizon.
"""

from __future__ import annotations

import pytest
from quant_foundry.dataset_manifest import (
    FoldSpec,
    FoldWindow,
    compute_fold_hash,
)
from quant_foundry.sequence_manifest import (
    SequenceChannel,
    SequenceDatasetManifest,
    SequenceManifestBuilder,
    WindowSpec,
    compute_sequence_data_hash,
    create_windows,
    validate_fold_assignment,
    validate_no_future_leakage,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_channel(
    name: str = "close",
    dtype: str = "float32",
    normalization: str = "standard",
    missing_policy: str = "fail",
) -> SequenceChannel:
    """Build a SequenceChannel with defaults."""
    return SequenceChannel(
        name=name,
        dtype=dtype,
        normalization=normalization,
        missing_policy=missing_policy,
    )


def _make_manifest_kwargs(**overrides) -> dict:
    """Build kwargs for a valid SequenceDatasetManifest."""
    base = dict(
        dataset_id="seq_001",
        symbols=["AAPL", "MSFT"],
        channels=[_make_channel("close"), _make_channel("volume", "float64")],
        window_length=60,
        stride=5,
        horizons=[1, 5, 21],
        window_start="2024-01-01T00:00:00Z",
        window_end="2024-06-01T00:00:00Z",
        label_timestamp="2024-06-02T00:00:00Z",
        availability_cutoff="2024-06-02T00:00:00Z",
        data_uri="s3://bucket/seq_001.npy",
        data_hash="a" * 64,
        created_at="2024-01-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> SequenceDatasetManifest:
    """Build a valid SequenceDatasetManifest."""
    return SequenceDatasetManifest(**_make_manifest_kwargs(**overrides))


def _make_window_kwargs(**overrides) -> dict:
    """Build kwargs for a valid WindowSpec."""
    base = dict(
        window_id="AAPL_2024-01-01T00:00:00Z_2024-01-60T00:00:00Z_1",
        symbol="AAPL",
        start="2024-01-01T00:00:00Z",
        end="2024-01-02T00:00:00Z",
        label_timestamp="2024-01-03T00:00:00Z",
        horizon=1,
        fold_id=None,
    )
    base.update(overrides)
    return base


def _make_window(**overrides) -> WindowSpec:
    """Build a valid WindowSpec."""
    return WindowSpec(**_make_window_kwargs(**overrides))


def _make_fold_spec(n_folds: int = 2) -> FoldSpec:
    """Build a valid FoldSpec with ``n_folds`` non-overlapping folds."""
    folds = []
    # Non-overlapping 2-month blocks per fold.
    # Fold 0: train Jan-Feb, val Mar
    # Fold 1: train Apr-May, val Jun
    # Fold 2: train Jul-Aug, val Sep
    ranges = [
        ("2024-01-01", "2024-02-28", "2024-03-01", "2024-03-31"),
        ("2024-04-01", "2024-05-31", "2024-06-01", "2024-06-30"),
        ("2024-07-01", "2024-08-31", "2024-09-01", "2024-09-30"),
    ]
    for i in range(n_folds):
        ts, te, vs, ve = ranges[i]
        folds.append(
            FoldWindow(
                fold_id=i,
                train_start=ts,
                train_end=te,
                validation_start=vs,
                validation_end=ve,
            )
        )
    return FoldSpec(
        folds=folds,
        fold_assignment_hash=compute_fold_hash(folds),
        row_id_columns=["symbol", "timestamp"],
    )


# ---------------------------------------------------------------------------
# SequenceChannel tests
# ---------------------------------------------------------------------------


class TestSequenceChannel:
    """Tests for SequenceChannel construction and validation."""

    def test_basic_construction(self) -> None:
        """A channel with name and dtype constructs successfully."""
        ch = SequenceChannel(name="close", dtype="float32")
        assert ch.name == "close"
        assert ch.dtype == "float32"

    def test_defaults(self) -> None:
        """Default normalization is 'standard' and missing_policy is 'fail'."""
        ch = SequenceChannel(name="close", dtype="float32")
        assert ch.normalization == "standard"
        assert ch.missing_policy == "fail"

    def test_all_dtypes(self) -> None:
        """All allowed dtypes are accepted."""
        for dtype in ("float32", "float64", "int32"):
            ch = SequenceChannel(name="ch", dtype=dtype)
            assert ch.dtype == dtype

    def test_all_normalizations(self) -> None:
        """All allowed normalization strategies are accepted."""
        for norm in ("standard", "robust", "minmax", "none"):
            ch = SequenceChannel(name="ch", dtype="float32", normalization=norm)
            assert ch.normalization == norm

    def test_all_missing_policies(self) -> None:
        """All allowed missing policies are accepted."""
        for pol in ("fail", "mean_fill", "zero_fill"):
            ch = SequenceChannel(name="ch", dtype="float32", missing_policy=pol)
            assert ch.missing_policy == pol

    def test_invalid_dtype_rejected(self) -> None:
        """An invalid dtype is rejected."""
        with pytest.raises(ValueError, match="dtype"):
            SequenceChannel(name="ch", dtype="float16")

    def test_invalid_normalization_rejected(self) -> None:
        """An invalid normalization is rejected."""
        with pytest.raises(ValueError, match="normalization"):
            SequenceChannel(name="ch", dtype="float32", normalization="weird")

    def test_invalid_missing_policy_rejected(self) -> None:
        """An invalid missing policy is rejected."""
        with pytest.raises(ValueError, match="missing_policy"):
            SequenceChannel(name="ch", dtype="float32", missing_policy="bfill")

    def test_empty_name_rejected(self) -> None:
        """An empty channel name is rejected."""
        with pytest.raises(ValueError, match="name"):
            SequenceChannel(name="", dtype="float32")

    def test_frozen(self) -> None:
        """A channel is frozen (immutable)."""
        ch = _make_channel()
        with pytest.raises(Exception):
            ch.name = "other"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        """Extra fields are forbidden."""
        with pytest.raises(Exception):
            SequenceChannel(name="ch", dtype="float32", extra="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SequenceDatasetManifest tests
# ---------------------------------------------------------------------------


class TestSequenceDatasetManifest:
    """Tests for SequenceDatasetManifest construction and validation."""

    def test_basic_construction(self) -> None:
        """A valid manifest constructs successfully."""
        m = _make_manifest()
        assert m.dataset_id == "seq_001"
        assert len(m.symbols) == 2
        assert len(m.channels) == 2
        assert m.window_length == 60

    def test_frozen(self) -> None:
        """A manifest is frozen (immutable)."""
        m = _make_manifest()
        with pytest.raises(Exception):
            m.dataset_id = "other"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        """Extra fields are forbidden."""
        kwargs = _make_manifest_kwargs(extra="bad")
        with pytest.raises(Exception):
            SequenceDatasetManifest(**kwargs)  # type: ignore[arg-type]

    def test_empty_symbols_rejected(self) -> None:
        """An empty symbols list is rejected."""
        with pytest.raises(ValueError, match="symbols"):
            _make_manifest(symbols=[])

    def test_duplicate_symbols_rejected(self) -> None:
        """Duplicate symbols are rejected (fail-closed)."""
        with pytest.raises(ValueError, match="duplicate"):
            _make_manifest(symbols=["AAPL", "AAPL"])

    def test_empty_channels_rejected(self) -> None:
        """An empty channels list is rejected."""
        with pytest.raises(ValueError, match="channels"):
            _make_manifest(channels=[])

    def test_duplicate_channels_rejected(self) -> None:
        """Duplicate channel names are rejected (fail-closed)."""
        with pytest.raises(ValueError, match="duplicate"):
            _make_manifest(channels=[_make_channel("close"), _make_channel("close")])

    def test_window_length_must_be_positive(self) -> None:
        """window_length must be >= 1."""
        with pytest.raises(ValueError, match="window_length"):
            _make_manifest(window_length=0)

    def test_stride_must_be_positive(self) -> None:
        """stride must be >= 1."""
        with pytest.raises(ValueError, match="stride"):
            _make_manifest(stride=0)

    def test_empty_horizons_rejected(self) -> None:
        """An empty horizons list is rejected."""
        with pytest.raises(ValueError, match="horizons"):
            _make_manifest(horizons=[])

    def test_horizon_must_be_positive(self) -> None:
        """Each horizon must be >= 1."""
        with pytest.raises(ValueError, match="horizon"):
            _make_manifest(horizons=[1, 0, 5])

    def test_window_end_must_be_after_start(self) -> None:
        """window_end must be > window_start."""
        with pytest.raises(ValueError, match="window_end"):
            _make_manifest(
                window_start="2024-06-01T00:00:00Z",
                window_end="2024-01-01T00:00:00Z",
            )

    def test_label_must_be_after_window_end(self) -> None:
        """label_timestamp must be > window_end (no future leakage)."""
        with pytest.raises(ValueError, match="label_timestamp"):
            _make_manifest(
                label_timestamp="2024-05-01T00:00:00Z",  # before window_end
            )

    def test_label_equal_to_window_end_rejected(self) -> None:
        """label_timestamp == window_end is rejected (must be strictly >)."""
        with pytest.raises(ValueError, match="label_timestamp"):
            _make_manifest(
                window_end="2024-06-01T00:00:00Z",
                label_timestamp="2024-06-01T00:00:00Z",
            )

    def test_availability_cutoff_must_be_at_least_label(self) -> None:
        """availability_cutoff must be >= label_timestamp."""
        with pytest.raises(ValueError, match="availability_cutoff"):
            _make_manifest(
                label_timestamp="2024-06-02T00:00:00Z",
                availability_cutoff="2024-06-01T00:00:00Z",
            )

    def test_availability_cutoff_equal_to_label_ok(self) -> None:
        """availability_cutoff == label_timestamp is allowed (>=)."""
        m = _make_manifest(
            label_timestamp="2024-06-02T00:00:00Z",
            availability_cutoff="2024-06-02T00:00:00Z",
        )
        assert m.availability_cutoff == m.label_timestamp

    def test_invalid_data_hash_rejected(self) -> None:
        """A non-64-char data_hash is rejected."""
        with pytest.raises(ValueError, match="data_hash"):
            _make_manifest(data_hash="abc123")

    def test_empty_data_uri_rejected(self) -> None:
        """An empty data_uri is rejected."""
        with pytest.raises(ValueError, match="data_uri"):
            _make_manifest(data_uri="")

    def test_fold_uri_without_hash_rejected(self) -> None:
        """fold_assignment_uri without hash is rejected (inconsistent)."""
        with pytest.raises(ValueError, match="fold_assignment"):
            _make_manifest(
                fold_assignment_uri="s3://bucket/folds.json",
                fold_assignment_hash=None,
            )

    def test_fold_hash_without_uri_rejected(self) -> None:
        """fold_assignment_hash without uri is rejected (inconsistent)."""
        with pytest.raises(ValueError, match="fold_assignment"):
            _make_manifest(
                fold_assignment_uri=None,
                fold_assignment_hash="b" * 64,
            )

    def test_fold_uri_and_hash_both_set_ok(self) -> None:
        """Both fold fields set together is valid."""
        m = _make_manifest(
            fold_assignment_uri="s3://bucket/folds.json",
            fold_assignment_hash="b" * 64,
        )
        assert m.fold_assignment_uri is not None
        assert m.fold_assignment_hash is not None

    def test_invalid_temporal_rejected(self) -> None:
        """An unparseable temporal string is rejected."""
        with pytest.raises(ValueError, match="window_start"):
            _make_manifest(window_start="not-a-date")

    def test_empty_dataset_id_rejected(self) -> None:
        """An empty dataset_id is rejected."""
        with pytest.raises(ValueError, match="dataset_id"):
            _make_manifest(dataset_id="")


# ---------------------------------------------------------------------------
# WindowSpec tests
# ---------------------------------------------------------------------------


class TestWindowSpec:
    """Tests for WindowSpec construction and validation."""

    def test_basic_construction(self) -> None:
        """A valid WindowSpec constructs successfully."""
        w = _make_window()
        assert w.symbol == "AAPL"
        assert w.horizon == 1

    def test_frozen(self) -> None:
        """A WindowSpec is frozen."""
        w = _make_window()
        with pytest.raises(Exception):
            w.symbol = "MSFT"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        """Extra fields are forbidden."""
        kwargs = _make_window_kwargs(extra="bad")
        with pytest.raises(Exception):
            WindowSpec(**kwargs)  # type: ignore[arg-type]

    def test_end_must_be_after_start(self) -> None:
        """end must be > start."""
        with pytest.raises(ValueError, match="end"):
            _make_window(
                start="2024-01-02T00:00:00Z",
                end="2024-01-01T00:00:00Z",
            )

    def test_label_must_be_after_end(self) -> None:
        """label_timestamp must be > end (no future leakage)."""
        with pytest.raises(ValueError, match="label_timestamp"):
            _make_window(
                end="2024-01-03T00:00:00Z",
                label_timestamp="2024-01-02T00:00:00Z",
            )

    def test_label_equal_to_end_rejected(self) -> None:
        """label_timestamp == end is rejected (must be strictly >)."""
        with pytest.raises(ValueError, match="label_timestamp"):
            _make_window(
                end="2024-01-02T00:00:00Z",
                label_timestamp="2024-01-02T00:00:00Z",
            )

    def test_horizon_must_be_positive(self) -> None:
        """horizon must be >= 1."""
        with pytest.raises(ValueError, match="horizon"):
            _make_window(horizon=0)

    def test_negative_fold_id_rejected(self) -> None:
        """A negative fold_id is rejected."""
        with pytest.raises(ValueError, match="fold_id"):
            _make_window(fold_id=-1)

    def test_fold_id_none_ok(self) -> None:
        """fold_id=None is allowed (unassigned)."""
        w = _make_window(fold_id=None)
        assert w.fold_id is None

    def test_fold_id_zero_ok(self) -> None:
        """fold_id=0 is allowed."""
        w = _make_window(fold_id=0)
        assert w.fold_id == 0

    def test_empty_window_id_rejected(self) -> None:
        """An empty window_id is rejected."""
        with pytest.raises(ValueError, match="window_id"):
            _make_window(window_id="")

    def test_empty_symbol_rejected(self) -> None:
        """An empty symbol is rejected."""
        with pytest.raises(ValueError, match="symbol"):
            _make_window(symbol="")


# ---------------------------------------------------------------------------
# validate_no_future_leakage tests
# ---------------------------------------------------------------------------


class TestValidateNoFutureLeakage:
    """Tests for validate_no_future_leakage."""

    def test_valid_window(self) -> None:
        """A window with label > end passes validation."""
        w = _make_window(
            end="2024-01-02T00:00:00Z",
            label_timestamp="2024-01-03T00:00:00Z",
        )
        assert validate_no_future_leakage(w) is True

    def test_label_equal_to_end_rejected(self) -> None:
        """label == end is future leakage (must be strictly >)."""
        # WindowSpec rejects this at construction, so build manually via
        # model_construct to test the validator function directly.
        w = WindowSpec.model_construct(
            window_id="test",
            symbol="AAPL",
            start="2024-01-01T00:00:00Z",
            end="2024-01-02T00:00:00Z",
            label_timestamp="2024-01-02T00:00:00Z",
            horizon=1,
            fold_id=None,
        )
        with pytest.raises(ValueError, match="future leakage"):
            validate_no_future_leakage(w)

    def test_label_before_end_rejected(self) -> None:
        """label < end is future leakage."""
        w = WindowSpec.model_construct(
            window_id="test",
            symbol="AAPL",
            start="2024-01-01T00:00:00Z",
            end="2024-01-03T00:00:00Z",
            label_timestamp="2024-01-02T00:00:00Z",
            horizon=1,
            fold_id=None,
        )
        with pytest.raises(ValueError, match="future leakage"):
            validate_no_future_leakage(w)

    def test_far_future_label_ok(self) -> None:
        """A label far in the future is valid."""
        w = _make_window(
            end="2024-01-02T00:00:00Z",
            label_timestamp="2025-01-01T00:00:00Z",
        )
        assert validate_no_future_leakage(w) is True


# ---------------------------------------------------------------------------
# validate_fold_assignment tests
# ---------------------------------------------------------------------------


class TestValidateFoldAssignment:
    """Tests for validate_fold_assignment."""

    def test_valid_no_folds(self) -> None:
        """Windows with no fold_ids pass when fold_spec is given."""
        windows = [_make_window(), _make_window(window_id="w2")]
        fs = _make_fold_spec(2)
        assert validate_fold_assignment(windows, fs) is True

    def test_valid_with_folds(self) -> None:
        """Windows with valid fold_ids pass."""
        windows = [
            _make_window(
                window_id="w0",
                start="2024-01-10",
                end="2024-01-11",
                label_timestamp="2024-01-12",
                fold_id=0,
            ),
            _make_window(
                window_id="w1",
                start="2024-04-10",
                end="2024-04-11",
                label_timestamp="2024-04-12",
                fold_id=1,
            ),
        ]
        fs = _make_fold_spec(2)
        assert validate_fold_assignment(windows, fs) is True

    def test_invalid_fold_id_rejected(self) -> None:
        """A fold_id not in the fold_spec is rejected."""
        windows = [
            _make_window(
                window_id="w0",
                start="2024-01-10",
                end="2024-01-11",
                label_timestamp="2024-01-12",
                fold_id=99,
            ),
        ]
        fs = _make_fold_spec(2)
        with pytest.raises(ValueError, match="invalid fold_ids"):
            validate_fold_assignment(windows, fs)

    def test_partial_assignment_rejected(self) -> None:
        """Some windows with fold_ids and some without is rejected."""
        windows = [
            _make_window(
                window_id="w0",
                start="2024-01-10",
                end="2024-01-11",
                label_timestamp="2024-01-12",
                fold_id=0,
            ),
            _make_window(window_id="w1"),
        ]
        fs = _make_fold_spec(2)
        with pytest.raises(ValueError, match="partial"):
            validate_fold_assignment(windows, fs)

    def test_empty_windows_rejected(self) -> None:
        """An empty window list is rejected."""
        fs = _make_fold_spec(2)
        with pytest.raises(ValueError, match="non-empty"):
            validate_fold_assignment([], fs)

    def test_fold_with_no_windows_rejected(self) -> None:
        """A fold with zero windows assigned is rejected."""
        windows = [
            _make_window(
                window_id="w0",
                start="2024-01-10",
                end="2024-01-11",
                label_timestamp="2024-01-12",
                fold_id=0,
            ),
        ]
        fs = _make_fold_spec(2)
        with pytest.raises(ValueError, match="no windows"):
            validate_fold_assignment(windows, fs)


# ---------------------------------------------------------------------------
# compute_sequence_data_hash tests
# ---------------------------------------------------------------------------


class TestComputeSequenceDataHash:
    """Tests for compute_sequence_data_hash."""

    def test_deterministic_same_array(self) -> None:
        """The same array produces the same hash."""
        import numpy as np

        arr = np.array([1.0, 2.0, 3.0], dtype="float32")
        h1 = compute_sequence_data_hash(arr)
        h2 = compute_sequence_data_hash(arr.copy())
        assert h1 == h2

    def test_different_values_different_hash(self) -> None:
        """Different values produce different hashes."""
        import numpy as np

        arr1 = np.array([1.0, 2.0, 3.0], dtype="float32")
        arr2 = np.array([1.0, 2.0, 4.0], dtype="float32")
        assert compute_sequence_data_hash(arr1) != compute_sequence_data_hash(arr2)

    def test_different_dtype_different_hash(self) -> None:
        """Different dtypes produce different hashes."""
        import numpy as np

        arr32 = np.array([1.0, 2.0, 3.0], dtype="float32")
        arr64 = np.array([1.0, 2.0, 3.0], dtype="float64")
        assert compute_sequence_data_hash(arr32) != compute_sequence_data_hash(arr64)

    def test_different_shape_different_hash(self) -> None:
        """Different shapes produce different hashes."""
        import numpy as np

        arr1 = np.array([1.0, 2.0, 3.0], dtype="float32")
        arr2 = np.array([[1.0, 2.0, 3.0]], dtype="float32")
        assert compute_sequence_data_hash(arr1) != compute_sequence_data_hash(arr2)

    def test_hash_is_64_char_hex(self) -> None:
        """The hash is a 64-character lowercase hex string."""
        import numpy as np

        arr = np.array([1.0], dtype="float32")
        h = compute_sequence_data_hash(arr)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_bytes_input(self) -> None:
        """A bytes object is accepted."""
        h = compute_sequence_data_hash(b"hello")
        assert len(h) == 64

    def test_none_rejected(self) -> None:
        """None is rejected."""
        with pytest.raises(ValueError, match="None"):
            compute_sequence_data_hash(None)

    def test_invalid_type_rejected(self) -> None:
        """An object without tobytes() is rejected."""
        with pytest.raises(ValueError, match="tobytes"):
            compute_sequence_data_hash(42)


# ---------------------------------------------------------------------------
# create_windows tests
# ---------------------------------------------------------------------------


class TestCreateWindows:
    """Tests for create_windows."""

    def test_basic_window_generation(self) -> None:
        """Windows are generated for each symbol and horizon."""
        manifest = _make_manifest(window_length=3, stride=2, horizons=[1])
        timestamps = [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-06",
        ]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        # i=0: start=01-01, end=01-03, label=01-04
        # i=2: start=01-03, end=01-05, label=01-06
        # i=4: start=01-05, end=01-07 -> out of range, skip
        assert len(windows) == 2
        assert windows[0].start == "2024-01-01"
        assert windows[0].end == "2024-01-03"
        assert windows[0].label_timestamp == "2024-01-04"
        assert windows[0].symbol == "AAPL"

    def test_multiple_symbols(self) -> None:
        """Windows are generated for each symbol."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1])
        timestamps = [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        windows = create_windows(manifest, timestamps, ["AAPL", "MSFT"])
        # i=0: start=01-01, end=01-02, label=01-03 ✓
        # i=1: start=01-02, end=01-03, label=01-04 ✓
        # i=2: start=01-03, end=01-04, label_idx=4 >= 4, skip
        # 2 positions * 2 symbols = 4 windows
        assert len(windows) == 4
        symbols = {w.symbol for w in windows}
        assert symbols == {"AAPL", "MSFT"}

    def test_multiple_horizons(self) -> None:
        """Windows are generated for each horizon."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1, 2])
        timestamps = [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        # i=0: end=01-02, h=1 -> label=01-03; h=2 -> label=01-04
        # i=1: end=01-03, h=1 -> label=01-04; h=2 -> label=01-05 (skip)
        assert len(windows) == 3
        horizons = {w.horizon for w in windows}
        assert horizons == {1, 2}

    def test_stride_applied(self) -> None:
        """Stride controls the step between window starts."""
        manifest = _make_manifest(window_length=2, stride=3, horizons=[1])
        timestamps = [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-06",
        ]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        # i=0: start=01-01, end=01-02, label=01-03
        # i=3: start=01-04, end=01-05, label=01-06
        assert len(windows) == 2
        assert windows[0].start == "2024-01-01"
        assert windows[1].start == "2024-01-04"

    def test_window_id_deterministic(self) -> None:
        """window_id is deterministic: symbol_start_end_horizon."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1])
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        w = windows[0]
        assert w.window_id == f"AAPL_{w.start}_{w.end}_{w.horizon}"

    def test_fold_assignment(self) -> None:
        """fold_ids are assigned when fold_spec is provided."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1])
        # Jan timestamps fall in fold 0 train; Apr timestamps in fold 1 train.
        timestamps = [
            "2024-01-10",
            "2024-01-11",
            "2024-01-12",
            "2024-04-10",
            "2024-04-11",
            "2024-04-12",
        ]
        fs = _make_fold_spec(2)
        windows = create_windows(manifest, timestamps, ["AAPL"], fold_spec=fs)
        # First windows (Jan) should be fold 0, later (Apr) fold 1.
        fold_ids = [w.fold_id for w in windows]
        assert 0 in fold_ids
        assert 1 in fold_ids

    def test_no_fold_spec_leaves_none(self) -> None:
        """Without fold_spec, fold_id is None."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1])
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        assert all(w.fold_id is None for w in windows)

    def test_short_timestamps_rejected(self) -> None:
        """Timestamps shorter than window_length are rejected."""
        manifest = _make_manifest(window_length=5, stride=1, horizons=[1])
        timestamps = ["2024-01-01", "2024-01-02"]
        with pytest.raises(ValueError, match="timestamps"):
            create_windows(manifest, timestamps, ["AAPL"])

    def test_empty_timestamps_rejected(self) -> None:
        """Empty timestamps are rejected."""
        manifest = _make_manifest()
        with pytest.raises(ValueError, match="timestamps"):
            create_windows(manifest, [], ["AAPL"])

    def test_empty_symbols_rejected(self) -> None:
        """Empty symbols are rejected."""
        manifest = _make_manifest()
        timestamps = ["2024-01-01", "2024-01-02"]
        with pytest.raises(ValueError, match="symbols"):
            create_windows(manifest, timestamps, [])

    def test_symbol_not_in_manifest_rejected(self) -> None:
        """A symbol not in the manifest is rejected."""
        manifest = _make_manifest(symbols=["AAPL"])
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        with pytest.raises(ValueError, match="not in manifest"):
            create_windows(manifest, timestamps, ["GOOG"])

    def test_windows_pass_leakage_validation(self) -> None:
        """Generated windows pass validate_no_future_leakage."""
        manifest = _make_manifest(window_length=2, stride=1, horizons=[1])
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]
        windows = create_windows(manifest, timestamps, ["AAPL"])
        for w in windows:
            assert validate_no_future_leakage(w) is True


# ---------------------------------------------------------------------------
# SequenceManifestBuilder tests
# ---------------------------------------------------------------------------


class TestSequenceManifestBuilder:
    """Tests for the SequenceManifestBuilder fluent API."""

    def test_full_build(self) -> None:
        """A fully-specified builder produces a valid manifest."""
        import numpy as np

        arr = np.zeros((10, 5), dtype="float32")
        data_hash = compute_sequence_data_hash(arr)
        manifest = (
            SequenceManifestBuilder("seq_builder_001")
            .with_symbols(["AAPL", "MSFT"])
            .with_channels([_make_channel("close"), _make_channel("volume")])
            .with_window(length=60, stride=5)
            .with_horizons([1, 5, 21])
            .with_time_range(
                start="2024-01-01T00:00:00Z",
                end="2024-06-01T00:00:00Z",
                label_ts="2024-06-02T00:00:00Z",
                avail_cutoff="2024-06-02T00:00:00Z",
            )
            .with_data(uri="s3://bucket/seq.npy", data_hash=data_hash)
            .with_created_at("2024-01-01T00:00:00Z")
            .build()
        )
        assert manifest.dataset_id == "seq_builder_001"
        assert manifest.window_length == 60
        assert manifest.data_hash == data_hash

    def test_builder_chaining_returns_self(self) -> None:
        """Each with_* method returns the builder for chaining."""
        b = SequenceManifestBuilder("seq_002")
        assert b.with_symbols(["AAPL"]) is b
        assert b.with_channels([_make_channel()]) is b
        assert b.with_window(length=10, stride=1) is b
        assert b.with_horizons([1]) is b
        assert b.with_time_range("2024-01-01", "2024-06-01", "2024-06-02", "2024-06-02") is b
        assert b.with_data("uri", "a" * 64) is b

    def test_builder_default_created_at(self) -> None:
        """build() defaults created_at to now if not set."""
        manifest = (
            SequenceManifestBuilder("seq_003")
            .with_symbols(["AAPL"])
            .with_channels([_make_channel()])
            .with_window(length=10, stride=1)
            .with_horizons([1])
            .with_time_range("2024-01-01", "2024-06-01", "2024-06-02", "2024-06-02")
            .with_data("uri", "a" * 64)
            .build()
        )
        # created_at should be a valid ISO string.
        _parse_temporal_check(manifest.created_at)

    def test_builder_with_folds(self) -> None:
        """with_folds sets both uri and hash."""
        manifest = (
            SequenceManifestBuilder("seq_004")
            .with_symbols(["AAPL"])
            .with_channels([_make_channel()])
            .with_window(length=10, stride=1)
            .with_horizons([1])
            .with_time_range("2024-01-01", "2024-06-01", "2024-06-02", "2024-06-02")
            .with_data("uri", "a" * 64)
            .with_folds("s3://bucket/folds.json", "b" * 64)
            .with_created_at("2024-01-01T00:00:00Z")
            .build()
        )
        assert manifest.fold_assignment_uri == "s3://bucket/folds.json"
        assert manifest.fold_assignment_hash == "b" * 64

    def test_builder_validation_fail_closed(self) -> None:
        """build() fails closed on invalid data (future leakage)."""
        b = (
            SequenceManifestBuilder("seq_005")
            .with_symbols(["AAPL"])
            .with_channels([_make_channel()])
            .with_window(length=10, stride=1)
            .with_horizons([1])
            .with_time_range(
                start="2024-01-01",
                end="2024-06-01",
                label_ts="2024-05-01",  # before end -> leakage
                avail_cutoff="2024-06-02",
            )
            .with_data("uri", "a" * 64)
            .with_created_at("2024-01-01T00:00:00Z")
        )
        with pytest.raises(ValueError, match="label_timestamp"):
            b.build()


def _parse_temporal_check(value: str) -> None:
    """Assert that a string is a parseable ISO datetime."""
    from quant_foundry.dataset_manifest import _parse_temporal

    _parse_temporal(value)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests: single symbol, single channel, single horizon."""

    def test_single_symbol(self) -> None:
        """A manifest with a single symbol is valid."""
        m = _make_manifest(symbols=["AAPL"])
        assert m.symbols == ["AAPL"]

    def test_single_channel(self) -> None:
        """A manifest with a single channel is valid."""
        m = _make_manifest(channels=[_make_channel()])
        assert len(m.channels) == 1

    def test_single_horizon(self) -> None:
        """A manifest with a single horizon is valid."""
        m = _make_manifest(horizons=[1])
        assert m.horizons == [1]

    def test_window_length_one(self) -> None:
        """window_length=1 is valid (minimum)."""
        m = _make_manifest(window_length=1)
        assert m.window_length == 1

    def test_stride_one(self) -> None:
        """stride=1 is valid (minimum, no skipping)."""
        m = _make_manifest(stride=1)
        assert m.stride == 1

    def test_large_horizon(self) -> None:
        """A large horizon is valid."""
        m = _make_manifest(horizons=[252])
        assert m.horizons == [252]

    def test_date_only_temporals(self) -> None:
        """Date-only ISO strings (no time component) are accepted."""
        m = _make_manifest(
            window_start="2024-01-01",
            window_end="2024-06-01",
            label_timestamp="2024-06-02",
            availability_cutoff="2024-06-02",
            created_at="2024-01-01",
        )
        assert m.window_start == "2024-01-01"
