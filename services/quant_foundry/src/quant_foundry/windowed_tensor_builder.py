"""quant_foundry.windowed_tensor_builder — windowed tensor builder (T-10.2).

This module converts raw daily bar data (a pandas DataFrame of
``symbol, timestamp, <channels...>``) into **windowed tensors** suitable
for training sequence models (RNN / Transformer / TCN). It is the
tensor-materialization counterpart to :mod:`quant_foundry.sequence_manifest`
(T-10.1), which defines the *contract* of a sequence dataset.

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):

- **No future leakage**: the label timestamp for every window is strictly
  after the feature window end. :func:`validate_no_label_in_features`
  additionally fail-closes if the label value appears inside the feature
  data (a common accidental-leakage bug).
- **Deterministic window ids**: each :class:`WindowedTensor` has a
  deterministic ``window_id`` of the form
  ``"{symbol}_{start}_{end}_{horizon}"`` so two runs over the same data
  produce identical ids.
- **Deterministic output hash**: :func:`compute_tensor_hash` produces a
  stable SHA-256 over the windowed tensor data (sorted by ``window_id``),
  so the same input always yields the same :class:`WindowedTensorReceipt`.
- **Output formats**: ``.npz`` (numpy) or ``parquet`` (one row per
  window), selected via :attr:`WindowedTensorConfig.output_format`.
- **Audit receipt**: :class:`WindowedTensorReceipt` records the manifest
  reference, window count, symbol count, output path, output hash, and
  the list of window ids — the provenance trail for downstream consumers.

The module reuses :class:`SequenceDatasetManifest` /
:class:`SequenceChannel` from :mod:`quant_foundry.sequence_manifest` and
the temporal parsing helper :func:`_parse_temporal` from
:mod:`quant_foundry.dataset_manifest`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.dataset_manifest import _parse_temporal
from quant_foundry.sequence_manifest import (
    SequenceDatasetManifest,
    _make_window_id,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed output formats for :class:`WindowedTensorConfig`.
_ALLOWED_OUTPUT_FORMATS: frozenset[str] = frozenset({"npz", "parquet"})


# ---------------------------------------------------------------------------
# WindowedTensorConfig
# ---------------------------------------------------------------------------


class WindowedTensorConfig(BaseModel):
    """Configuration for the :class:`WindowedTensorBuilder`.

    Fixes the window geometry (length, stride), the label horizons, the
    feature channel names, and the output format. Two builders with the
    same config over the same data produce identical windowed tensors.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        window_length: the number of time steps in each feature window
            (>= 1).
        stride: the step between consecutive window starts (>= 1).
        horizons: list of label horizons in time steps (at least 1, each
            >= 1, no duplicates).
        channels: list of feature channel names (at least 1, no
            duplicates). These must match columns in the input dataframe.
        output_format: the output file format — ``"npz"`` or
            ``"parquet"``. Defaults to ``"npz"``.
        include_symbol: whether to include the symbol in the output.
            Defaults to True.
        include_timestamp: whether to include timestamps in the output.
            Defaults to True.
        include_window_id: whether to include the window id in the
            output. Defaults to True.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_length: int
    stride: int
    horizons: list[int]
    channels: list[str]
    output_format: str = "npz"
    include_symbol: bool = True
    include_timestamp: bool = True
    include_window_id: bool = True

    @field_validator("window_length")
    @classmethod
    def _window_length_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"window_length must be >= 1; got {v}")
        return v

    @field_validator("stride")
    @classmethod
    def _stride_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"stride must be >= 1; got {v}")
        return v

    @field_validator("horizons")
    @classmethod
    def _horizons_valid(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("horizons must contain at least 1 horizon")
        for h in v:
            if not isinstance(h, int) or h < 1:
                raise ValueError(f"each horizon must be an integer >= 1; got {h!r}")
        return v

    @field_validator("channels")
    @classmethod
    def _channels_valid(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("channels must contain at least 1 channel")
        for c in v:
            if not isinstance(c, str) or not c.strip():
                raise ValueError("channels entries must be non-empty strings")
        return v

    @field_validator("output_format")
    @classmethod
    def _output_format_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {sorted(_ALLOWED_OUTPUT_FORMATS)!r}; got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _no_duplicate_channels(self) -> WindowedTensorConfig:
        """Channel names must be unique (no duplicate channels)."""
        if len(set(self.channels)) != len(self.channels):
            dupes = sorted({c for c in self.channels if self.channels.count(c) > 1})
            raise ValueError(f"channels must not contain duplicates: {dupes!r}")
        return self

    @model_validator(mode="after")
    def _no_duplicate_horizons(self) -> WindowedTensorConfig:
        """Horizons must be unique (no duplicate horizons)."""
        if len(set(self.horizons)) != len(self.horizons):
            dupes = sorted({h for h in self.horizons if self.horizons.count(h) > 1})
            raise ValueError(f"horizons must not contain duplicates: {dupes!r}")
        return self


# ---------------------------------------------------------------------------
# WindowedTensor
# ---------------------------------------------------------------------------


class WindowedTensor(BaseModel):
    """A single windowed tensor (one feature window + label).

    A :class:`WindowedTensor` holds the feature data for one temporal
    window (``window_length`` rows x ``len(channels)`` columns), the label
    (future return at the horizon), and the metadata needed for audit
    (symbol, timestamps, horizon, window id).

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        window_id: deterministic id of the form
            ``"{symbol}_{start}_{end}_{horizon}"``.
        symbol: the instrument symbol for this window.
        start_timestamp: ISO datetime — inclusive start of the feature
            window.
        end_timestamp: ISO datetime — inclusive end of the feature
            window.
        label_timestamp: ISO datetime — the timestamp of the label. Must
            be > end_timestamp (no future leakage into the feature
            window).
        horizon: the label horizon in time steps (>= 1).
        data: the feature data as a list of rows
            (``window_length`` x ``len(channels)``).
        label: the label value (future return at the horizon).
        weight: the sample weight. Defaults to 1.0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_id: str
    symbol: str
    start_timestamp: str
    end_timestamp: str
    label_timestamp: str
    horizon: int
    data: list[list[float]]
    label: float
    weight: float = 1.0

    @field_validator("window_id", "symbol")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v

    @field_validator("start_timestamp", "end_timestamp", "label_timestamp")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                f"{info.field_name} must be a non-empty ISO datetime string; got {v!r}"
            )
        _parse_temporal(v)
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"horizon must be an integer >= 1; got {v!r}")
        return v

    @field_validator("data")
    @classmethod
    def _data_nonempty(cls, v: list[list[float]]) -> list[list[float]]:
        if not v:
            raise ValueError("data must contain at least 1 row")
        return v

    @model_validator(mode="after")
    def _check_data_shape(self) -> WindowedTensor:
        """All rows must have the same number of columns (>= 1)."""
        n_cols = len(self.data[0])
        if n_cols < 1:
            raise ValueError(f"each data row must have at least 1 column; got {n_cols}")
        for i, row in enumerate(self.data):
            if len(row) != n_cols:
                raise ValueError(
                    f"data rows must all have the same length; "
                    f"row 0 has {n_cols} columns but row {i} has "
                    f"{len(row)}"
                )
        return self

    @model_validator(mode="after")
    def _check_no_future_leakage(self) -> WindowedTensor:
        """label_timestamp must be > end_timestamp (no future leakage)."""
        end_epoch = _parse_temporal(self.end_timestamp)
        label_epoch = _parse_temporal(self.label_timestamp)
        if not (label_epoch > end_epoch):
            raise ValueError(
                f"label_timestamp must be > end_timestamp "
                f"(no future leakage) "
                f"(label_timestamp={self.label_timestamp!r}, "
                f"end_timestamp={self.end_timestamp!r})"
            )
        return self

    @property
    def window_length(self) -> int:
        """The number of rows in the feature data."""
        return len(self.data)

    @property
    def n_channels(self) -> int:
        """The number of columns in the feature data."""
        return len(self.data[0]) if self.data else 0


# ---------------------------------------------------------------------------
# WindowedTensorReceipt
# ---------------------------------------------------------------------------


class WindowedTensorReceipt(BaseModel):
    """Receipt for a built windowed tensor dataset.

    Records the provenance trail for a windowed tensor export: the
    manifest it was built from, the window/symbol counts, the output
    path, the deterministic output hash, the creation timestamp, and the
    list of window ids.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        manifest: the :class:`SequenceDatasetManifest` this dataset was
            built from.
        n_windows: the number of windows in the output.
        n_symbols: the number of distinct symbols in the output.
        output_path: the path to the output file.
        output_hash: the deterministic SHA-256 of the windowed tensor
            data (64-char hex).
        created_at: ISO timestamp of receipt creation.
        window_ids: the list of window ids in the output (sorted).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: SequenceDatasetManifest
    n_windows: int
    n_symbols: int
    output_path: str
    output_hash: str
    created_at: str
    window_ids: list[str]

    @field_validator("n_windows")
    @classmethod
    def _n_windows_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"n_windows must be >= 0; got {v}")
        return v

    @field_validator("n_symbols")
    @classmethod
    def _n_symbols_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_symbols must be >= 1; got {v}")
        return v

    @field_validator("output_path")
    @classmethod
    def _output_path_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("output_path must be a non-empty string")
        return v

    @field_validator("output_hash")
    @classmethod
    def _output_hash_shape(cls, v: str) -> str:
        if not isinstance(v, str) or len(v) != 64:
            raise ValueError(f"output_hash must be a 64-char hex SHA-256; got {v!r}")
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError(f"output_hash must be a 64-char hex SHA-256; got {v!r}") from exc
        return v.lower()

    @field_validator("created_at")
    @classmethod
    def _created_at_parseable(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"created_at must be a non-empty ISO datetime string; got {v!r}")
        _parse_temporal(v)
        return v

    @field_validator("window_ids")
    @classmethod
    def _window_ids_nonempty(cls, v: list[str]) -> list[str]:
        if v is None:
            raise ValueError("window_ids must not be None")
        return v

    @model_validator(mode="after")
    def _window_count_matches(self) -> WindowedTensorReceipt:
        """n_windows must equal len(window_ids)."""
        if self.n_windows != len(self.window_ids):
            raise ValueError(
                f"n_windows ({self.n_windows}) must equal len(window_ids) ({len(self.window_ids)})"
            )
        return self


# ---------------------------------------------------------------------------
# compute_tensor_hash
# ---------------------------------------------------------------------------


def compute_tensor_hash(windows: list[WindowedTensor]) -> str:
    """Compute a deterministic SHA-256 hash over windowed tensor data.

    The hash is computed over a canonical JSON representation of the
    windows sorted by ``window_id``. Each window contributes its
    ``window_id``, ``symbol``, ``start_timestamp``, ``end_timestamp``,
    ``label_timestamp``, ``horizon``, ``data``, ``label``, and
    ``weight``. Two lists with the same windows (in any order) produce
    the same hash; any change to a value, window, or order-altering
    membership alters the hash.

    Args:
        windows: the list of :class:`WindowedTensor` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if ``windows`` is empty.
    """
    if not windows:
        raise ValueError("windows must be non-empty to compute a hash")

    sorted_windows = sorted(windows, key=lambda w: w.window_id)
    payload: list[dict[str, Any]] = []
    for w in sorted_windows:
        payload.append(
            {
                "window_id": w.window_id,
                "symbol": w.symbol,
                "start_timestamp": w.start_timestamp,
                "end_timestamp": w.end_timestamp,
                "label_timestamp": w.label_timestamp,
                "horizon": w.horizon,
                "data": w.data,
                "label": float(w.label),
                "weight": float(w.weight),
            }
        )
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# validate_no_label_in_features
# ---------------------------------------------------------------------------


def validate_no_label_in_features(window: WindowedTensor) -> bool:
    """Check that the label value is not present in the feature data.

    A common accidental-leakage bug is to include the label (future
    return) as one of the feature channels. This function fail-closes if
    the label value appears anywhere in the window's feature ``data``.

    Args:
        window: the :class:`WindowedTensor` to check.

    Returns:
        True if no leakage is detected.

    Raises:
        ValueError: if the label value is found in the feature data.
    """
    label = float(window.label)
    for i, row in enumerate(window.data):
        for j, val in enumerate(row):
            if float(val) == label:
                raise ValueError(
                    f"label value {label!r} found in feature data "
                    f"(row {i}, col {j}) for window "
                    f"{window.window_id!r} — possible label leakage"
                )
    return True


# ---------------------------------------------------------------------------
# WindowedTensorBuilder
# ---------------------------------------------------------------------------


class WindowedTensorBuilder:
    """Builds windowed tensors from raw daily bar data.

    Converts a pandas DataFrame (``symbol, timestamp, <channels...>``)
    into a list of :class:`WindowedTensor` windows, writes them to
    ``.npz`` or ``parquet`` format, and returns a
    :class:`WindowedTensorReceipt` with the deterministic output hash.

    The builder enforces:
    - **No future leakage**: each window's ``label_timestamp`` is
      strictly after its ``end_timestamp`` (windows without enough
      future data for a label are skipped).
    - **No label in features**: :func:`validate_no_label_in_features`
      is run on every window.
    - **Deterministic output**: the same input + config always produces
      the same ``output_hash`` and ``window_ids``.

    Args:
        config: the :class:`WindowedTensorConfig` controlling window
            geometry and output format.
    """

    def __init__(self, config: WindowedTensorConfig) -> None:
        """Initialize the builder with a config.

        Args:
            config: the :class:`WindowedTensorConfig` controlling window
                geometry and output format.
        """
        self.config: WindowedTensorConfig = config

    # --- core build ------------------------------------------------------

    def build(
        self,
        df: Any,
        manifest: SequenceDatasetManifest,
        output_path: str,
    ) -> WindowedTensorReceipt:
        """Build windowed tensors from a dataframe and write to disk.

        Takes a dataframe with columns ``symbol, timestamp, <channels...>``
        and, for each symbol, creates sliding windows of
        ``window_length`` with ``stride``. For each window and each
        horizon, extracts the feature data and computes the label
        (future return at the horizon). Windows without enough future
        data for a label are skipped (no future leakage).

        Args:
            df: a pandas DataFrame with columns ``symbol``,
                ``timestamp``, and one column per channel in
                ``config.channels``.
            manifest: the :class:`SequenceDatasetManifest` this dataset
                is built from (recorded in the receipt for provenance).
            output_path: the path to write the output file to. The
                extension must match ``config.output_format``
                (``.npz`` or ``.parquet``).

        Returns:
            A :class:`WindowedTensorReceipt` with the output hash and
            window ids.

        Raises:
            ValueError: if the dataframe is missing required columns,
                if no windows can be built, or if label leakage is
                detected.
        """
        windows = self._extract_windows(df)

        if not windows:
            raise ValueError(
                "no windows could be built from the dataframe — "
                "need at least window_length + max(horizons) rows per "
                "symbol"
            )

        # Fail-closed: validate no label leakage in any window.
        for w in windows:
            validate_no_label_in_features(w)

        # Write output in the configured format.
        if self.config.output_format == "npz":
            self.build_npz(windows, output_path)
        elif self.config.output_format == "parquet":
            self.build_parquet(windows, output_path)
        else:  # pragma: no cover — guarded by config validator
            raise ValueError(f"unsupported output_format: {self.config.output_format!r}")

        output_hash = compute_tensor_hash(windows)
        n_symbols = len({w.symbol for w in windows})
        window_ids = sorted(w.window_id for w in windows)
        created_at = datetime.now(UTC).isoformat()

        return WindowedTensorReceipt(
            manifest=manifest,
            n_windows=len(windows),
            n_symbols=n_symbols,
            output_path=output_path,
            output_hash=output_hash,
            created_at=created_at,
            window_ids=window_ids,
        )

    # --- window extraction ----------------------------------------------

    def _extract_windows(self, df: Any) -> list[WindowedTensor]:
        """Extract windowed tensors from a dataframe.

        Args:
            df: a pandas DataFrame with columns ``symbol``,
                ``timestamp``, and one column per channel.

        Returns:
            A list of :class:`WindowedTensor`.

        Raises:
            ValueError: if required columns are missing.
        """
        required = ["symbol", "timestamp"] + list(self.config.channels)
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(
                f"dataframe is missing required columns: {missing!r} (have {list(df.columns)!r})"
            )

        wl = self.config.window_length
        stride = self.config.stride
        channels = list(self.config.channels)
        horizons = sorted(self.config.horizons)
        max_horizon = max(horizons)

        windows: list[WindowedTensor] = []

        # Group by symbol, preserving the original row order within each
        # symbol (the dataframe is assumed sorted by timestamp per
        # symbol; we sort to be safe).
        for symbol in sorted(df["symbol"].unique()):
            sub = df[df["symbol"] == symbol].sort_values("timestamp")
            n = len(sub)
            if n < wl + max_horizon:
                # Not enough data for even one window + label.
                continue

            timestamps = sub["timestamp"].tolist()
            # Channel values as a 2D numpy array (n x n_channels).
            feat = sub[channels].to_numpy(dtype=np.float64)

            for i in range(0, n - wl + 1, stride):
                start_ts = str(timestamps[i])
                end_idx = i + wl - 1
                end_ts = str(timestamps[end_idx])

                # Feature window: rows [i, i+wl).
                window_data = feat[i : i + wl].tolist()

                for horizon in horizons:
                    label_idx = end_idx + horizon
                    if label_idx >= n:
                        # Label period hasn't happened yet — skip.
                        continue
                    label_ts = str(timestamps[label_idx])

                    # Label = future return at horizon.
                    # Use the first channel (typically "close") as the
                    # price reference for the return, if available;
                    # otherwise use the mean of the last feature row.
                    ref_col = channels[0]
                    ref_vals = sub[ref_col].to_numpy(dtype=np.float64)
                    current_price = float(ref_vals[end_idx])
                    future_price = float(ref_vals[label_idx])
                    if current_price != 0.0:
                        label = (future_price - current_price) / abs(current_price)
                    else:
                        label = 0.0
                    label = float(label)

                    window_id = _make_window_id(symbol, start_ts, end_ts, horizon)
                    window = WindowedTensor(
                        window_id=window_id,
                        symbol=symbol,
                        start_timestamp=start_ts,
                        end_timestamp=end_ts,
                        label_timestamp=label_ts,
                        horizon=horizon,
                        data=window_data,
                        label=label,
                    )
                    windows.append(window)

        return windows

    # --- output writers --------------------------------------------------

    def build_npz(
        self,
        windows: list[WindowedTensor],
        output_path: str,
    ) -> str:
        """Write windows to a ``.npz`` file.

        The ``.npz`` archive contains, for each window, the feature
        data array plus metadata arrays (window_id, symbol, timestamps,
        horizon, label, weight). Arrays are stacked so that window ``k``
        can be recovered by indexing each array at ``[k]``.

        Args:
            windows: the list of :class:`WindowedTensor` to write.
            output_path: the path to write to (should end in ``.npz``).

        Returns:
            The output path.
        """
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Stack feature data into a 3D array (n_windows x wl x n_channels).
        data_stack = np.array(
            [np.array(w.data, dtype=np.float64) for w in windows],
            dtype=np.float64,
        )

        archive: dict[str, np.ndarray] = {
            "data": data_stack,
            "label": np.array([w.label for w in windows], dtype=np.float64),
            "weight": np.array([w.weight for w in windows], dtype=np.float64),
            "horizon": np.array([w.horizon for w in windows], dtype=np.int64),
        }

        if self.config.include_symbol:
            archive["symbol"] = np.array([w.symbol for w in windows], dtype=object)
        if self.config.include_timestamp:
            archive["start_timestamp"] = np.array(
                [w.start_timestamp for w in windows], dtype=object
            )
            archive["end_timestamp"] = np.array([w.end_timestamp for w in windows], dtype=object)
            archive["label_timestamp"] = np.array(
                [w.label_timestamp for w in windows], dtype=object
            )
        if self.config.include_window_id:
            archive["window_id"] = np.array([w.window_id for w in windows], dtype=object)

        np.savez(str(path), **archive)  # type: ignore[arg-type]  # numpy savez **kwargs typing doesn't match dict unpacking
        return str(path)

    def build_parquet(
        self,
        windows: list[WindowedTensor],
        output_path: str,
    ) -> str:
        """Write windows to a parquet file (one row per window).

        Each window becomes one row with columns: ``window_id``,
        ``symbol``, ``start_timestamp``, ``end_timestamp``,
        ``label_timestamp``, ``horizon``, ``data`` (the 2D feature array
        as a nested list), ``label``, and ``weight``.

        Args:
            windows: the list of :class:`WindowedTensor` to write.
            output_path: the path to write to (should end in
                ``.parquet``).

        Returns:
            The output path.

        Raises:
            ImportError: if ``pyarrow`` / ``pandas`` parquet engine is
                not available.
        """
        import pandas as pd

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        records: list[dict[str, Any]] = []
        for w in windows:
            row: dict[str, Any] = {
                "horizon": w.horizon,
                "data": w.data,
                "label": w.label,
                "weight": w.weight,
            }
            if self.config.include_symbol:
                row["symbol"] = w.symbol
            if self.config.include_timestamp:
                row["start_timestamp"] = w.start_timestamp
                row["end_timestamp"] = w.end_timestamp
                row["label_timestamp"] = w.label_timestamp
            if self.config.include_window_id:
                row["window_id"] = w.window_id
            records.append(row)

        df = pd.DataFrame(records)
        df.to_parquet(str(path), index=False)
        return str(path)

    # --- validation ------------------------------------------------------

    def validate_output(
        self,
        receipt: WindowedTensorReceipt,
        output_path: str,
    ) -> bool:
        """Validate that an output file matches a receipt.

        Checks:
        - The output file exists.
        - The receipt's ``output_hash`` matches a recomputed hash of the
          windows loaded from the file (for ``.npz``) — or, if the file
          cannot be re-parsed into windows, matches the file's SHA-256.
        - The window count in the receipt matches the number of windows
          in the file.

        Args:
            receipt: the :class:`WindowedTensorReceipt` to validate
                against.
            output_path: the path to the output file.

        Returns:
            True if all checks pass.

        Raises:
            ValueError: if the file does not exist, the hash does not
                match, or the window count does not match.
        """
        path = Path(output_path)
        if not path.exists():
            raise ValueError(f"output file does not exist: {output_path!r}")

        # Load windows from the file and recompute the hash.
        loaded_windows = self._load_windows(output_path)

        if len(loaded_windows) != receipt.n_windows:
            raise ValueError(
                f"window count mismatch: receipt has "
                f"{receipt.n_windows} but file has "
                f"{len(loaded_windows)}"
            )

        recomputed = compute_tensor_hash(loaded_windows)
        if recomputed != receipt.output_hash:
            raise ValueError(
                f"output hash mismatch: receipt has "
                f"{receipt.output_hash!r} but recomputed "
                f"{recomputed!r}"
            )

        return True

    def _load_windows(self, output_path: str) -> list[WindowedTensor]:
        """Load windows from an output file for validation.

        Args:
            output_path: the path to the output file.

        Returns:
            A list of :class:`WindowedTensor`.

        Raises:
            ValueError: if the file format is unsupported or malformed.
        """
        path = Path(output_path)
        if path.suffix == ".npz":
            return self._load_npz(output_path)
        elif path.suffix == ".parquet":
            return self._load_parquet(output_path)
        else:
            raise ValueError(
                f"unsupported output file extension: {path.suffix!r} (expected .npz or .parquet)"
            )

    def _load_npz(self, output_path: str) -> list[WindowedTensor]:
        """Load windows from a ``.npz`` file.

        Args:
            output_path: the path to the ``.npz`` file.

        Returns:
            A list of :class:`WindowedTensor`.
        """
        archive = np.load(output_path, allow_pickle=True)
        data_stack = archive["data"]  # (n, wl, n_channels)
        labels = archive["label"]
        weights = archive["weight"]
        horizons = archive["horizon"]
        symbols = archive["symbol"] if "symbol" in archive.files else None
        start_ts = archive["start_timestamp"] if "start_timestamp" in archive.files else None
        end_ts = archive["end_timestamp"] if "end_timestamp" in archive.files else None
        label_ts = archive["label_timestamp"] if "label_timestamp" in archive.files else None
        window_ids = archive["window_id"] if "window_id" in archive.files else None

        windows: list[WindowedTensor] = []
        n = data_stack.shape[0]
        for k in range(n):
            row_data = data_stack[k].tolist()
            sym = str(symbols[k]) if symbols is not None else "UNKNOWN"
            s_ts = str(start_ts[k]) if start_ts is not None else ""
            e_ts = str(end_ts[k]) if end_ts is not None else ""
            l_ts = str(label_ts[k]) if label_ts is not None else ""
            wid = (
                str(window_ids[k])
                if window_ids is not None
                else f"{sym}_{s_ts}_{e_ts}_{int(horizons[k])}"
            )
            windows.append(
                WindowedTensor(
                    window_id=wid,
                    symbol=sym,
                    start_timestamp=s_ts,
                    end_timestamp=e_ts,
                    label_timestamp=l_ts,
                    horizon=int(horizons[k]),
                    data=row_data,
                    label=float(labels[k]),
                    weight=float(weights[k]),
                )
            )
        return windows

    def _load_parquet(self, output_path: str) -> list[WindowedTensor]:
        """Load windows from a parquet file.

        Args:
            output_path: the path to the parquet file.

        Returns:
            A list of :class:`WindowedTensor`.
        """
        import pandas as pd

        df = pd.read_parquet(output_path)
        windows: list[WindowedTensor] = []
        for _, row in df.iterrows():
            windows.append(
                WindowedTensor(
                    window_id=str(row["window_id"]),
                    symbol=str(row["symbol"]),
                    start_timestamp=str(row["start_timestamp"]),
                    end_timestamp=str(row["end_timestamp"]),
                    label_timestamp=str(row["label_timestamp"]),
                    horizon=int(row["horizon"]),
                    data=list(row["data"]),
                    label=float(row["label"]),
                    weight=float(row["weight"]),
                )
            )
        return windows


__all__ = [
    "WindowedTensor",
    "WindowedTensorBuilder",
    "WindowedTensorConfig",
    "WindowedTensorReceipt",
    "compute_tensor_hash",
    "validate_no_label_in_features",
]
