"""quant_foundry.sequence_manifest — sequence dataset manifest for temporal windows.

TASK-10.1: SequenceDatasetManifest.

This module defines the manifest schema for **sequence / temporal-window**
datasets — the counterpart to the tabular :class:`FeatureLakeManifest` in
:mod:`quant_foundry.dataset_manifest`. A sequence dataset is a collection of
fixed-length temporal windows (e.g. 60 bars of OHLCV) used to train
sequence models (RNN / Transformer / TCN).

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):
- **No future leakage**: the label timestamp must be strictly after the
  feature window end. :func:`validate_no_future_leakage` fail-closes if a
  window's label timestamp is not after its end.
- **Availability cutoff**: data must be available up to at least the label
  timestamp — you cannot label a window whose label period hasn't happened
  yet (``availability_cutoff >= label_timestamp``).
- **Deterministic window ids**: each :class:`WindowSpec` has a deterministic
  ``window_id`` of the form ``symbol_start_end_horizon`` so two runs over
  the same data produce identical ids.
- **Deterministic data hash**: :func:`compute_sequence_data_hash` produces a
  stable SHA-256 over the raw bytes of a numpy tensor.
- **Fold consistency**: :func:`validate_fold_assignment` checks that every
  window's ``fold_id`` refers to a real fold in the :class:`FoldSpec` and
  that the window count matches the fold assignment.

The module reuses :class:`FoldSpec` / :class:`FoldWindow` from
:mod:`quant_foundry.dataset_manifest` (T-3.1 / T-8.1) and the temporal
parsing helper :func:`_parse_temporal`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.dataset_manifest import FoldSpec, _parse_temporal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Allowed dtype strings for :class:`SequenceChannel`.
_ALLOWED_DTYPES: frozenset[str] = frozenset({"float32", "float64", "int32"})

#: Allowed normalization strategies.
_ALLOWED_NORMALIZATIONS: frozenset[str] = frozenset({"standard", "robust", "minmax", "none"})

#: Allowed missing-value policies.
_ALLOWED_MISSING_POLICIES: frozenset[str] = frozenset({"fail", "mean_fill", "zero_fill"})

# 64-char lowercase hex (SHA-256) — same pattern as dataset_manifest.py.
_HEX256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _validate_hex256(value: str, field_name: str) -> str:
    """Require a 64-char hex SHA-256, return lowercase.

    Args:
        value: the hash string to validate.
        field_name: the field name for error messages.

    Returns:
        The lowercase hex string.

    Raises:
        ValueError: if ``value`` is not a 64-char hex string.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty 64-char hex string")
    if not _HEX256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-char hex SHA-256; got {value!r}")
    return value.lower()


