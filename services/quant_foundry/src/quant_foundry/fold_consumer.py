"""quant_foundry.fold_consumer — manifest-driven fold consumption (T-8.4).

This module implements **manifest-driven fold consumption**: the trainer
reads fold assignments from the manifest's :class:`FoldSpec` as the
*contract of record* and never re-derives fold boundaries from the data.

The flow is:

1. The manifest declares a :class:`~quant_foundry.dataset_manifest.FoldSpec`
   (a list of :class:`FoldWindow` with train/validation/embargo windows).
2. :func:`consume_manifest_folds` reads the dataframe, extracts row keys
   using the declared ``row_id_columns``, and assigns each row to a fold
   based on the fold windows' time ranges. The result is a
   :class:`FoldAssignment` — a parallel list of row keys + fold ids.
3. :func:`validate_fold_assignment` checks the assignment is consistent
   with the fold spec (valid fold ids, no train/validation overlap after
   embargo, row counts match).
4. :func:`get_fold_data` returns the (train_indices, validation_indices)
   for a given fold so the trainer can slice the dataframe.
5. :func:`verify_fold_determinism` runs consumption multiple times and
   asserts identical results (deterministic).

Fail-closed behaviour:
- A production manifest with no ``FoldSpec`` raises
  ``ValueError("production manifest requires fold spec")``.
- Fold windows with invalid overlap after purge/embargo raise
  ``ValueError`` (enforced at :class:`FoldWindow` construction).
- A row that doesn't fit in any fold window raises ``ValueError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as _date
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.dataset_manifest import (
    FoldSpec,
    FoldWindow,
    _parse_temporal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_epoch(value: str) -> float:
    """Parse an ISO date/datetime string into a comparable epoch float.

    Thin wrapper around :func:`dataset_manifest._parse_temporal` kept here
    for module self-containment and clear error messages.
    """
    return _parse_temporal(value)


def _row_timestamp(df_row: Any, timestamp_column: str) -> float:
    """Extract a comparable epoch timestamp from a dataframe row.

    Accepts:
    - A string (ISO date/datetime) — parsed via :func:`_to_epoch`.
    - A numeric value — treated as a POSIX timestamp (seconds).
    - A ``datetime`` / ``date`` object — converted via ``timestamp()``.

    Raises:
        ValueError: if the value cannot be interpreted as a timestamp.
    """
    raw = df_row[timestamp_column]
    if isinstance(raw, str):
        return _to_epoch(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=UTC)
        return raw.timestamp()
    if isinstance(raw, _date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC).timestamp()
    raise ValueError(
        f"unsupported timestamp value in column {timestamp_column!r}: "
        f"{raw!r} (type {type(raw).__name__})"
    )


def _find_timestamp_column(fold_spec: FoldSpec, df_columns: list[str]) -> str:
    """Determine the timestamp column used for fold window matching.

    The fold spec's ``row_id_columns`` must include exactly one column that
    can serve as the temporal key for matching rows to fold windows. We
    pick the first ``row_id_columns`` entry that looks like a timestamp
    column (heuristic: contains ``time``, ``date``, or ``ts``), falling
    back to the first row_id_column if none match the heuristic.

    Raises:
        ValueError: if no suitable timestamp column can be determined.
    """
    cols = list(fold_spec.row_id_columns)
    if not cols:
        raise ValueError("FoldSpec.row_id_columns is empty — cannot find timestamp column")
    available = set(df_columns)
    # Heuristic: prefer a column whose name suggests a timestamp.
    for col in cols:
        low = col.lower()
        if any(hint in low for hint in ("time", "date", "ts", "as_of")):
            if col in available:
                return col
    # Fallback: the first row_id_column that exists in the dataframe.
    for col in cols:
        if col in available:
            return col
    missing = [c for c in cols if c not in available]
    raise ValueError(
        f"none of the FoldSpec.row_id_columns are present in the dataframe; "
        f"missing columns: {missing!r}"
    )


# ---------------------------------------------------------------------------
# FoldAssignment
# ---------------------------------------------------------------------------


class FoldAssignment(BaseModel):
    """The result of consuming a manifest's fold spec against a dataframe.

    A :class:`FoldAssignment` is a parallel list of row keys and fold ids,
    together with the originating :class:`FoldSpec`. It is the *consumed*
    contract that the trainer uses to slice the dataframe into train /
    validation sets per fold.

    Fields:
        row_keys: list of row key tuples (one per dataframe row, in row
            order). Each tuple is formed from the ``row_id_columns`` values
            of that row.
        fold_ids: list of fold ids parallel to ``row_keys``. ``fold_ids[i]``
            is the fold that row ``i`` belongs to.
        fold_spec: the :class:`FoldSpec` that produced this assignment.

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    row_keys: list[tuple]
    fold_ids: list[int]
    fold_spec: FoldSpec

    @field_validator("row_keys", "fold_ids")
    @classmethod
    def _nonempty_lists(cls, v: list, info: Any) -> list:
        if not isinstance(v, list):
            raise ValueError(f"{info.field_name} must be a list")
        return v

    @model_validator(mode="after")
    def _check_parallel_lengths(self) -> FoldAssignment:
        """row_keys and fold_ids must be parallel (same length)."""
        if len(self.row_keys) != len(self.fold_ids):
            raise ValueError(
                f"row_keys and fold_ids must be the same length; "
                f"got len(row_keys)={len(self.row_keys)}, "
                f"len(fold_ids)={len(self.fold_ids)}"
            )
        return self

    @model_validator(mode="after")
    def _check_fold_ids_valid(self) -> FoldAssignment:
        """Every fold_id must refer to a fold in fold_spec.folds."""
        valid_ids = {f.fold_id for f in self.fold_spec.folds}
        bad = sorted({fid for fid in self.fold_ids if fid not in valid_ids})
        if bad:
            raise ValueError(
                f"fold_ids contain values not in fold_spec.folds: {bad!r} "
                f"(valid ids: {sorted(valid_ids)!r})"
            )
        return self

    # --- convenience -----------------------------------------------------

    @property
    def n_rows(self) -> int:
        """The number of rows in this assignment."""
        return len(self.row_keys)

    @property
    def n_folds(self) -> int:
        """The number of folds in the originating fold spec."""
        return len(self.fold_spec.folds)

    def fold_row_counts(self) -> dict[int, int]:
        """Return a mapping ``fold_id -> row count`` for this assignment."""
        counts: dict[int, int] = {}
        for fid in self.fold_ids:
            counts[fid] = counts.get(fid, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Core: consume_manifest_folds
# ---------------------------------------------------------------------------


def consume_manifest_folds(
    fold_spec: FoldSpec,
    df: Any,
    row_id_columns: list[str] | None = None,
    *,
    timestamp_column: str | None = None,
) -> FoldAssignment:
    """Consume a manifest's fold spec and assign each dataframe row to a fold.

    This is the primary entry point for manifest-driven fold consumption.
    It reads the dataframe, extracts row keys using ``row_id_columns``
    (defaulting to ``fold_spec.row_id_columns``), and assigns each row to
    a fold based on the fold windows' time ranges.

    A row is assigned to a fold if its timestamp falls within that fold's
    **train** or **validation** window (after embargo). The timestamp
    column is auto-detected from ``row_id_columns`` unless
    ``timestamp_column`` is given explicitly.

    Fail-closed:
    - If a row doesn't fit in any fold window, raises ``ValueError``.
    - If the fold windows have invalid overlap (caught at ``FoldWindow``
      construction), raises ``ValueError``.

    Args:
        fold_spec: the :class:`FoldSpec` from the manifest.
        df: the dataframe (pandas DataFrame or any object supporting
            ``iterrows``-like iteration with column access). Must have
            the columns listed in ``row_id_columns``.
        row_id_columns: the columns that form the stable row key. If
            None, defaults to ``fold_spec.row_id_columns``.
        timestamp_column: the column to use as the temporal key for fold
            matching. If None, auto-detected from ``row_id_columns``.

    Returns:
        A :class:`FoldAssignment` with parallel ``row_keys`` and
        ``fold_ids``.

    Raises:
        ValueError: if any row doesn't fit in any fold window, or if the
            fold spec / dataframe are inconsistent.
    """
    cols = list(row_id_columns) if row_id_columns is not None else list(fold_spec.row_id_columns)
    if not cols:
        raise ValueError("row_id_columns must be non-empty")

    # Determine the dataframe columns.
    df_columns = _get_df_columns(df)
    for col in cols:
        if col not in df_columns:
            raise ValueError(
                f"row_id_column {col!r} not found in dataframe columns {sorted(df_columns)!r}"
            )

    # Determine the timestamp column for fold matching.
    ts_col = timestamp_column
    if ts_col is None:
        ts_col = _find_timestamp_column(fold_spec, df_columns)
    elif ts_col not in df_columns:
        raise ValueError(
            f"timestamp_column {ts_col!r} not found in dataframe columns {sorted(df_columns)!r}"
        )

    # Pre-compute fold window epochs for fast comparison.
    fold_epochs: list[dict[str, float]] = []
    for fw in fold_spec.folds:
        fold_epochs.append(
            {
                "fold_id": fw.fold_id,
                "train_start": _to_epoch(fw.train_start),
                "train_end": _to_epoch(fw.train_end),
                "validation_start": _to_epoch(fw.validation_start),
                "validation_end": _to_epoch(fw.validation_end),
            }
        )

    row_keys: list[tuple] = []
    fold_ids: list[int] = []

    for idx, row in _iter_rows(df):
        # Build the row key tuple from row_id_columns.
        key = tuple(row[col] for col in cols)
        row_keys.append(key)

        # Determine the row's timestamp.
        row_ts = _row_timestamp(row, ts_col)

        # Assign to a fold: a row belongs to a fold if its timestamp is in
        # that fold's train window OR validation window.
        assigned: int | None = None
        for fe in fold_epochs:
            in_train = fe["train_start"] <= row_ts <= fe["train_end"]
            in_val = fe["validation_start"] <= row_ts <= fe["validation_end"]
            if in_train or in_val:
                assigned = int(fe["fold_id"])
                break

        if assigned is None:
            raise ValueError(
                f"row {idx} (key={key!r}, {ts_col}={row[ts_col]!r}, "
                f"epoch={row_ts}) does not fit in any fold window — "
                "manifest fold spec does not cover this row"
            )
        fold_ids.append(assigned)

    return FoldAssignment(
        row_keys=row_keys,
        fold_ids=fold_ids,
        fold_spec=fold_spec,
    )


# ---------------------------------------------------------------------------
# validate_fold_assignment
# ---------------------------------------------------------------------------


def validate_fold_assignment(
    assignment: FoldAssignment,
    fold_spec: FoldSpec,
) -> bool:
    """Validate a :class:`FoldAssignment` against a :class:`FoldSpec`.

    Checks:
    - All fold_ids in the assignment are valid (refer to folds in the spec).
    - No overlap between train and validation windows *after* embargo, for
      every fold in the spec.
    - Row counts per fold are non-empty (each fold has at least one row).

    Args:
        assignment: the :class:`FoldAssignment` to validate.
        fold_spec: the :class:`FoldSpec` to validate against.

    Returns:
        True if the assignment is valid.

    Raises:
        ValueError: if any check fails (fail-closed).
    """
    valid_ids = {f.fold_id for f in fold_spec.folds}
    bad = sorted({fid for fid in assignment.fold_ids if fid not in valid_ids})
    if bad:
        raise ValueError(
            f"assignment contains invalid fold_ids: {bad!r} (valid: {sorted(valid_ids)!r})"
        )

    # Check no train/validation overlap after embargo for each fold.
    for fw in fold_spec.folds:
        te = _to_epoch(fw.train_end)
        vs = _to_epoch(fw.validation_start)
        if fw.embargo_until is not None:
            eu = _to_epoch(fw.embargo_until)
            if not (te < eu <= vs):
                raise ValueError(
                    f"fold {fw.fold_id}: invalid overlap after purge/embargo "
                    f"(train_end={fw.train_end!r}, embargo_until={fw.embargo_until!r}, "
                    f"validation_start={fw.validation_start!r})"
                )
        else:
            if not (te < vs):
                raise ValueError(
                    f"fold {fw.fold_id}: train_end must be < validation_start "
                    f"(train_end={fw.train_end!r}, "
                    f"validation_start={fw.validation_start!r}) — overlap detected"
                )

    # Check each fold has at least one row assigned.
    counts = assignment.fold_row_counts()
    for fw in fold_spec.folds:
        if counts.get(fw.fold_id, 0) == 0:
            raise ValueError(
                f"fold {fw.fold_id} has no rows assigned — every fold must have at least one row"
            )

    return True


# ---------------------------------------------------------------------------
# get_fold_data
# ---------------------------------------------------------------------------


def get_fold_data(
    assignment: FoldAssignment,
    fold_id: int,
) -> tuple[list[int], list[int]]:
    """Return (train_indices, validation_indices) for a given fold.

    The indices are row indices into the original dataframe (the same
    order as ``assignment.row_keys``). A row is a *train* row for a fold
    if its timestamp falls in that fold's train window; it is a
    *validation* row if its timestamp falls in that fold's validation
    window.

    Args:
        assignment: the :class:`FoldAssignment`.
        fold_id: the fold to slice.

    Returns:
        A tuple ``(train_indices, validation_indices)`` where each is a
        list of integer row indices.

    Raises:
        ValueError: if ``fold_id`` is not in the fold spec.
    """
    valid_ids = {f.fold_id for f in assignment.fold_spec.folds}
    if fold_id not in valid_ids:
        raise ValueError(f"fold_id {fold_id} not in fold spec (valid: {sorted(valid_ids)!r})")

    # Find the fold window for this fold_id.
    fold_window: FoldWindow | None = None
    for fw in assignment.fold_spec.folds:
        if fw.fold_id == fold_id:
            fold_window = fw
            break
    assert fold_window is not None  # guaranteed by the check above

    train_start = _to_epoch(fold_window.train_start)
    train_end = _to_epoch(fold_window.train_end)
    val_start = _to_epoch(fold_window.validation_start)
    val_end = _to_epoch(fold_window.validation_end)

    # We need the timestamp for each row to classify it as train vs val.
    # The row_keys contain the row_id_columns values; we need to find the
    # timestamp column within them. However, the row_keys are opaque
    # tuples — we don't know which element is the timestamp. Instead, we
    # classify based on the fold assignment: a row assigned to fold_id is
    # either a train row or a validation row. We re-derive this from the
    # row key by locating the timestamp column.
    ts_col = _find_timestamp_column(assignment.fold_spec, list(assignment.fold_spec.row_id_columns))
    ts_idx = list(assignment.fold_spec.row_id_columns).index(ts_col)

    train_indices: list[int] = []
    validation_indices: list[int] = []
    for i, fid in enumerate(assignment.fold_ids):
        if fid != fold_id:
            continue
        # Extract the timestamp value from the row key.
        ts_val = assignment.row_keys[i][ts_idx]
        row_ts = _coerce_key_timestamp(ts_val)
        if train_start <= row_ts <= train_end:
            train_indices.append(i)
        elif val_start <= row_ts <= val_end:
            validation_indices.append(i)
        else:
            # A row assigned to this fold but outside both windows —
            # this should not happen if consume_manifest_folds is correct,
            # but fail-closed.
            raise ValueError(
                f"row {i} (key={assignment.row_keys[i]!r}) is assigned to "
                f"fold {fold_id} but its timestamp {ts_val!r} is outside "
                f"both the train and validation windows"
            )

    return train_indices, validation_indices


def _coerce_key_timestamp(value: Any) -> float:
    """Coerce a row-key timestamp value to an epoch float."""
    if isinstance(value, str):
        return _to_epoch(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
    raise ValueError(f"cannot coerce row-key timestamp value {value!r} to epoch")


# ---------------------------------------------------------------------------
# verify_fold_determinism
# ---------------------------------------------------------------------------


def verify_fold_determinism(
    fold_spec: FoldSpec,
    df: Any,
    row_id_columns: list[str],
    *,
    n_runs: int = 3,
    timestamp_column: str | None = None,
) -> bool:
    """Verify that fold consumption is deterministic across repeated runs.

    Runs :func:`consume_manifest_folds` ``n_runs`` times and asserts that
    every run produces an identical :class:`FoldAssignment` (same row keys,
    same fold ids, same fold spec hash).

    Args:
        fold_spec: the :class:`FoldSpec`.
        df: the dataframe.
        row_id_columns: the row key columns.
        n_runs: the number of runs (default 3).
        timestamp_column: optional explicit timestamp column.

    Returns:
        True if all runs produce identical assignments.

    Raises:
        ValueError: if any run differs from the first (non-deterministic),
            or if consumption itself fails.
    """
    if n_runs < 2:
        raise ValueError(f"n_runs must be >= 2 for determinism check; got {n_runs}")

    baseline: FoldAssignment | None = None
    for run in range(n_runs):
        current = consume_manifest_folds(
            fold_spec,
            df,
            row_id_columns,
            timestamp_column=timestamp_column,
        )
        if baseline is None:
            baseline = current
            continue
        if current.row_keys != baseline.row_keys:
            raise ValueError(f"fold consumption is non-deterministic: row_keys differ on run {run}")
        if current.fold_ids != baseline.fold_ids:
            raise ValueError(f"fold consumption is non-deterministic: fold_ids differ on run {run}")
        if current.fold_spec.fold_assignment_hash != baseline.fold_spec.fold_assignment_hash:
            raise ValueError(
                f"fold consumption is non-deterministic: fold_spec hash differs on run {run}"
            )
    return True


# ---------------------------------------------------------------------------
# Production fail-closed helper
# ---------------------------------------------------------------------------


def require_fold_spec_for_production(
    fold_spec: FoldSpec | None,
    *,
    is_production: bool,
) -> FoldSpec:
    """Fail-closed guard: production manifests must have a fold spec.

    Args:
        fold_spec: the fold spec from the manifest (may be None).
        is_production: whether the manifest is a production manifest.

    Returns:
        The fold spec if present.

    Raises:
        ValueError: if ``is_production`` is True and ``fold_spec`` is None
            (``"production manifest requires fold spec"``).
    """
    if is_production and fold_spec is None:
        raise ValueError("production manifest requires fold spec")
    if fold_spec is None:
        raise ValueError("fold spec is required (was None)")
    return fold_spec


# ---------------------------------------------------------------------------
# DataFrame abstraction helpers
# ---------------------------------------------------------------------------


def _get_df_columns(df: Any) -> list[str]:
    """Return the list of column names for a dataframe-like object."""
    # pandas DataFrame.
    if hasattr(df, "columns") and hasattr(df.columns, "__iter__"):
        return [str(c) for c in df.columns]
    # list of dicts.
    if isinstance(df, list) and df and isinstance(df[0], dict):
        return list(df[0].keys())
    raise ValueError(
        f"unsupported dataframe type {type(df).__name__} — expected a "
        "pandas DataFrame or a list of dicts"
    )


def _iter_rows(df: Any):
    """Yield (index, row) pairs for a dataframe-like object.

    For a pandas DataFrame, uses ``iterrows()``. For a list of dicts,
    yields ``(i, dict)``.
    """
    if hasattr(df, "iterrows"):
        for idx, row in df.iterrows():
            yield int(idx), row
        return
    if isinstance(df, list):
        for i, row in enumerate(df):
            yield i, row
        return
    raise ValueError(f"unsupported dataframe type {type(df).__name__} — cannot iterate rows")
