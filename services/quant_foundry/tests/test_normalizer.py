"""Tests for quant_foundry.normalizer (T-9.2).

Covers:
- NormalizationMethod and MissingPolicy enums
- ColumnNormalizerStats construction + validation
- NormalizerArtifact construction + hash computation
- Normalizer.fit / transform / fit_transform with synthetic data
- all normalization methods (standard, robust, minmax, none)
- all missing policies (fail, mean_fill, median_fill, zero_fill)
- artifact save/load round-trip
- validate_normalizer_present (required + optional)
- merge_fold_normalizers
- compute_normalizer_hash determinism
- fail-closed: missing required normalizer
- edge cases: empty data, single column, all missing values
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from quant_foundry.normalizer import (
    ColumnNormalizerStats,
    MissingPolicy,
    NormalizationMethod,
    Normalizer,
    NormalizerArtifact,
    apply_missing_policy,
    apply_normalization,
    compute_normalizer_hash,
    merge_fold_normalizers,
    validate_normalizer_present,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_df() -> pd.DataFrame:
    """A small synthetic DataFrame with two numeric columns + NaNs."""
    return pd.DataFrame(
        {
            "a": [1.0, 2.0, 3.0, 4.0, 5.0, np.nan],
            "b": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        }
    )


@pytest.fixture
def clean_df() -> pd.DataFrame:
    """A small synthetic DataFrame with no missing values."""
    return pd.DataFrame(
        {
            "x": [1.0, 2.0, 3.0, 4.0, 5.0],
            "y": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    )


def _make_stats(
    column_name: str = "a",
    method: NormalizationMethod = NormalizationMethod.STANDARD,
    missing_policy: MissingPolicy = MissingPolicy.MEAN_FILL,
    mean: float | None = 3.0,
    std: float | None = 1.5,
    median: float | None = None,
    iqr: float | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
    missing_fill_value: float | None = 3.0,
    n_samples: int = 5,
    n_missing: int = 0,
) -> ColumnNormalizerStats:
    """Helper to build a ColumnNormalizerStats with sensible defaults."""
    return ColumnNormalizerStats(
        column_name=column_name,
        method=method,
        mean=mean,
        std=std,
        median=median,
        iqr=iqr,
        min_val=min_val,
        max_val=max_val,
        missing_policy=missing_policy,
        missing_fill_value=missing_fill_value,
        n_samples=n_samples,
        n_missing=n_missing,
    )


def _make_artifact(
    columns: list[ColumnNormalizerStats] | None = None,
    artifact_id: str = "test-artifact",
    fold_id: int | None = None,
) -> NormalizerArtifact:
    """Helper to build a NormalizerArtifact with a valid hash."""
    if columns is None:
        columns = [_make_stats()]
    normalizer_hash = compute_normalizer_hash(columns)
    return NormalizerArtifact(
        artifact_id=artifact_id,
        columns=columns,
        normalizer_hash=normalizer_hash,
        created_at="2026-01-01T00:00:00+00:00",
        fold_id=fold_id,
    )


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestNormalizationMethodEnum:
    def test_standard_value(self) -> None:
        assert NormalizationMethod.STANDARD.value == "standard"

    def test_robust_value(self) -> None:
        assert NormalizationMethod.ROBUST.value == "robust"

    def test_minmax_value(self) -> None:
        assert NormalizationMethod.MINMAX.value == "minmax"

    def test_none_value(self) -> None:
        assert NormalizationMethod.NONE.value == "none"

    def test_all_members_count(self) -> None:
        assert len(NormalizationMethod) == 4

    def test_from_string(self) -> None:
        assert NormalizationMethod("standard") is NormalizationMethod.STANDARD
        assert NormalizationMethod("robust") is NormalizationMethod.ROBUST
        assert NormalizationMethod("minmax") is NormalizationMethod.MINMAX
        assert NormalizationMethod("none") is NormalizationMethod.NONE

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            NormalizationMethod("bogus")


class TestMissingPolicyEnum:
    def test_fail_value(self) -> None:
        assert MissingPolicy.FAIL.value == "fail"

    def test_mean_fill_value(self) -> None:
        assert MissingPolicy.MEAN_FILL.value == "mean_fill"

    def test_median_fill_value(self) -> None:
        assert MissingPolicy.MEDIAN_FILL.value == "median_fill"

    def test_zero_fill_value(self) -> None:
        assert MissingPolicy.ZERO_FILL.value == "zero_fill"

    def test_all_members_count(self) -> None:
        assert len(MissingPolicy) == 4

    def test_from_string(self) -> None:
        assert MissingPolicy("fail") is MissingPolicy.FAIL
        assert MissingPolicy("mean_fill") is MissingPolicy.MEAN_FILL
        assert MissingPolicy("median_fill") is MissingPolicy.MEDIAN_FILL
        assert MissingPolicy("zero_fill") is MissingPolicy.ZERO_FILL

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            MissingPolicy("bogus")


# ---------------------------------------------------------------------------
# ColumnNormalizerStats tests
# ---------------------------------------------------------------------------


class TestColumnNormalizerStats:
    def test_basic_construction(self) -> None:
        stats = _make_stats()
        assert stats.column_name == "a"
        assert stats.method is NormalizationMethod.STANDARD
        assert stats.mean == 3.0
        assert stats.std == 1.5
        assert stats.n_samples == 5
        assert stats.n_missing == 0

    def test_frozen(self) -> None:
        stats = _make_stats()
        with pytest.raises(ValidationError):
            stats.mean = 99.0  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        # Baseline construction succeeds
        _make_stats()
        # Build directly with an extra field is rejected
        with pytest.raises(ValidationError):
            ColumnNormalizerStats(
                column_name="a",
                method=NormalizationMethod.STANDARD,
                mean=1.0,
                std=1.0,
                missing_policy=MissingPolicy.MEAN_FILL,
                missing_fill_value=1.0,
                n_samples=5,
                n_missing=0,
                bogus_field=123,  # type: ignore[call-arg]
            )

    def test_negative_n_samples_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_stats(n_samples=-1)

    def test_negative_n_missing_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_stats(n_missing=-1)

    def test_n_missing_exceeds_n_samples_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_stats(n_samples=3, n_missing=4)

    def test_empty_column_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_stats(column_name="")

    def test_all_optional_fields_default_none(self) -> None:
        stats = ColumnNormalizerStats(
            column_name="a",
            method=NormalizationMethod.NONE,
            missing_policy=MissingPolicy.FAIL,
            n_samples=5,
            n_missing=0,
        )
        assert stats.mean is None
        assert stats.std is None
        assert stats.median is None
        assert stats.iqr is None
        assert stats.min_val is None
        assert stats.max_val is None
        assert stats.missing_fill_value is None

    def test_robust_stats(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.ROBUST,
            mean=None,
            std=None,
            median=3.0,
            iqr=2.0,
            missing_policy=MissingPolicy.MEDIAN_FILL,
            missing_fill_value=3.0,
        )
        assert stats.median == 3.0
        assert stats.iqr == 2.0

    def test_minmax_stats(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.MINMAX,
            mean=None,
            std=None,
            min_val=1.0,
            max_val=5.0,
            missing_policy=MissingPolicy.ZERO_FILL,
            missing_fill_value=0.0,
        )
        assert stats.min_val == 1.0
        assert stats.max_val == 5.0


# ---------------------------------------------------------------------------
# NormalizerArtifact + hash tests
# ---------------------------------------------------------------------------


class TestNormalizerArtifact:
    def test_basic_construction(self) -> None:
        art = _make_artifact()
        assert art.artifact_id == "test-artifact"
        assert len(art.columns) == 1
        assert art.fold_id is None

    def test_frozen(self) -> None:
        art = _make_artifact()
        with pytest.raises(ValidationError):
            art.artifact_id = "other"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            NormalizerArtifact(
                artifact_id="x",
                columns=[_make_stats()],
                normalizer_hash=compute_normalizer_hash([_make_stats()]),
                created_at="2026-01-01T00:00:00+00:00",
                bogus=123,  # type: ignore[call-arg]
            )

    def test_duplicate_columns_rejected(self) -> None:
        cols = [_make_stats(column_name="a"), _make_stats(column_name="a")]
        with pytest.raises(ValidationError):
            _make_artifact(columns=cols)

    def test_hash_mismatch_rejected(self) -> None:
        cols = [_make_stats()]
        with pytest.raises(ValidationError):
            NormalizerArtifact(
                artifact_id="x",
                columns=cols,
                normalizer_hash="0" * 64,
                created_at="2026-01-01T00:00:00+00:00",
            )

    def test_empty_artifact_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizerArtifact(
                artifact_id="",
                columns=[_make_stats()],
                normalizer_hash=compute_normalizer_hash([_make_stats()]),
                created_at="2026-01-01T00:00:00+00:00",
            )

    def test_fold_id_supported(self) -> None:
        art = _make_artifact(fold_id=2)
        assert art.fold_id == 2

    def test_empty_columns_allowed(self) -> None:
        art = NormalizerArtifact(
            artifact_id="empty",
            columns=[],
            normalizer_hash=compute_normalizer_hash([]),
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert art.columns == []


class TestComputeNormalizerHash:
    def test_deterministic(self) -> None:
        cols = [_make_stats(), _make_stats(column_name="b")]
        h1 = compute_normalizer_hash(cols)
        h2 = compute_normalizer_hash(cols)
        assert h1 == h2

    def test_is_sha256_hex(self) -> None:
        h = compute_normalizer_hash([_make_stats()])
        assert len(h) == 64
        int(h, 16)  # valid hex

    def test_different_stats_different_hash(self) -> None:
        c1 = _make_stats(mean=1.0)
        c2 = _make_stats(mean=2.0)
        assert compute_normalizer_hash([c1]) != compute_normalizer_hash([c2])

    def test_different_column_order_different_hash(self) -> None:
        a = _make_stats(column_name="a")
        b = _make_stats(column_name="b", mean=10.0, missing_fill_value=10.0)
        assert compute_normalizer_hash([a, b]) != compute_normalizer_hash([b, a])

    def test_empty_list_hash(self) -> None:
        h = compute_normalizer_hash([])
        assert len(h) == 64


# ---------------------------------------------------------------------------
# apply_normalization tests
# ---------------------------------------------------------------------------


class TestApplyNormalization:
    def test_standard(self) -> None:
        stats = _make_stats(method=NormalizationMethod.STANDARD, mean=2.0, std=2.0)
        out = apply_normalization(np.array([0.0, 2.0, 4.0]), stats)
        np.testing.assert_allclose(out, [-1.0, 0.0, 1.0])

    def test_robust(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.ROBUST,
            mean=None,
            std=None,
            median=3.0,
            iqr=2.0,
            missing_policy=MissingPolicy.MEDIAN_FILL,
            missing_fill_value=3.0,
        )
        out = apply_normalization(np.array([1.0, 3.0, 5.0]), stats)
        np.testing.assert_allclose(out, [-1.0, 0.0, 1.0])

    def test_minmax(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.MINMAX,
            mean=None,
            std=None,
            min_val=0.0,
            max_val=10.0,
            missing_policy=MissingPolicy.ZERO_FILL,
            missing_fill_value=0.0,
        )
        out = apply_normalization(np.array([0.0, 5.0, 10.0]), stats)
        np.testing.assert_allclose(out, [0.0, 0.5, 1.0])

    def test_none_passthrough(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.NONE,
            mean=None,
            std=None,
            missing_policy=MissingPolicy.FAIL,
            missing_fill_value=None,
        )
        arr = np.array([1.0, 2.0, 3.0])
        out = apply_normalization(arr, stats)
        np.testing.assert_array_equal(out, arr)

    def test_standard_zero_std_raises(self) -> None:
        stats = _make_stats(method=NormalizationMethod.STANDARD, mean=1.0, std=0.0)
        with pytest.raises(ValueError, match="non-zero std"):
            apply_normalization(np.array([1.0, 2.0]), stats)

    def test_robust_zero_iqr_raises(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.ROBUST,
            mean=None,
            std=None,
            median=1.0,
            iqr=0.0,
            missing_policy=MissingPolicy.MEDIAN_FILL,
            missing_fill_value=1.0,
        )
        with pytest.raises(ValueError, match="non-zero iqr"):
            apply_normalization(np.array([1.0, 2.0]), stats)

    def test_minmax_zero_range_raises(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.MINMAX,
            mean=None,
            std=None,
            min_val=5.0,
            max_val=5.0,
            missing_policy=MissingPolicy.ZERO_FILL,
            missing_fill_value=0.0,
        )
        with pytest.raises(ValueError, match="non-zero range"):
            apply_normalization(np.array([1.0, 2.0]), stats)

    def test_standard_missing_mean_raises(self) -> None:
        stats = _make_stats(method=NormalizationMethod.STANDARD, mean=None, std=1.0)
        with pytest.raises(ValueError, match="requires mean and std"):
            apply_normalization(np.array([1.0]), stats)


# ---------------------------------------------------------------------------
# apply_missing_policy tests
# ---------------------------------------------------------------------------


class TestApplyMissingPolicy:
    def test_fail_no_nan_ok(self) -> None:
        stats = _make_stats(missing_policy=MissingPolicy.FAIL, missing_fill_value=None)
        out = apply_missing_policy(np.array([1.0, 2.0]), stats)
        np.testing.assert_array_equal(out, [1.0, 2.0])

    def test_fail_with_nan_raises(self) -> None:
        stats = _make_stats(missing_policy=MissingPolicy.FAIL, missing_fill_value=None)
        with pytest.raises(ValueError, match="FAIL"):
            apply_missing_policy(np.array([1.0, np.nan]), stats)

    def test_mean_fill(self) -> None:
        stats = _make_stats(missing_policy=MissingPolicy.MEAN_FILL, missing_fill_value=3.0)
        out = apply_missing_policy(np.array([1.0, np.nan, 5.0]), stats)
        np.testing.assert_allclose(out, [1.0, 3.0, 5.0])

    def test_median_fill(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.ROBUST,
            mean=None,
            std=None,
            median=3.0,
            iqr=2.0,
            missing_policy=MissingPolicy.MEDIAN_FILL,
            missing_fill_value=3.0,
        )
        out = apply_missing_policy(np.array([1.0, np.nan, 5.0]), stats)
        np.testing.assert_allclose(out, [1.0, 3.0, 5.0])

    def test_zero_fill(self) -> None:
        stats = _make_stats(
            method=NormalizationMethod.MINMAX,
            mean=None,
            std=None,
            min_val=1.0,
            max_val=5.0,
            missing_policy=MissingPolicy.ZERO_FILL,
            missing_fill_value=0.0,
        )
        out = apply_missing_policy(np.array([1.0, np.nan, 5.0]), stats)
        np.testing.assert_allclose(out, [1.0, 0.0, 5.0])

    def test_mean_fill_falls_back_to_stats_mean(self) -> None:
        stats = _make_stats(
            missing_policy=MissingPolicy.MEAN_FILL, missing_fill_value=None, mean=7.0
        )
        out = apply_missing_policy(np.array([1.0, np.nan]), stats)
        np.testing.assert_allclose(out, [1.0, 7.0])

    def test_no_nan_returns_unchanged(self) -> None:
        stats = _make_stats(missing_policy=MissingPolicy.MEAN_FILL, missing_fill_value=3.0)
        arr = np.array([1.0, 2.0, 3.0])
        out = apply_missing_policy(arr, stats)
        np.testing.assert_array_equal(out, arr)


# ---------------------------------------------------------------------------
# Normalizer fit / transform / fit_transform tests
# ---------------------------------------------------------------------------


class TestNormalizerFitTransform:
    def test_fit_returns_artifact(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer()
        art = norm.fit(clean_df, ["x", "y"])
        assert isinstance(art, NormalizerArtifact)
        assert {c.column_name for c in art.columns} == {"x", "y"}
        assert norm.artifact_ is art

    def test_fit_empty_columns_raises(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer()
        with pytest.raises(ValueError, match="at least one column"):
            norm.fit(clean_df, [])

    def test_fit_missing_column_raises(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer()
        with pytest.raises(ValueError, match="not found"):
            norm.fit(clean_df, ["zzz"])

    def test_transform_without_fit_raises(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer()
        with pytest.raises(ValueError, match="must be fit"):
            norm.transform(clean_df, ["x"])

    def test_transform_unknown_column_raises(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer()
        norm.fit(clean_df, ["x"])
        with pytest.raises(ValueError, match="not found in normalizer"):
            norm.transform(clean_df, ["y"])

    def test_fit_transform_standard(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        out, art = norm.fit_transform(clean_df, ["x"])
        # x = [1,2,3,4,5], mean=3, std~1.4142 (population)
        np.testing.assert_allclose(
            out["x"].to_numpy(), (clean_df["x"].to_numpy() - 3.0) / np.std(clean_df["x"].to_numpy())
        )

    def test_fit_transform_minmax(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.MINMAX, missing_policy=MissingPolicy.FAIL)
        out, _ = norm.fit_transform(clean_df, ["x"])
        np.testing.assert_allclose(out["x"].to_numpy(), [0.0, 0.25, 0.5, 0.75, 1.0])

    def test_fit_transform_robust(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.ROBUST, missing_policy=MissingPolicy.FAIL)
        out, _ = norm.fit_transform(clean_df, ["x"])
        median = 3.0
        q75, q25 = np.percentile(clean_df["x"].to_numpy(), [75, 25])
        iqr = q75 - q25
        np.testing.assert_allclose(out["x"].to_numpy(), (clean_df["x"].to_numpy() - median) / iqr)

    def test_fit_transform_none(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.NONE, missing_policy=MissingPolicy.FAIL)
        out, _ = norm.fit_transform(clean_df, ["x"])
        np.testing.assert_array_equal(out["x"].to_numpy(), clean_df["x"].to_numpy())

    def test_transform_does_not_mutate_input(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        original = clean_df["x"].to_numpy().copy()
        norm.fit_transform(clean_df, ["x"])
        np.testing.assert_array_equal(clean_df["x"].to_numpy(), original)

    def test_mean_fill_during_transform(self, sample_df: pd.DataFrame) -> None:
        norm = Normalizer(
            method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.MEAN_FILL
        )
        out, _ = norm.fit_transform(sample_df, ["a"])
        # No NaN should remain
        assert not out["a"].isna().any()

    def test_zero_fill_during_transform(self, sample_df: pd.DataFrame) -> None:
        norm = Normalizer(
            method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.ZERO_FILL
        )
        out, _ = norm.fit_transform(sample_df, ["a"])
        assert not out["a"].isna().any()

    def test_fail_policy_during_transform_raises(self, sample_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        with pytest.raises(ValueError, match="FAIL"):
            norm.fit_transform(sample_df, ["a"])

    def test_n_missing_recorded(self, sample_df: pd.DataFrame) -> None:
        norm = Normalizer(
            method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.MEAN_FILL
        )
        art = norm.fit(sample_df, ["a"])
        col = next(c for c in art.columns if c.column_name == "a")
        assert col.n_missing == 1
        assert col.n_samples == 6


# ---------------------------------------------------------------------------
# Save / load round-trip tests
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_save_load_roundtrip(self, clean_df: pd.DataFrame, tmp_path: Path) -> None:
        norm = Normalizer()
        art = norm.fit(clean_df, ["x", "y"])
        path = tmp_path / "normalizer.json"
        norm.save_artifact(str(path))
        assert path.exists()
        loaded = Normalizer.load_artifact(str(path))
        assert loaded == art

    def test_save_without_fit_raises(self, tmp_path: Path) -> None:
        norm = Normalizer()
        with pytest.raises(ValueError, match="must be fit"):
            norm.save_artifact(str(tmp_path / "x.json"))

    def test_save_creates_parent_dirs(self, clean_df: pd.DataFrame, tmp_path: Path) -> None:
        norm = Normalizer()
        norm.fit(clean_df, ["x"])
        path = tmp_path / "nested" / "dir" / "normalizer.json"
        norm.save_artifact(str(path))
        assert path.exists()

    def test_load_validates_hash(self, clean_df: pd.DataFrame, tmp_path: Path) -> None:
        norm = Normalizer()
        norm.fit(clean_df, ["x"])
        path = tmp_path / "normalizer.json"
        norm.save_artifact(str(path))
        # Tamper with the hash
        data = json.loads(path.read_text(encoding="utf-8"))
        data["normalizer_hash"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValidationError):
            Normalizer.load_artifact(str(path))

    def test_load_returns_correct_columns(self, clean_df: pd.DataFrame, tmp_path: Path) -> None:
        norm = Normalizer()
        norm.fit(clean_df, ["x", "y"])
        path = tmp_path / "normalizer.json"
        norm.save_artifact(str(path))
        loaded = Normalizer.load_artifact(str(path))
        assert {c.column_name for c in loaded.columns} == {"x", "y"}


# ---------------------------------------------------------------------------
# validate_normalizer_present tests
# ---------------------------------------------------------------------------


class TestValidateNormalizerPresent:
    def test_required_none_raises(self) -> None:
        with pytest.raises(ValueError, match="inference requires normalizer"):
            validate_normalizer_present(None, required=True)

    def test_optional_none_returns_true(self) -> None:
        assert validate_normalizer_present(None, required=False) is True

    def test_present_returns_true(self) -> None:
        art = _make_artifact()
        assert validate_normalizer_present(art, required=True) is True
        assert validate_normalizer_present(art, required=False) is True

    def test_default_required(self) -> None:
        with pytest.raises(ValueError):
            validate_normalizer_present(None)


# ---------------------------------------------------------------------------
# merge_fold_normalizers tests
# ---------------------------------------------------------------------------


class TestMergeFoldNormalizers:
    def test_merge_single_artifact(self) -> None:
        art = _make_artifact(fold_id=0)
        merged = merge_fold_normalizers([art])
        assert merged.fold_id is None
        assert merged.artifact_id.endswith("::merged")
        assert {c.column_name for c in merged.columns} == {"a"}

    def test_merge_multiple_artifacts(self) -> None:
        arts = [
            _make_artifact(
                columns=[_make_stats("a"), _make_stats("b", mean=10.0, missing_fill_value=10.0)],
                fold_id=i,
            )
            for i in range(3)
        ]
        merged = merge_fold_normalizers(arts)
        assert merged.fold_id is None
        assert len(merged.columns) == 2

    def test_merge_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            merge_fold_normalizers([])

    def test_merge_mismatched_columns_raises(self) -> None:
        a1 = _make_artifact(columns=[_make_stats("a")], fold_id=0)
        a2 = _make_artifact(
            columns=[_make_stats("b", mean=10.0, missing_fill_value=10.0)], fold_id=1
        )
        with pytest.raises(ValueError, match="same set of column names"):
            merge_fold_normalizers([a1, a2])

    def test_merge_hash_is_valid(self) -> None:
        arts = [_make_artifact(fold_id=i) for i in range(2)]
        merged = merge_fold_normalizers(arts)
        # Hash should match recomputed
        assert merged.normalizer_hash == compute_normalizer_hash(merged.columns)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_dataframe(self) -> None:
        df = pd.DataFrame({"a": pd.Series([], dtype=float)})
        norm = Normalizer(method=NormalizationMethod.NONE, missing_policy=MissingPolicy.FAIL)
        art = norm.fit(df, ["a"])
        col = art.columns[0]
        assert col.n_samples == 0
        assert col.n_missing == 0
        assert col.mean is None

    def test_single_column(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        out, art = norm.fit_transform(clean_df, ["x"])
        assert len(art.columns) == 1
        assert art.columns[0].column_name == "x"
        assert not out["x"].isna().any()

    def test_all_missing_values(self) -> None:
        df = pd.DataFrame({"a": [np.nan, np.nan, np.nan]})
        norm = Normalizer(method=NormalizationMethod.NONE, missing_policy=MissingPolicy.ZERO_FILL)
        out, art = norm.fit_transform(df, ["a"])
        col = art.columns[0]
        assert col.n_samples == 3
        assert col.n_missing == 3
        assert col.mean is None
        np.testing.assert_array_equal(out["a"].to_numpy(), [0.0, 0.0, 0.0])

    def test_all_missing_with_mean_fill_falls_back_to_zero(self) -> None:
        df = pd.DataFrame({"a": [np.nan, np.nan]})
        norm = Normalizer(method=NormalizationMethod.NONE, missing_policy=MissingPolicy.MEAN_FILL)
        out, art = norm.fit_transform(df, ["a"])
        # mean is None -> falls back to 0.0
        assert not out["a"].isna().any()

    def test_constant_column_standard_raises_on_transform(self) -> None:
        df = pd.DataFrame({"a": [5.0, 5.0, 5.0]})
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        with pytest.raises(ValueError, match="non-zero std"):
            norm.fit_transform(df, ["a"])

    def test_constant_column_minmax_raises_on_transform(self) -> None:
        df = pd.DataFrame({"a": [5.0, 5.0, 5.0]})
        norm = Normalizer(method=NormalizationMethod.MINMAX, missing_policy=MissingPolicy.FAIL)
        with pytest.raises(ValueError, match="non-zero range"):
            norm.fit_transform(df, ["a"])

    def test_transform_preserves_other_columns(self, clean_df: pd.DataFrame) -> None:
        norm = Normalizer(method=NormalizationMethod.STANDARD, missing_policy=MissingPolicy.FAIL)
        out, _ = norm.fit_transform(clean_df, ["x"])
        # y should be unchanged
        np.testing.assert_array_equal(out["y"].to_numpy(), clean_df["y"].to_numpy())
