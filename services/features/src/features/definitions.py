"""Versioned feature definitions and feature-set versioning (Tier 2.6).

This module formalizes feature definitions so that:

  1. Every feature has a name, transform type, parameters, and version.
  2. A ``FeatureSetVersion`` bundles a set of feature definitions
     with a deterministic hash, so two training runs that reference
     the same feature-set version are guaranteed to use the same
     features.
  3. The dataset manifest and training request can pin a
     ``feature_set_version``, extending the existing
     ``feature_schema_hash`` with a human-readable version string.

The existing transform classes (``PriceFeatures``, ``VolatilityFeatures``,
``CrossFeatures``) remain the compute kernels. This module provides the
**declarative layer** that makes feature definitions versioned and
auditable.

Design:
  * Pydantic v2 models, frozen + ``extra='forbid'`` for audit integrity.
  * The feature-set hash is a deterministic SHA-256 over the canonical
    JSON of the feature definitions, so identical definitions produce
    identical hashes.
  * No imports from ``services/`` beyond ``features`` itself.
  * The ``FeatureRegistry`` is a thin in-memory registry (file-backed
    persistence is a future concern; the registry is enough for
    training-request validation).
"""

from __future__ import annotations

import hashlib
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FeatureDefinition",
    "FeatureSetVersion",
    "FeatureRegistry",
    "RegistryError",
    "compute_feature_set_hash",
]


# --------------------------------------------------------------------------- #
# Feature definition                                                          #
# --------------------------------------------------------------------------- #


