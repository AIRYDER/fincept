"""Feature schema compatibility checking.

When a model artifact is loaded for inference, the feature schema it was
trained against must be compatible with the feature schema of the live
feature snapshot.  This module enforces that check loudly -- a mismatch
is a hard failure, not a silent degradation.

Compatibility rules:

  1. Same ``feature_schema_version`` (the pipeline generation must match).
  2. Same ``feature_schema_hash`` *or* a declared compatibility mapping.
  3. All features the artifact was trained on must be present in the
     snapshot (extra snapshot features are allowed by default).

A schema version bump means "the feature pipeline changed in a way that
invalidates models trained on the previous version."  The version is
orthogonal to the hash -- the hash identifies the exact feature set,
the version identifies the pipeline generation.
"""

from __future__ import annotations

from dataclasses import dataclass


class SchemaIncompatibilityError(ValueError):
    """A feature schema mismatch was detected during artifact loading.

    ``code`` is a stable string: ``"version_mismatch"``,
    ``"missing_features"``, ``"hash_mismatch"``, or
    ``"extra_features"``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SchemaCompatResult:
    """Result of a compatibility check."""

    compatible: bool
    error: SchemaIncompatibilityError | None = None


def check_feature_schema_compatibility(
    *,
    artifact_feature_schema_hash: str,
    artifact_feature_schema_version: int,
    artifact_feature_names: tuple[str, ...] | list[str],
    snapshot_feature_schema_hash: str,
    snapshot_feature_schema_version: int,
    snapshot_feature_names: tuple[str, ...] | list[str],
    allow_extra_features: bool = True,
) -> SchemaCompatResult:
    """Check if a snapshot's feature schema is compatible with an artifact's.

    Rules:

    1. Version must match exactly.  A version mismatch is a hard failure.
    2. If hashes match, fully compatible (same feature set).
    3. If hashes differ, check that all artifact features are present in
       the snapshot.  Extra features in the snapshot are allowed by
       default (the artifact just ignores them) but can be rejected.
    4. Missing features (artifact has a feature the snapshot doesn't) is
       always a hard failure.
    """
    artifact_features = set(artifact_feature_names)
    snapshot_features = set(snapshot_feature_names)

    # Rule 1: version must match.
    if artifact_feature_schema_version != snapshot_feature_schema_version:
        return SchemaCompatResult(
            compatible=False,
            error=SchemaIncompatibilityError(
                "version_mismatch",
                f"feature schema version mismatch: artifact has "
                f"version {artifact_feature_schema_version}, snapshot has "
                f"version {snapshot_feature_schema_version}. "
                f"Retrain the model on the new feature pipeline.",
            ),
        )

    # Rule 2: exact hash match = fully compatible.
    if artifact_feature_schema_hash == snapshot_feature_schema_hash:
        return SchemaCompatResult(compatible=True)

    # Rule 3: check feature subsets.
    missing = artifact_features - snapshot_features
    if missing:
        return SchemaCompatResult(
            compatible=False,
            error=SchemaIncompatibilityError(
                "missing_features",
                f"snapshot is missing features the artifact was trained on: "
                f"{sorted(missing)}. The model cannot make predictions "
                f"without these features.",
            ),
        )

    # Rule 4: extra features in snapshot.
    extra = snapshot_features - artifact_features
    if extra and not allow_extra_features:
        return SchemaCompatResult(
            compatible=False,
            error=SchemaIncompatibilityError(
                "extra_features",
                f"snapshot has extra features not in the artifact's schema: "
                f"{sorted(extra)}. Set allow_extra_features=True to ignore.",
            ),
        )

    # Hashes differ but all artifact features are present -- compatible
    # with a warning.  The hash mismatch is informational.
    return SchemaCompatResult(compatible=True)


def assert_feature_schema_compatible(
    *,
    artifact_feature_schema_hash: str,
    artifact_feature_schema_version: int,
    artifact_feature_names: tuple[str, ...] | list[str],
    snapshot_feature_schema_hash: str,
    snapshot_feature_schema_version: int,
    snapshot_feature_names: tuple[str, ...] | list[str],
    allow_extra_features: bool = True,
) -> None:
    """Check compatibility and raise on failure."""
    result = check_feature_schema_compatibility(
        artifact_feature_schema_hash=artifact_feature_schema_hash,
        artifact_feature_schema_version=artifact_feature_schema_version,
        artifact_feature_names=artifact_feature_names,
        snapshot_feature_schema_hash=snapshot_feature_schema_hash,
        snapshot_feature_schema_version=snapshot_feature_schema_version,
        snapshot_feature_names=snapshot_feature_names,
        allow_extra_features=allow_extra_features,
    )
    if not result.compatible and result.error is not None:
        raise result.error
