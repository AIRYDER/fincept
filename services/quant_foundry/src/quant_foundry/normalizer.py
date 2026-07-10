"""
quant_foundry.normalizer — normalization and missing-value artifacts (T-9.2).

This module provides a self-contained, importable normalizer artifact store for
neural models trained by the quant foundry. It captures the normalization
statistics and missing-value policies computed during training so that the
**exact same** transformation can be applied at inference time — a critical
leakage / distribution-shift guard for tabular neural networks.

Capabilities:

- :class:`NormalizationMethod` — enum of supported normalization methods
  (STANDARD, ROBUST, MINMAX, NONE).
- :class:`MissingPolicy` — enum of supported missing-value policies
  (FAIL, MEAN_FILL, MEDIAN_FILL, ZERO_FILL).
- :class:`ColumnNormalizerStats` — per-column normalization statistics +
  missing-value policy (Pydantic v2, frozen + ``extra='forbid'``).
- :class:`NormalizerArtifact` — the top-level artifact bundling per-column
  stats, a deterministic content hash, creation timestamp, and optional fold id.
- :func:`compute_normalizer_hash` — deterministic SHA-256 over the canonical
  JSON of the column stats.
- :func:`apply_normalization` — apply a single column's normalization.
- :func:`apply_missing_policy` — apply a single column's missing-value fill.
- :func:`validate_normalizer_present` — fail-closed guard for inference.
- :func:`merge_fold_normalizers` — merge per-fold normalizers into one
  artifact (e.g. for ensembles).
- :class:`Normalizer` — the fit / transform / fit_transform / save / load
  façade used by the training and inference runtimes.

Design notes:

- **Pydantic v2, frozen, extra='forbid'.** All artifact models are frozen and
  reject unknown fields so a normalizer artifact can be hashed, signed, and
  referenced immutably by the dispatch / callback path.
- **No secrets.** Artifacts carry only statistics and policy metadata — never
  credentials or filesystem paths beyond the optional save/load path.
- **Deterministic hash.** ``compute_normalizer_hash`` serializes the column
  stats via ``model_dump_json`` (sorted, stable) and hashes the bytes with
  SHA-256. Two artifacts with identical stats produce identical hashes.
- **Fail closed.** ``validate_normalizer_present(required=True)`` raises when
  the artifact is missing — inference never silently skips normalization when
  the caller declares it required.
- **numpy for computations.** All numeric statistics are computed with numpy
  so NaN handling is explicit and consistent across platforms.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NormalizationMethod(StrEnum):
    """Supported normalization methods for tabular neural features.

    Members:
        STANDARD: z-score normalization — ``(x - mean) / std``.
        ROBUST: robust scaling — ``(x - median) / IQR``.
        MINMAX: min-max scaling — ``(x - min) / (max - min)``.
        NONE: no normalization (values are passed through unchanged).
    """

    STANDARD = "standard"
    ROBUST = "robust"
    MINMAX = "minmax"
    NONE = "none"


class MissingPolicy(StrEnum):
    """Supported missing-value policies for tabular neural features.

    Members:
        FAIL: raise on any missing value encountered during transform.
        MEAN_FILL: fill missing values with the training mean.
        MEDIAN_FILL: fill missing values with the training median.
        ZERO_FILL: fill missing values with 0.0.
    """

    FAIL = "fail"
    MEAN_FILL = "mean_fill"
    MEDIAN_FILL = "median_fill"
    ZERO_FILL = "zero_fill"


# ---------------------------------------------------------------------------
# Artifact models
# ---------------------------------------------------------------------------


class ColumnNormalizerStats(BaseModel):
    """Per-column normalization statistics + missing-value policy.

    Frozen + ``extra='forbid'`` for audit integrity. Only the fields
    relevant to the chosen ``method`` are expected to be non-None, but all
    fields are always present (as ``None`` when not applicable) so the schema
    is stable across methods.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    column_name: str
    method: NormalizationMethod
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    iqr: float | None = None
    min_val: float | None = None
    max_val: float | None = None
    missing_policy: MissingPolicy
    missing_fill_value: float | None = None
    n_samples: int
    n_missing: int

    @model_validator(mode="after")
    def _validate_non_negative_counts(self) -> ColumnNormalizerStats:
        """Ensure sample / missing counts are non-negative and missing <= samples."""
        if self.n_samples < 0:
            raise ValueError("n_samples must be non-negative")
        if self.n_missing < 0:
            raise ValueError("n_missing must be non-negative")
        if self.n_missing > self.n_samples:
            raise ValueError("n_missing cannot exceed n_samples")
        if not self.column_name:
            raise ValueError("column_name must be a non-empty string")
        return self


