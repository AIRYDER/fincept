"""Tests for ``fincept_core.datasets.schema_compat`` (feature schema versioning).

Covers the compatibility rules for loading a model artifact against a
live feature snapshot:

  * Same version + same hash = compatible.
  * Version mismatch = incompatible with ``"version_mismatch"`` code.
  * Missing features = incompatible with ``"missing_features"`` code.
  * Extra features with ``allow_extra_features=True`` = compatible.
  * Extra features with ``allow_extra_features=False`` = incompatible.
  * ``assert_feature_schema_compatible`` raises on incompatibility.
  * Same version + different hash + all features present = compatible.
  * ``SchemaIncompatibilityError`` exposes a stable ``code`` attribute.
"""

from __future__ import annotations

import pytest

from fincept_core.datasets import (
    SchemaCompatResult,
    SchemaIncompatibilityError,
    assert_feature_schema_compatible,
    check_feature_schema_compatibility,
)

# Canonical 64-char lowercase hex SHA-256 used across tests.
_HEX256 = "a" * 64
_HEX256_B = "b" * 64

_FEATURES_A = ("mom_z_60m", "mom_z_240m", "vol_z_60m")
_FEATURES_A_EXTRA = ("mom_z_60m", "mom_z_240m", "vol_z_60m", "rsi_14")
_FEATURES_A_MISSING = ("mom_z_60m", "mom_z_240m")


def _check(**overrides: object) -> SchemaCompatResult:
    """Run a compatibility check with sane defaults overridden by ``overrides``."""
    defaults: dict[str, object] = {
        "artifact_feature_schema_hash": _HEX256,
        "artifact_feature_schema_version": 1,
        "artifact_feature_names": _FEATURES_A,
        "snapshot_feature_schema_hash": _HEX256,
        "snapshot_feature_schema_version": 1,
        "snapshot_feature_names": _FEATURES_A,
        "allow_extra_features": True,
    }
    defaults.update(overrides)
    return check_feature_schema_compatibility(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Compatibility checks                                                         #
# --------------------------------------------------------------------------- #


def test_same_version_same_hash_is_compatible() -> None:
    """Rule 2: exact hash match is fully compatible."""
    result = _check()
    assert result.compatible is True
    assert result.error is None


def test_version_mismatch_is_incompatible() -> None:
    """Rule 1: version mismatch is a hard failure with ``version_mismatch``."""
    result = _check(
        artifact_feature_schema_version=1,
        snapshot_feature_schema_version=2,
    )
    assert result.compatible is False
    assert result.error is not None
    assert result.error.code == "version_mismatch"
    assert "version" in str(result.error)


def test_missing_features_is_incompatible() -> None:
    """Rule 3: snapshot missing an artifact feature is a hard failure."""
    result = _check(
        snapshot_feature_schema_hash=_HEX256_B,
        snapshot_feature_names=_FEATURES_A_MISSING,
    )
    assert result.compatible is False
    assert result.error is not None
    assert result.error.code == "missing_features"
    assert "vol_z_60m" in str(result.error)


def test_extra_features_allowed_by_default() -> None:
    """Rule 4: extra snapshot features are compatible when allowed."""
    result = _check(
        snapshot_feature_schema_hash=_HEX256_B,
        snapshot_feature_names=_FEATURES_A_EXTRA,
        allow_extra_features=True,
    )
    assert result.compatible is True
    assert result.error is None


def test_extra_features_rejected_when_disallowed() -> None:
    """Rule 4: extra snapshot features are incompatible when disallowed."""
    result = _check(
        snapshot_feature_schema_hash=_HEX256_B,
        snapshot_feature_names=_FEATURES_A_EXTRA,
        allow_extra_features=False,
    )
    assert result.compatible is False
    assert result.error is not None
    assert result.error.code == "extra_features"
    assert "rsi_14" in str(result.error)


def test_assert_raises_on_incompatibility() -> None:
    """``assert_feature_schema_compatible`` raises on a version mismatch."""
    with pytest.raises(SchemaIncompatibilityError, match="version") as exc_info:
        assert_feature_schema_compatible(
            artifact_feature_schema_hash=_HEX256,
            artifact_feature_schema_version=1,
            artifact_feature_names=_FEATURES_A,
            snapshot_feature_schema_hash=_HEX256,
            snapshot_feature_schema_version=2,
            snapshot_feature_names=_FEATURES_A,
        )
    assert exc_info.value.code == "version_mismatch"


def test_assert_passes_on_compatibility() -> None:
    """``assert_feature_schema_compatible`` does not raise when compatible."""
    assert_feature_schema_compatible(
        artifact_feature_schema_hash=_HEX256,
        artifact_feature_schema_version=1,
        artifact_feature_names=_FEATURES_A,
        snapshot_feature_schema_hash=_HEX256,
        snapshot_feature_schema_version=1,
        snapshot_feature_names=_FEATURES_A,
    )


def test_same_version_different_hash_all_features_present() -> None:
    """Rule 3 (positive): hashes differ but all artifact features present."""
    result = _check(
        snapshot_feature_schema_hash=_HEX256_B,
        snapshot_feature_names=_FEATURES_A_EXTRA,
    )
    assert result.compatible is True
    assert result.error is None


def test_incompatibility_error_has_code_attribute() -> None:
    """``SchemaIncompatibilityError`` exposes a stable ``code`` string."""
    err = SchemaIncompatibilityError("version_mismatch", "boom")
    assert err.code == "version_mismatch"
    assert str(err) == "boom"
    assert isinstance(err, ValueError)
