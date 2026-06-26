"""Tests for ``fincept_core.datasets.schemas`` (ML dataset evidence spine).

Covers the QA scenarios from todo 2 of the ``ml-dataset-evidence-spine``
plan:
  * Happy path: round-trip serialize/deserialize for each of the three
    manifest classes (``DatasetManifest``, ``ArtifactManifest``,
    ``FeatureSnapshot``).
  * Failure path: extra key (``extra="forbid"``) raises ``ValidationError``;
    a non-hex ``feature_schema_hash`` raises; a negative ``as_of_ts``
    raises; a missing required field raises.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from fincept_core.datasets.schemas import (
    ArtifactManifest,
    DatasetManifest,
    FeatureRow,
    FeatureSnapshot,
)

# A canonical 64-char lowercase hex SHA-256 used across tests.
_HEX256 = "a" * 64
_HEX256_B = "b" * 64
_HEX256_C = "c" * 64


# ---------------------------------------------------------------------------
# Happy path: round-trip serialize/deserialize.
# ---------------------------------------------------------------------------


def test_dataset_manifest_round_trip() -> None:
    payload = {
        "schema_version": 1,
        "dataset_id": "ds_train_btc_2024",
        "feature_schema_hash": _HEX256,
        "label_schema_hash": _HEX256_B,
        "as_of_ts": 1_700_000_000_000_000_000,
        "universe_hash": _HEX256_C,
        "row_count": 12345,
        "source_vintage_refs": ["vintage/2024-01-01", "vintage/2024-02-01"],
    }
    obj = DatasetManifest.model_validate(payload)
    assert obj.dataset_id == "ds_train_btc_2024"
    # hex hashes are normalised to lowercase.
    assert obj.feature_schema_hash == _HEX256
    as_dict = obj.model_dump()
    obj2 = DatasetManifest.model_validate(as_dict)
    assert obj2 == obj
    # to_dict / from_dict via pydantic primitives is exact.
    assert obj2.model_dump() == as_dict


def test_artifact_manifest_round_trip() -> None:
    payload = {
        "schema_version": 1,
        "artifact_id": "art_xgb_btc_v1",
        "sha256": _HEX256,
        "size_bytes": 4096,
        "uri": "s3://bucket/artifacts/art_xgb_btc_v1.bin",
        "model_family": "xgboost",
        "created_at_ns": 1_700_000_000_000_000_000,
        "feature_schema_hash": _HEX256_B,
        "label_schema_hash": _HEX256_C,
        "code_git_sha": "deadbeef",
        "lockfile_hash": None,
        "container_image_digest": None,
    }
    obj = ArtifactManifest.model_validate(payload)
    assert obj.artifact_id == "art_xgb_btc_v1"
    assert obj.sha256 == _HEX256
    as_dict = obj.model_dump()
    obj2 = ArtifactManifest.model_validate(as_dict)
    assert obj2 == obj
    assert obj2.model_dump() == as_dict


def test_feature_snapshot_round_trip() -> None:
    payload = {
        "schema_version": 1,
        "decision_time_ns": 1_700_000_000_000_000_000,
        "rows": [
            {
                "schema_version": 1,
                "symbol": "BTCUSD",
                "ts": 1_699_999_999_000_000_000,
                "features": {"ret_1d": 0.0123, "vol_20d": 0.45},
            },
            {
                "symbol": "ETHUSD",
                "ts": 1_699_999_999_500_000_000,
                "features": {"ret_1d": -0.0042},
            },
        ],
        "feature_schema_hash": _HEX256,
    }
    obj = FeatureSnapshot.model_validate(payload)
    assert obj.decision_time_ns == 1_700_000_000_000_000_000
    assert len(obj.rows) == 2
    assert isinstance(obj.rows[0], FeatureRow)
    assert obj.rows[0].features["ret_1d"] == 0.0123
    as_dict = obj.model_dump()
    obj2 = FeatureSnapshot.model_validate(as_dict)
    assert obj2 == obj
    assert obj2.model_dump() == as_dict


# ---------------------------------------------------------------------------
# Failure path.
# ---------------------------------------------------------------------------


def test_extra_key_rejected() -> None:
    payload = {
        "dataset_id": "ds_x",
        "feature_schema_hash": _HEX256,
        "label_schema_hash": _HEX256_B,
        "as_of_ts": 1,
        "universe_hash": _HEX256_C,
        "row_count": 1,
        "extra_unexpected_field": "boom",
    }
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(payload)


def test_bad_feature_schema_hash_rejected() -> None:
    payload = {
        "dataset_id": "ds_x",
        "feature_schema_hash": "not-a-hex-string",
        "label_schema_hash": _HEX256_B,
        "as_of_ts": 1,
        "universe_hash": _HEX256_C,
        "row_count": 1,
    }
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(payload)


def test_negative_as_of_ts_rejected() -> None:
    payload = {
        "dataset_id": "ds_x",
        "feature_schema_hash": _HEX256,
        "label_schema_hash": _HEX256_B,
        "as_of_ts": -1,
        "universe_hash": _HEX256_C,
        "row_count": 1,
    }
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(payload)


def test_missing_required_field_rejected() -> None:
    payload = {
        "dataset_id": "ds_x",
        # feature_schema_hash omitted on purpose.
        "label_schema_hash": _HEX256_B,
        "as_of_ts": 1,
        "universe_hash": _HEX256_C,
        "row_count": 1,
    }
    with pytest.raises(ValidationError):
        DatasetManifest.model_validate(payload)


def test_feature_snapshot_lookahead_rejected() -> None:
    """A row whose ``ts`` exceeds ``decision_time_ns`` is a look-ahead leak."""
    payload = {
        "decision_time_ns": 1_000,
        "rows": [
            {"symbol": "BTCUSD", "ts": 2_000, "features": {}},
        ],
        "feature_schema_hash": _HEX256,
    }
    with pytest.raises(ValidationError):
        FeatureSnapshot.model_validate(payload)


def test_dataset_manifest_is_frozen() -> None:
    """Frozen models must reject attribute mutation."""
    obj = DatasetManifest.model_validate(
        {
            "dataset_id": "ds_x",
            "feature_schema_hash": _HEX256,
            "label_schema_hash": _HEX256_B,
            "as_of_ts": 1,
            "universe_hash": _HEX256_C,
            "row_count": 1,
        }
    )
    with pytest.raises(ValidationError):
        obj.dataset_id = "mutated"  # type: ignore[misc]


def test_deep_copy_preserves_round_trip() -> None:
    """A deep copy of a manifest must still round-trip identically."""
    payload = {
        "dataset_id": "ds_x",
        "feature_schema_hash": _HEX256,
        "label_schema_hash": _HEX256_B,
        "as_of_ts": 42,
        "universe_hash": _HEX256_C,
        "row_count": 7,
        "source_vintage_refs": ["a", "b"],
    }
    obj = DatasetManifest.model_validate(payload)
    clone = copy.deepcopy(obj)
    assert clone == obj
    assert clone.model_dump() == obj.model_dump()