class NormalizerArtifact(BaseModel):
    """Top-level normalizer artifact bundling per-column stats + content hash.

    Frozen + ``extra='forbid'`` for audit integrity. The ``normalizer_hash``
    is a deterministic SHA-256 over the canonical JSON of ``columns`` and is
    validated on construction to ensure it matches the recomputed hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    columns: list[ColumnNormalizerStats] = Field(default_factory=list)
    normalizer_hash: str
    created_at: str
    fold_id: int | None = None

    @model_validator(mode="after")
    def _validate_no_duplicate_columns(self) -> NormalizerArtifact:
        """Reject duplicate column names — each column must appear once."""
        names = [c.column_name for c in self.columns]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(f"duplicate column name in normalizer artifact: {name!r}")
            seen.add(name)
        return self

    @model_validator(mode="after")
    def _validate_hash_matches_content(self) -> NormalizerArtifact:
        """Ensure ``normalizer_hash`` matches the recomputed content hash."""
        expected = compute_normalizer_hash(list(self.columns))
        if expected != self.normalizer_hash:
            raise ValueError(
                "normalizer_hash does not match content: "
                f"expected {expected!r}, got {self.normalizer_hash!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_artifact_id_nonempty(self) -> NormalizerArtifact:
        """Ensure ``artifact_id`` is a non-empty string."""
        if not self.artifact_id:
            raise ValueError("artifact_id must be a non-empty string")
        return self


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------


def compute_normalizer_hash(stats: list[ColumnNormalizerStats]) -> str:
    """Compute a deterministic SHA-256 hash over a list of column stats.

    The hash is computed over the canonical JSON of the column stats list
    (sorted keys, stable float serialization via Pydantic's ``model_dump_json``).
    Two identical stats lists always produce the same hash.

    Args:
        stats: list of :class:`ColumnNormalizerStats` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    # Serialize each column stats model with Pydantic's stable JSON, then wrap
    # in a JSON array string with sorted keys for full determinism.
    serialized = [c.model_dump_json() for c in stats]
    # Parse + re-dump with sort_keys for canonical ordering across runs.
    parsed = [json.loads(s) for s in serialized]
    payload = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Normalization + missing-policy application
# ---------------------------------------------------------------------------


def apply_missing_policy(values: Any, stats: ColumnNormalizerStats) -> Any:
    """Apply a single column's missing-value policy to a 1-D array of values.

    Args:
        values: a 1-D numpy array (or array-like) of floats, possibly
            containing NaN.
        stats: the :class:`ColumnNormalizerStats` describing the policy.

    Returns:
        A numpy float array with missing values filled per the policy.

    Raises:
        ValueError: if ``missing_policy`` is FAIL and any NaN is present.
    """
    arr = np.asarray(values, dtype=float)
    nan_mask = np.isnan(arr)
    n_nan = int(np.sum(nan_mask))

    if stats.missing_policy == MissingPolicy.FAIL:
        if n_nan > 0:
            raise ValueError(
                f"missing policy FAIL encountered {n_nan} missing value(s) "
                f"in column {stats.column_name!r}"
            )
        return arr

    if n_nan == 0:
        return arr

    filled = arr.copy()
    if stats.missing_policy == MissingPolicy.MEAN_FILL:
        fill = stats.missing_fill_value
        if fill is None:
            fill = stats.mean if stats.mean is not None else 0.0
        filled[nan_mask] = float(fill)
    elif stats.missing_policy == MissingPolicy.MEDIAN_FILL:
        fill = stats.missing_fill_value
        if fill is None:
            fill = stats.median if stats.median is not None else 0.0
        filled[nan_mask] = float(fill)
    elif stats.missing_policy == MissingPolicy.ZERO_FILL:
        filled[nan_mask] = 0.0
    else:  # pragma: no cover - exhaustive enum
        raise ValueError(f"unknown missing policy: {stats.missing_policy!r}")

    return filled


def apply_normalization(values: Any, stats: ColumnNormalizerStats) -> Any:
    """Apply a single column's normalization to a 1-D array of values.

    Missing values are **not** filled here — call :func:`apply_missing_policy`
    first if the values may contain NaN. The normalization is applied
    according to ``stats.method``:

    - STANDARD: ``(x - mean) / std``
    - ROBUST: ``(x - median) / IQR``
    - MINMAX: ``(x - min) / (max - min)``
    - NONE: passthrough

    Args:
        values: a 1-D numpy array (or array-like) of floats.
        stats: the :class:`ColumnNormalizerStats` describing the method.

    Returns:
        A numpy float array with the normalization applied.

    Raises:
        ValueError: if a required statistic is missing or degenerate
            (e.g. std == 0, IQR == 0, min == max).
    """
    arr = np.asarray(values, dtype=float)

    if stats.method == NormalizationMethod.NONE:
        return arr

    if stats.method == NormalizationMethod.STANDARD:
        if stats.mean is None or stats.std is None:
            raise ValueError(
                f"STANDARD normalization requires mean and std for column {stats.column_name!r}"
            )
        if stats.std == 0:
            raise ValueError(
                f"STANDARD normalization requires non-zero std for column {stats.column_name!r}"
            )
        return (arr - stats.mean) / stats.std

    if stats.method == NormalizationMethod.ROBUST:
        if stats.median is None or stats.iqr is None:
            raise ValueError(
                f"ROBUST normalization requires median and iqr for column {stats.column_name!r}"
            )
        if stats.iqr == 0:
            raise ValueError(
                f"ROBUST normalization requires non-zero iqr for column {stats.column_name!r}"
            )
        return (arr - stats.median) / stats.iqr

    if stats.method == NormalizationMethod.MINMAX:
        if stats.min_val is None or stats.max_val is None:
            raise ValueError(
                f"MINMAX normalization requires min_val and max_val for column "
                f"{stats.column_name!r}"
            )
        span = stats.max_val - stats.min_val
        if span == 0:
            raise ValueError(
                f"MINMAX normalization requires non-zero range for column {stats.column_name!r}"
            )
        return (arr - stats.min_val) / span

    # pragma: no cover - exhaustive enum
    raise ValueError(f"unknown normalization method: {stats.method!r}")


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


def validate_normalizer_present(
    artifact: NormalizerArtifact | None,
    required: bool = True,
) -> bool:
    """Validate that a normalizer artifact is present when required.

    Args:
        artifact: the normalizer artifact (or None).
        required: if True, a missing artifact raises; if False, a missing
            artifact is allowed (normalization is optional).

    Returns:
        True if the artifact is present, or if it is absent and not required.

    Raises:
        ValueError: if ``required`` is True and ``artifact`` is None.
    """
    if artifact is None:
        if required:
            raise ValueError("inference requires normalizer artifact")
        return True
    return True


# ---------------------------------------------------------------------------
# Fold merging
# ---------------------------------------------------------------------------


def merge_fold_normalizers(artifacts: list[NormalizerArtifact]) -> NormalizerArtifact:
    """Merge per-fold normalizer artifacts into a single ensemble artifact.

    The merge strategy uses the **first fold's stats** for each column. This
    keeps the merged artifact deterministic and avoids silently averaging
    statistics that may not be commensurate across folds (e.g. different
    fold-specific medians). All artifacts must share the same set of column
    names; otherwise a ValueError is raised.

    The merged artifact's ``fold_id`` is set to ``None`` (it is no longer
    fold-specific) and a fresh content hash is computed.

    Args:
        artifacts: list of per-fold :class:`NormalizerArtifact` to merge.

    Returns:
        A new :class:`NormalizerArtifact` merging the folds.

    Raises:
        ValueError: if the artifacts list is empty, or if the artifacts do
            not all share the same set of column names.
    """
    if not artifacts:
        raise ValueError("merge_fold_normalizers requires at least one artifact")

    base = artifacts[0]
    base_names = [c.column_name for c in base.columns]
    base_name_set = set(base_names)

    for art in artifacts[1:]:
        names = [c.column_name for c in art.columns]
        if set(names) != base_name_set:
            raise ValueError(
                "merge_fold_normalizers requires all artifacts to share the "
                "same set of column names"
            )

    # Use the first fold's stats for each column (deterministic).
    merged_columns: list[ColumnNormalizerStats] = []
    for name in base_names:
        # Find the column in the first artifact (base) by name.
        for col in base.columns:
            if col.column_name == name:
                merged_columns.append(col)
                break

    merged_hash = compute_normalizer_hash(merged_columns)
    created_at = datetime.now(UTC).isoformat()
    # Derive a merged artifact id from the first fold's id.
    merged_id = f"{base.artifact_id}::merged"

    return NormalizerArtifact(
        artifact_id=merged_id,
        columns=merged_columns,
        normalizer_hash=merged_hash,
        created_at=created_at,
        fold_id=None,
    )


# ---------------------------------------------------------------------------
# Normalizer façade
# ---------------------------------------------------------------------------


class Normalizer:
    """Fit / transform / save / load façade for normalizer artifacts.

    The normalizer computes per-column statistics from a pandas DataFrame
    during ``fit``, applies them during ``transform``, and persists the
    artifact as JSON via ``save_artifact`` / ``load_artifact``.

    Args:
        method: the :class:`NormalizationMethod` to apply to all columns.
        missing_policy: the :class:`MissingPolicy` to apply to all columns.
    """

    def __init__(
        self,
        method: NormalizationMethod = NormalizationMethod.STANDARD,
        missing_policy: MissingPolicy = MissingPolicy.MEAN_FILL,
    ) -> None:
        self.method = method
        self.missing_policy = missing_policy
        self.artifact_: NormalizerArtifact | None = None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_array(series: Any) -> np.ndarray:
        """Convert a pandas Series (or array-like) to a float numpy array."""
        return np.asarray(series, dtype=float)

    def _compute_column_stats(self, series: Any, column_name: str) -> ColumnNormalizerStats:
        """Compute :class:`ColumnNormalizerStats` for a single column."""
        arr = self._to_array(series)
        n_samples = int(arr.shape[0])
        nan_mask = np.isnan(arr)
        n_missing = int(np.sum(nan_mask))
        non_nan = arr[~nan_mask]

        mean: float | None = None
        std: float | None = None
        median: float | None = None
        iqr: float | None = None
        min_val: float | None = None
        max_val: float | None = None
        missing_fill_value: float | None = None

        if non_nan.shape[0] > 0:
            if self.method == NormalizationMethod.STANDARD:
                mean = float(np.mean(non_nan))
                std = float(np.std(non_nan))
                if self.missing_policy == MissingPolicy.MEAN_FILL:
                    missing_fill_value = mean
            elif self.method == NormalizationMethod.ROBUST:
                median = float(np.median(non_nan))
                q75 = float(np.percentile(non_nan, 75))
                q25 = float(np.percentile(non_nan, 25))
                iqr = q75 - q25
                if self.missing_policy == MissingPolicy.MEDIAN_FILL:
                    missing_fill_value = median
            elif self.method == NormalizationMethod.MINMAX:
                min_val = float(np.min(non_nan))
                max_val = float(np.max(non_nan))
            elif self.method == NormalizationMethod.NONE:
                # No stats needed, but still compute mean for mean_fill if used.
                if self.missing_policy == MissingPolicy.MEAN_FILL:
                    mean = float(np.mean(non_nan))
                    missing_fill_value = mean
                if self.missing_policy == MissingPolicy.MEDIAN_FILL:
                    median = float(np.median(non_nan))
                    missing_fill_value = median
            else:  # pragma: no cover - exhaustive enum
                raise ValueError(f"unknown normalization method: {self.method!r}")

        if self.missing_policy == MissingPolicy.ZERO_FILL:
            missing_fill_value = 0.0

        return ColumnNormalizerStats(
            column_name=column_name,
            method=self.method,
            mean=mean,
            std=std,
            median=median,
            iqr=iqr,
            min_val=min_val,
            max_val=max_val,
            missing_policy=self.missing_policy,
            missing_fill_value=missing_fill_value,
            n_samples=n_samples,
            n_missing=n_missing,
        )

    # -- public API --------------------------------------------------------

    def fit(self, df: Any, columns: list[str]) -> NormalizerArtifact:
        """Fit the normalizer on a DataFrame and return a :class:`NormalizerArtifact`.

        Args:
            df: a pandas DataFrame (or dict of array-likes).
            columns: the column names to compute statistics for.

        Returns:
            The fitted :class:`NormalizerArtifact`.

        Raises:
            ValueError: if ``columns`` is empty or a column is missing from df.
        """
        if not columns:
            raise ValueError("fit requires at least one column")

        col_stats: list[ColumnNormalizerStats] = []
        for col in columns:
            if col not in df:
                raise ValueError(f"column {col!r} not found in DataFrame")
            col_stats.append(self._compute_column_stats(df[col], col))

        normalizer_hash = compute_normalizer_hash(col_stats)
        created_at = datetime.now(UTC).isoformat()
        artifact_id = f"normalizer::{normalizer_hash[:16]}"

        artifact = NormalizerArtifact(
            artifact_id=artifact_id,
            columns=col_stats,
            normalizer_hash=normalizer_hash,
            created_at=created_at,
            fold_id=None,
        )
        self.artifact_ = artifact
        return artifact

    def transform(self, df: Any, columns: list[str]) -> Any:
        """Apply the fitted normalization + missing policy to a DataFrame.

        Returns a new DataFrame (a copy) with the specified columns
        normalized and missing values filled. Columns not in ``columns``
        are left untouched.

        Args:
            df: a pandas DataFrame.
            columns: the column names to transform.

        Returns:
            A transformed pandas DataFrame.

        Raises:
            ValueError: if the normalizer has not been fitted (no artifact).
        """
        if self.artifact_ is None:
            raise ValueError("Normalizer must be fit before transform")

        return self._transform_with_artifact(df, columns, self.artifact_)

    def _transform_with_artifact(
        self, df: Any, columns: list[str], artifact: NormalizerArtifact
    ) -> Any:
        """Internal: apply transform using a specific artifact."""
        stats_by_name = {c.column_name: c for c in artifact.columns}
        result = df.copy()
        for col in columns:
            if col not in stats_by_name:
                raise ValueError(f"column {col!r} not found in normalizer artifact")
            stats = stats_by_name[col]
            arr = self._to_array(df[col])
            arr = apply_missing_policy(arr, stats)
            arr = apply_normalization(arr, stats)
            result[col] = arr
        return result

    def fit_transform(self, df: Any, columns: list[str]) -> tuple[Any, NormalizerArtifact]:
        """Fit the normalizer and transform the DataFrame in one call.

        Args:
            df: a pandas DataFrame.
            columns: the column names to fit + transform.

        Returns:
            A tuple of (transformed DataFrame, NormalizerArtifact).
        """
        artifact = self.fit(df, columns)
        transformed = self._transform_with_artifact(df, columns, artifact)
        return transformed, artifact

    def save_artifact(self, path: str) -> None:
        """Save the fitted normalizer artifact as JSON.

        Args:
            path: filesystem path to write the artifact JSON to.

        Raises:
            ValueError: if the normalizer has not been fitted.
        """
        if self.artifact_ is None:
            raise ValueError("Normalizer must be fit before save_artifact")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.artifact_.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def load_artifact(path: str) -> NormalizerArtifact:
        """Load a normalizer artifact from a JSON file.

        Args:
            path: filesystem path to read the artifact JSON from.

        Returns:
            The loaded + validated :class:`NormalizerArtifact`.
        """
        p = Path(path)
        data = p.read_text(encoding="utf-8")
        return NormalizerArtifact.model_validate_json(data)