class FeatureDefinition(BaseModel):
    """A single versioned feature definition.

    Fields:
        feature_name: the feature key (e.g. ``"ret_log_1"``,
            ``"mom_20"``, ``"vol_rs_60"``).
        transform_type: the transform category (``"price"``,
            ``"volatility"``, ``"cross"``).
        parameters: transform parameters as a dict (e.g.
            ``{"window": 20}`` for a 20-bar momentum).
        version: the feature definition version (semver-style,
            e.g. ``"1.0.0"``).
        description: optional human-readable description.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_name: str
    transform_type: str
    parameters: dict[str, object] = Field(default_factory=dict)
    version: str = "1.0.0"
    description: str | None = None

    @field_validator("feature_name")
    @classmethod
    def _feature_name_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("feature_name must be non-empty")
        return v

    @field_validator("transform_type")
    @classmethod
    def _transform_type_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("transform_type must be non-empty")
        return v

    @field_validator("version")
    @classmethod
    def _version_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("version must be non-empty")
        return v


# --------------------------------------------------------------------------- #
# Feature set version                                                         #
# --------------------------------------------------------------------------- #


def compute_feature_set_hash(
    definitions: tuple[FeatureDefinition, ...],
) -> str:
    """Compute a deterministic SHA-256 hash over feature definitions.

    The hash is over the canonical JSON of the definitions (sorted by
    feature_name, then by version), so identical definitions always
    produce the same hash.
    """
    sorted_defs = sorted(definitions, key=lambda d: (d.feature_name, d.version))
    payload = json.dumps(
        [d.model_dump(mode="json") for d in sorted_defs],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FeatureSetVersion(BaseModel):
    """A versioned set of feature definitions.

    This is the unit that a training request pins via
    ``feature_set_version``. The ``feature_set_hash`` is a deterministic
    SHA-256 over the feature definitions, so two training runs that
    reference the same feature-set version are guaranteed to use the
    same features.

    Fields:
        feature_set_id: a human-readable identifier (e.g.
            ``"price-vol-cross-v1"``).
        version: the feature-set version string (e.g. ``"1.0.0"``).
        feature_definitions: the tuple of feature definitions.
        feature_set_hash: deterministic SHA-256 over the definitions.
        created_at_ns: creation timestamp (nanoseconds since epoch).
        description: optional human-readable description.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_set_id: str
    version: str
    feature_definitions: tuple[FeatureDefinition, ...]
    feature_set_hash: str
    created_at_ns: int
    description: str | None = None

    @field_validator("feature_set_id")
    @classmethod
    def _feature_set_id_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("feature_set_id must be non-empty")
        return v

    @field_validator("version")
    @classmethod
    def _version_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("version must be non-empty")
        return v

    @field_validator("feature_set_hash")
    @classmethod
    def _feature_set_hash_shape(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError("feature_set_hash must be a 64-char SHA-256 hex")
        return v

    @classmethod
    def create(
        cls,
        *,
        feature_set_id: str,
        version: str,
        feature_definitions: tuple[FeatureDefinition, ...],
        created_at_ns: int,
        description: str | None = None,
    ) -> "FeatureSetVersion":
        """Create a FeatureSetVersion, computing the hash automatically."""
        if not feature_definitions:
            raise ValueError("feature_definitions must be non-empty")
        return cls(
            feature_set_id=feature_set_id,
            version=version,
            feature_definitions=feature_definitions,
            feature_set_hash=compute_feature_set_hash(feature_definitions),
            created_at_ns=created_at_ns,
            description=description,
        )

    @property
    def feature_names(self) -> tuple[str, ...]:
        """The sorted tuple of feature names in this set."""
        return tuple(sorted(d.feature_name for d in self.feature_definitions))


# --------------------------------------------------------------------------- #
# Feature registry                                                            #
# --------------------------------------------------------------------------- #


class RegistryError(Exception):
    """Raised when a feature registry operation fails."""


class FeatureRegistry:
    """In-memory registry of feature set versions.

    The registry maps ``(feature_set_id, version)`` to a
    :class:`FeatureSetVersion`. It supports:

      * ``register()`` — add a feature set version (idempotent on
        re-registration of the same version).
      * ``get()`` — retrieve a feature set version by id and version.
      * ``list_versions()`` — list all versions for a feature set id.
      * ``verify()`` — verify that a training request's
        ``feature_set_version`` matches a registered version.

    The registry is intentionally in-memory. File-backed persistence
    is a future concern; for now, the registry is constructed at
    startup from the known feature definitions.
    """

    def __init__(self) -> None:
        self._sets: dict[tuple[str, str], FeatureSetVersion] = {}

    def register(self, fsv: FeatureSetVersion) -> None:
        """Register a feature set version.

        Raises:
            RegistryError: if a different FeatureSetVersion with the
                same ``(feature_set_id, version)`` is already
                registered (hash mismatch).
        """
        key = (fsv.feature_set_id, fsv.version)
        existing = self._sets.get(key)
        if existing is not None and existing.feature_set_hash != fsv.feature_set_hash:
            raise RegistryError(
                f"feature set {fsv.feature_set_id} version {fsv.version} "
                f"already registered with a different hash: "
                f"existing={existing.feature_set_hash[:16]}... "
                f"new={fsv.feature_set_hash[:16]}..."
            )
        self._sets[key] = fsv

    def get(self, feature_set_id: str, version: str) -> FeatureSetVersion:
        """Retrieve a feature set version.

        Raises:
            RegistryError: if not found.
        """
        key = (feature_set_id, version)
        fsv = self._sets.get(key)
        if fsv is None:
            raise RegistryError(
                f"feature set {feature_set_id} version {version} not found"
            )
        return fsv

    def list_versions(self, feature_set_id: str) -> list[str]:
        """List all registered versions for a feature set id."""
        return sorted(
            v for (sid, v) in self._sets if sid == feature_set_id
        )

    def verify(self, feature_set_id: str, version: str) -> bool:
        """Verify that a feature set version is registered.

        Returns True if the version is registered, False otherwise.
        Unlike :meth:`get`, this does not raise.
        """
        return (feature_set_id, version) in self._sets

    def __len__(self) -> int:
        return len(self._sets)