def _validate_iso_temporal(value: str, field_name: str) -> str:
    """Validate that ``value`` is a parseable ISO date/datetime string.

    Args:
        value: the string to validate.
        field_name: the field name for error messages.

    Returns:
        The validated string.

    Raises:
        ValueError: if ``value`` is not a parseable ISO temporal.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty ISO datetime string; got {value!r}")
    _parse_temporal(value)
    return value


# ---------------------------------------------------------------------------
# SequenceChannel
# ---------------------------------------------------------------------------


class SequenceChannel(BaseModel):
    """A single channel (feature stream) in a sequence dataset.

    A sequence dataset is a multivariate time series; each *channel* is one
    of the parallel feature streams (e.g. ``"close"``, ``"volume"``,
    ``"returns"``). The channel declaration fixes its dtype, normalization
    strategy, and missing-value policy so that two consumers of the same
    manifest produce identical tensors.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        name: the channel name (e.g. ``"close"``, ``"volume"``,
            ``"returns"``). Must be a non-empty string.
        dtype: the numpy dtype of the channel data. One of
            ``"float32"``, ``"float64"``, ``"int32"``.
        normalization: the normalization strategy. One of ``"standard"``
            (z-score), ``"robust`` (median/IQR), ``"minmax"``, ``"none"``.
            Defaults to ``"standard"``.
        missing_policy: the missing-value policy. One of ``"fail"``,
            ``"mean_fill"``, ``"zero_fill"``. Defaults to ``"fail"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    dtype: str
    normalization: str = "standard"
    missing_policy: str = "fail"

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("SequenceChannel.name must be a non-empty string")
        return v

    @field_validator("dtype")
    @classmethod
    def _dtype_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_DTYPES:
            raise ValueError(
                f"SequenceChannel.dtype must be one of {sorted(_ALLOWED_DTYPES)!r}; got {v!r}"
            )
        return v

    @field_validator("normalization")
    @classmethod
    def _normalization_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_NORMALIZATIONS:
            raise ValueError(
                f"SequenceChannel.normalization must be one of "
                f"{sorted(_ALLOWED_NORMALIZATIONS)!r}; got {v!r}"
            )
        return v

    @field_validator("missing_policy")
    @classmethod
    def _missing_policy_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_MISSING_POLICIES:
            raise ValueError(
                f"SequenceChannel.missing_policy must be one of "
                f"{sorted(_ALLOWED_MISSING_POLICIES)!r}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# SequenceDatasetManifest
# ---------------------------------------------------------------------------


class SequenceDatasetManifest(BaseModel):
    """Manifest for a sequence (temporal-window) dataset.

    This is the contract of record for a sequence dataset export. It fixes
    the universe (symbols), the channel schema, the window geometry
    (length, stride, horizons), the temporal boundaries, the data location
    + hash, and optional fold assignment reference.

    Leakage-safe invariants (fail-closed at construction):
    - ``window_end > window_start`` (non-empty feature window).
    - ``label_timestamp > window_end`` (label is after the feature window —
      no future leakage into the feature window).
    - ``availability_cutoff >= label_timestamp`` (data must be available up
      to at least the label timestamp — you cannot label a window whose
      label period hasn't happened yet).
    - No duplicate symbols.
    - No duplicate channel names.
    - ``window_length >= 1``, ``stride >= 1``.
    - ``horizons`` non-empty, each ``>= 1``.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        dataset_id: the dataset identifier.
        symbols: list of instrument symbols (at least 1, no duplicates).
        channels: list of :class:`SequenceChannel` (at least 1, no
            duplicate names).
        window_length: the number of time steps in each feature window
            (>= 1).
        stride: the step between consecutive window starts (>= 1).
        horizons: list of label horizons in time steps (at least 1, each
            >= 1).
        window_start: ISO datetime — inclusive start of the feature window
            range.
        window_end: ISO datetime — inclusive end of the feature window
            range (must be > window_start).
        label_timestamp: ISO datetime — the timestamp of the label. Must
            be > window_end (label is after the feature window).
        availability_cutoff: ISO datetime — data is available up to this
            point. Must be >= label_timestamp.
        normalization_policy: the default normalization policy. Defaults
            to ``"standard"``.
        fold_assignment_uri: optional URI to the fold assignment file.
        fold_assignment_hash: optional SHA-256 of the fold assignment.
        data_uri: path/URI to the tensor data file.
        data_hash: SHA-256 of the tensor data (64-char hex).
        created_at: ISO timestamp of manifest creation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    symbols: list[str]
    channels: list[SequenceChannel]
    window_length: int
    stride: int
    horizons: list[int]
    window_start: str
    window_end: str
    label_timestamp: str
    availability_cutoff: str
    normalization_policy: str = "standard"
    fold_assignment_uri: str | None = None
    fold_assignment_hash: str | None = None
    data_uri: str
    data_hash: str
    created_at: str

    # --- field validators ------------------------------------------------

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dataset_id must be a non-empty string")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("symbols must contain at least 1 symbol")
        for s in v:
            if not isinstance(s, str) or not s.strip():
                raise ValueError("symbols entries must be non-empty strings")
        return v

    @field_validator("channels")
    @classmethod
    def _channels_nonempty(cls, v: list[SequenceChannel]) -> list[SequenceChannel]:
        if not v:
            raise ValueError("channels must contain at least 1 channel")
        return v

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

    @field_validator(
        "window_start",
        "window_end",
        "label_timestamp",
        "availability_cutoff",
        "created_at",
    )
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name)

    @field_validator("data_hash")
    @classmethod
    def _data_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "data_hash")

    @field_validator("fold_assignment_hash")
    @classmethod
    def _fold_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "fold_assignment_hash")

    @field_validator("data_uri")
    @classmethod
    def _data_uri_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("data_uri must be a non-empty string")
        return v

    # --- model validators ------------------------------------------------

    @model_validator(mode="after")
    def _no_duplicate_symbols(self) -> SequenceDatasetManifest:
        """Symbols must be unique (no duplicate instruments)."""
        if len(set(self.symbols)) != len(self.symbols):
            dupes = sorted({s for s in self.symbols if self.symbols.count(s) > 1})
            raise ValueError(f"symbols must not contain duplicates: {dupes!r}")
        return self

    @model_validator(mode="after")
    def _no_duplicate_channels(self) -> SequenceDatasetManifest:
        """Channel names must be unique (no duplicate channels)."""
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"channels must not contain duplicate names: {dupes!r}")
        return self

    @model_validator(mode="after")
    def _check_time_ordering(self) -> SequenceDatasetManifest:
        """Enforce window_start < window_end < label_timestamp and
        availability_cutoff >= label_timestamp (no future leakage)."""
        ws = _parse_temporal(self.window_start)
        we = _parse_temporal(self.window_end)
        lt = _parse_temporal(self.label_timestamp)
        ac = _parse_temporal(self.availability_cutoff)
        if not (ws < we):
            raise ValueError(
                f"window_end must be > window_start "
                f"(window_start={self.window_start!r}, "
                f"window_end={self.window_end!r})"
            )
        if not (lt > we):
            raise ValueError(
                f"label_timestamp must be > window_end (no future leakage) "
                f"(label_timestamp={self.label_timestamp!r}, "
                f"window_end={self.window_end!r})"
            )
        if not (ac >= lt):
            raise ValueError(
                f"availability_cutoff must be >= label_timestamp "
                f"(availability_cutoff={self.availability_cutoff!r}, "
                f"label_timestamp={self.label_timestamp!r})"
            )
        return self

    @model_validator(mode="after")
    def _fold_fields_consistent(self) -> SequenceDatasetManifest:
        """fold_assignment_uri and fold_assignment_hash must both be set or
        both be None."""
        uri_set = self.fold_assignment_uri is not None
        hash_set = self.fold_assignment_hash is not None
        if uri_set != hash_set:
            raise ValueError(
                "fold_assignment_uri and fold_assignment_hash must both be "
                "set or both be None "
                f"(uri={self.fold_assignment_uri!r}, "
                f"hash={self.fold_assignment_hash!r})"
            )
        return self


# ---------------------------------------------------------------------------
# WindowSpec
# ---------------------------------------------------------------------------


class WindowSpec(BaseModel):
    """A single temporal window specification.

    A :class:`WindowSpec` describes one (symbol, start, end, horizon) window
    in a sequence dataset. The ``window_id`` is deterministic
    (``symbol_start_end_horizon``) so two runs over the same data produce
    identical ids.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        window_id: deterministic id of the form
            ``"{symbol}_{start}_{end}_{horizon}"``.
        symbol: the instrument symbol for this window.
        start: ISO datetime — inclusive start of the feature window.
        end: ISO datetime — inclusive end of the feature window.
        label_timestamp: ISO datetime — the timestamp of the label. Must
            be > end (no future leakage into the feature window).
        horizon: the label horizon in time steps (>= 1).
        fold_id: the fold this window belongs to, or None if not yet
            assigned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_id: str
    symbol: str
    start: str
    end: str
    label_timestamp: str
    horizon: int
    fold_id: int | None = None

    @field_validator("window_id", "symbol")
    @classmethod
    def _nonempty_str(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return v

    @field_validator("start", "end", "label_timestamp")
    @classmethod
    def _temporal_parseable(cls, v: str, info: Any) -> str:
        return _validate_iso_temporal(v, info.field_name)

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if not isinstance(v, int) or v < 1:
            raise ValueError(f"horizon must be an integer >= 1; got {v!r}")
        return v

    @field_validator("fold_id")
    @classmethod
    def _fold_id_nonnegative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"fold_id must be >= 0 or None; got {v}")
        return v

    @model_validator(mode="after")
    def _check_ordering(self) -> WindowSpec:
        """Enforce end > start and label_timestamp > end (no future leakage)."""
        s = _parse_temporal(self.start)
        e = _parse_temporal(self.end)
        lt = _parse_temporal(self.label_timestamp)
        if not (e > s):
            raise ValueError(f"end must be > start (start={self.start!r}, end={self.end!r})")
        if not (lt > e):
            raise ValueError(
                f"label_timestamp must be > end (no future leakage) "
                f"(label_timestamp={self.label_timestamp!r}, "
                f"end={self.end!r})"
            )
        return self


# ---------------------------------------------------------------------------
# validate_no_future_leakage
# ---------------------------------------------------------------------------


def validate_no_future_leakage(window: WindowSpec) -> bool:
    """Check that a window has no future data leakage.

    Returns True if the window's ``label_timestamp`` is strictly after its
    ``end`` (the label is in the future relative to the feature window, so
    no future data bleeds into the feature window).

    Args:
        window: the :class:`WindowSpec` to check.

    Returns:
        True if there is no future leakage.

    Raises:
        ValueError: if ``label_timestamp <= end`` (future leakage detected).
    """
    end_epoch = _parse_temporal(window.end)
    label_epoch = _parse_temporal(window.label_timestamp)
    if not (label_epoch > end_epoch):
        raise ValueError(
            f"future leakage detected: label_timestamp "
            f"({window.label_timestamp!r}) must be > end "
            f"({window.end!r}) for window {window.window_id!r}"
        )
    return True


# ---------------------------------------------------------------------------
# validate_fold_assignment
# ---------------------------------------------------------------------------


def validate_fold_assignment(
    windows: list[WindowSpec],
    fold_spec: FoldSpec,
) -> bool:
    """Validate that window fold assignments are consistent with a FoldSpec.

    Checks:
    - Every window's ``fold_id`` (if not None) refers to a real fold in
      ``fold_spec.folds``.
    - If any window has a fold_id, all windows must have a fold_id (no
      partial assignment).
    - The window count is non-empty.

    Args:
        windows: the list of :class:`WindowSpec` to validate.
        fold_spec: the :class:`FoldSpec` to validate against.

    Returns:
        True if the fold assignment is valid.

    Raises:
        ValueError: if any check fails (fail-closed).
    """
    if not windows:
        raise ValueError("windows must be non-empty for fold validation")

    valid_fold_ids = {f.fold_id for f in fold_spec.folds}

    # Collect fold_ids.
    fold_ids = [w.fold_id for w in windows]
    assigned = [fid for fid in fold_ids if fid is not None]
    unassigned = [fid for fid in fold_ids if fid is None]

    # If some are assigned and some are not, that's a partial assignment.
    if assigned and unassigned:
        raise ValueError(
            f"partial fold assignment: {len(assigned)} windows have "
            f"fold_ids, {len(unassigned)} do not — all or none must be "
            "assigned"
        )

    # Check all assigned fold_ids are valid.
    bad = sorted({fid for fid in assigned if fid not in valid_fold_ids})
    if bad:
        raise ValueError(
            f"windows contain invalid fold_ids: {bad!r} (valid: {sorted(valid_fold_ids)!r})"
        )

    # If folds are assigned, check each fold has at least one window.
    if assigned:
        fold_counts: dict[int, int] = {}
        for fid in assigned:
            fold_counts[fid] = fold_counts.get(fid, 0) + 1
        for fw in fold_spec.folds:
            if fold_counts.get(fw.fold_id, 0) == 0:
                raise ValueError(
                    f"fold {fw.fold_id} has no windows assigned — every "
                    "fold must have at least one window"
                )

    return True


# ---------------------------------------------------------------------------
# compute_sequence_data_hash
# ---------------------------------------------------------------------------


def compute_sequence_data_hash(data: Any) -> str:
    """Compute a deterministic SHA-256 hash of tensor data.

    The hash is computed over the raw bytes of a numpy array (via
    ``ndarray.tobytes()``), which is deterministic for a given dtype and
    shape. Two arrays with the same dtype, shape, and values produce the
    same hash; any change to a value, dtype, or shape alters the hash.

    Args:
        data: a numpy array (or any object with a ``tobytes()`` method).

    Returns:
        A 64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if ``data`` does not support ``tobytes()``.
    """
    if data is None:
        raise ValueError("data must not be None")
    if hasattr(data, "tobytes"):
        raw = data.tobytes()
        # Include shape and dtype in the hash so that two arrays with the
        # same values but different shapes/dtypes produce different hashes.
        shape = getattr(data, "shape", ())
        dtype = str(getattr(data, "dtype", ""))
        prefix = f"{dtype}|{shape}|".encode()
        return hashlib.sha256(prefix + raw).hexdigest()
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        return hashlib.sha256(raw).hexdigest()
    else:
        raise ValueError(
            f"data must be a numpy array or bytes-like object with a "
            f"tobytes() method; got {type(data).__name__}"
        )


# ---------------------------------------------------------------------------
# create_windows
# ---------------------------------------------------------------------------


def _make_window_id(symbol: str, start: str, end: str, horizon: int) -> str:
    """Build a deterministic window id: ``symbol_start_end_horizon``.

    Args:
        symbol: the instrument symbol.
        start: the window start ISO string.
        end: the window end ISO string.
        horizon: the label horizon.

    Returns:
        The deterministic window id string.
    """
    return f"{symbol}_{start}_{end}_{horizon}"


def create_windows(
    manifest: SequenceDatasetManifest,
    timestamps: list[str],
    symbols: list[str],
    fold_spec: FoldSpec | None = None,
) -> list[WindowSpec]:
    """Generate window specs from manifest parameters.

    Slides a window of ``manifest.window_length`` timestamps over the
    ``timestamps`` list with ``manifest.stride``, for each symbol in
    ``symbols``. For each window position and each horizon in
    ``manifest.horizons``, creates a :class:`WindowSpec` whose:
    - ``start`` = ``timestamps[i]``
    - ``end`` = ``timestamps[i + window_length - 1]``
    - ``label_timestamp`` = ``timestamps[i + window_length - 1 + horizon]``
      (if that index exists; otherwise the window is skipped — the label
      period hasn't happened yet)

    If ``fold_spec`` is provided, each window's ``fold_id`` is assigned
    based on which fold's train or validation window contains the window's
    start timestamp.

    Args:
        manifest: the :class:`SequenceDatasetManifest` defining window
            geometry.
        timestamps: a sorted list of ISO datetime strings (the time axis).
        symbols: the list of symbols to generate windows for (must be a
            subset of ``manifest.symbols``).
        fold_spec: optional :class:`FoldSpec` for fold assignment.

    Returns:
        A list of :class:`WindowSpec`.

    Raises:
        ValueError: if ``timestamps`` is too short for the window length,
            if ``symbols`` is empty, or if a symbol is not in the manifest.
    """
    if not timestamps:
        raise ValueError("timestamps must be non-empty")
    if not symbols:
        raise ValueError("symbols must be non-empty")

    manifest_symbols = set(manifest.symbols)
    for s in symbols:
        if s not in manifest_symbols:
            raise ValueError(
                f"symbol {s!r} is not in manifest.symbols ({sorted(manifest_symbols)!r})"
            )

    wl = manifest.window_length
    stride = manifest.stride

    if len(timestamps) < wl:
        raise ValueError(
            f"timestamps has {len(timestamps)} entries but window_length "
            f"is {wl} — need at least {wl} timestamps"
        )

    # Pre-compute fold window epochs if fold_spec is provided.
    fold_epochs: list[dict[str, Any]] | None = None
    if fold_spec is not None:
        fold_epochs = []
        for fw in fold_spec.folds:
            fold_epochs.append(
                {
                    "fold_id": fw.fold_id,
                    "train_start": _parse_temporal(fw.train_start),
                    "train_end": _parse_temporal(fw.train_end),
                    "val_start": _parse_temporal(fw.validation_start),
                    "val_end": _parse_temporal(fw.validation_end),
                }
            )

    windows: list[WindowSpec] = []
    n = len(timestamps)

    for symbol in symbols:
        # Slide the window over timestamps with stride.
        for i in range(0, n - wl + 1, stride):
            start = timestamps[i]
            end = timestamps[i + wl - 1]
            start_epoch = _parse_temporal(start)

            for horizon in manifest.horizons:
                label_idx = i + wl - 1 + horizon
                if label_idx >= n:
                    # Label period hasn't happened yet — skip this window.
                    continue
                label_ts = timestamps[label_idx]

                # Assign fold_id if fold_spec provided.
                fold_id: int | None = None
                if fold_epochs is not None:
                    for fe in fold_epochs:
                        in_train = fe["train_start"] <= start_epoch <= fe["train_end"]
                        in_val = fe["val_start"] <= start_epoch <= fe["val_end"]
                        if in_train or in_val:
                            fold_id = int(fe["fold_id"])
                            break

                window_id = _make_window_id(symbol, start, end, horizon)
                windows.append(
                    WindowSpec(
                        window_id=window_id,
                        symbol=symbol,
                        start=start,
                        end=end,
                        label_timestamp=label_ts,
                        horizon=horizon,
                        fold_id=fold_id,
                    )
                )

    return windows


# ---------------------------------------------------------------------------
# SequenceManifestBuilder
# ---------------------------------------------------------------------------


class SequenceManifestBuilder:
    """Fluent builder for :class:`SequenceDatasetManifest`.

    Provides a chainable API for constructing a sequence dataset manifest
    field-by-field, then calling :meth:`build` to validate and create the
    immutable manifest.

    Example::

        manifest = (
            SequenceManifestBuilder("seq_dataset_001")
            .with_symbols(["AAPL", "MSFT"])
            .with_channels([
                SequenceChannel(name="close", dtype="float32"),
                SequenceChannel(name="volume", dtype="float64"),
            ])
            .with_window(length=60, stride=5)
            .with_horizons([1, 5, 21])
            .with_time_range(
                start="2024-01-01T00:00:00Z",
                end="2024-06-01T00:00:00Z",
                label_ts="2024-06-02T00:00:00Z",
                avail_cutoff="2024-06-02T00:00:00Z",
            )
            .with_data(
                uri="s3://bucket/seq_001.npy",
                data_hash=compute_sequence_data_hash(arr),
            )
            .build()
        )
    """

    def __init__(self, dataset_id: str) -> None:
        """Initialize the builder with a dataset id.

        Args:
            dataset_id: the dataset identifier.
        """
        self._dataset_id: str = dataset_id
        self._symbols: list[str] = []
        self._channels: list[SequenceChannel] = []
        self._window_length: int = 0
        self._stride: int = 0
        self._horizons: list[int] = []
        self._window_start: str = ""
        self._window_end: str = ""
        self._label_timestamp: str = ""
        self._availability_cutoff: str = ""
        self._normalization_policy: str = "standard"
        self._fold_assignment_uri: str | None = None
        self._fold_assignment_hash: str | None = None
        self._data_uri: str = ""
        self._data_hash: str = ""
        self._created_at: str = ""

    def with_symbols(self, symbols: list[str]) -> SequenceManifestBuilder:
        """Set the symbols (universe).

        Args:
            symbols: list of instrument symbols (at least 1).

        Returns:
            self (for chaining).
        """
        self._symbols = list(symbols)
        return self

    def with_channels(self, channels: list[SequenceChannel]) -> SequenceManifestBuilder:
        """Set the channels (feature streams).

        Args:
            channels: list of :class:`SequenceChannel` (at least 1).

        Returns:
            self (for chaining).
        """
        self._channels = list(channels)
        return self

    def with_window(self, length: int, stride: int) -> SequenceManifestBuilder:
        """Set the window geometry.

        Args:
            length: the window length (number of time steps, >= 1).
            stride: the stride between consecutive windows (>= 1).

        Returns:
            self (for chaining).
        """
        self._window_length = length
        self._stride = stride
        return self

    def with_horizons(self, horizons: list[int]) -> SequenceManifestBuilder:
        """Set the label horizons.

        Args:
            horizons: list of horizons in time steps (at least 1, each >= 1).

        Returns:
            self (for chaining).
        """
        self._horizons = list(horizons)
        return self

    def with_time_range(
        self,
        start: str,
        end: str,
        label_ts: str,
        avail_cutoff: str,
    ) -> SequenceManifestBuilder:
        """Set the temporal boundaries.

        Args:
            start: ISO datetime — feature window start.
            end: ISO datetime — feature window end (must be > start).
            label_ts: ISO datetime — label timestamp (must be > end).
            avail_cutoff: ISO datetime — availability cutoff
                (must be >= label_ts).

        Returns:
            self (for chaining).
        """
        self._window_start = start
        self._window_end = end
        self._label_timestamp = label_ts
        self._availability_cutoff = avail_cutoff
        return self

    def with_data(self, uri: str, data_hash: str) -> SequenceManifestBuilder:
        """Set the data location and hash.

        Args:
            uri: path/URI to the tensor data file.
            data_hash: SHA-256 of the tensor data (64-char hex).

        Returns:
            self (for chaining).
        """
        self._data_uri = uri
        self._data_hash = data_hash
        return self

    def with_folds(self, uri: str, hash: str) -> SequenceManifestBuilder:
        """Set the fold assignment reference.

        Args:
            uri: URI to the fold assignment file.
            hash: SHA-256 of the fold assignment (64-char hex).

        Returns:
            self (for chaining).
        """
        self._fold_assignment_uri = uri
        self._fold_assignment_hash = hash
        return self

    def with_created_at(self, created_at: str) -> SequenceManifestBuilder:
        """Set the creation timestamp.

        Args:
            created_at: ISO timestamp of manifest creation.

        Returns:
            self (for chaining).
        """
        self._created_at = created_at
        return self

    def with_normalization_policy(self, policy: str) -> SequenceManifestBuilder:
        """Set the default normalization policy.

        Args:
            policy: the normalization policy string.

        Returns:
            self (for chaining).
        """
        self._normalization_policy = policy
        return self

    def build(self) -> SequenceDatasetManifest:
        """Build and validate the :class:`SequenceDatasetManifest`.

        Returns:
            The validated, frozen manifest.

        Raises:
            ValueError: if any required field is missing or validation
                fails (fail-closed).
        """
        if not self._created_at:
            # Default to now if not set.
            from datetime import datetime

            self._created_at = datetime.now(UTC).isoformat()

        return SequenceDatasetManifest(
            dataset_id=self._dataset_id,
            symbols=self._symbols,
            channels=self._channels,
            window_length=self._window_length,
            stride=self._stride,
            horizons=self._horizons,
            window_start=self._window_start,
            window_end=self._window_end,
            label_timestamp=self._label_timestamp,
            availability_cutoff=self._availability_cutoff,
            normalization_policy=self._normalization_policy,
            fold_assignment_uri=self._fold_assignment_uri,
            fold_assignment_hash=self._fold_assignment_hash,
            data_uri=self._data_uri,
            data_hash=self._data_hash,
            created_at=self._created_at,
        )
