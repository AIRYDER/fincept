"""Manifest / snapshot schemas for the shared ML dataset evidence spine.

These primitives are importable from any service (they live in
``fincept_core``) and are intentionally separate from
``fincept_core.schemas`` (which holds *event* schemas) and from
``services/quant_foundry.schemas`` (which holds quant-foundry-specific
manifests).  All classes here are frozen Pydantic v2 models with
``extra="forbid"`` so that manifests are tamper-evident and round-trip
serialisation is exact.
"""

from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# 64-char lowercase hex (SHA-256).  We accept any-case input and normalise
# to lowercase in the validators so that hash comparisons are stable.
_HEX256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _validate_hex256(value: str, field_name: str) -> str:
    """Shared validator: require a 64-char hex SHA-256, return lowercase."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty 64-char hex string")
    if not _HEX256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-char hex SHA-256; got {value!r}")
    return value.lower()


class FeatureRow(BaseModel):
    """A single point-in-time feature row inside a :class:`FeatureSnapshot`.

    ``features`` is a flat ``str -> float`` mapping keyed by the feature
    names declared in the feature schema (the schema whose hash is recorded
    on the parent snapshot).  ``ts`` is a nanosecond epoch timestamp aligned
    with the rest of the evidence spine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    symbol: str
    ts: int
    features: dict[str, float] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must be non-empty")
        return v

    @field_validator("ts")
    @classmethod
    def _ts_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"ts must be >= 0; got {v}")
        return v


class DatasetManifest(BaseModel):
    """Point-in-time description of a dataset used for training/inference.

    Captures the *what* (schema hashes), *when* (``as_of_ts``), *which rows*
    (``universe_hash`` + ``row_count``) and *provenance*
    (``source_vintage_refs``) of a dataset so that any consumer can verify
    it is operating on the exact same data the producer intended.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    feature_schema_version: int = 1
    dataset_id: str
    feature_schema_hash: str
    label_schema_hash: str
    as_of_ts: int
    universe_hash: str
    row_count: int
    source_vintage_refs: list[str] = Field(default_factory=list)

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("dataset_id must be non-empty")
        return v

    @field_validator("feature_schema_hash")
    @classmethod
    def _feature_schema_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "feature_schema_hash")

    @field_validator("label_schema_hash")
    @classmethod
    def _label_schema_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "label_schema_hash")

    @field_validator("universe_hash")
    @classmethod
    def _universe_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "universe_hash")

    @field_validator("as_of_ts")
    @classmethod
    def _as_of_ts_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"as_of_ts must be >= 0; got {v}")
        return v

    @field_validator("row_count")
    @classmethod
    def _row_count_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"row_count must be >= 0; got {v}")
        return v


class ArtifactManifest(BaseModel):
    """Metadata for a trained model artifact (pull-based verified import).

    The ``sha256`` + ``size_bytes`` pair lets a consumer verify a downloaded
    artifact byte-for-byte before loading it; ``uri`` is the *declared*
    location (may be ``None`` for air-gapped staging).  The schema hashes
    bind the artifact to the feature/label schemas it was trained against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    feature_schema_version: int = 1
    artifact_id: str
    sha256: str
    size_bytes: int
    uri: str | None = None
    model_family: str
    created_at_ns: int
    feature_schema_hash: str
    label_schema_hash: str
    code_git_sha: str | None = None
    lockfile_hash: str | None = None
    container_image_digest: str | None = None

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("artifact_id must be non-empty")
        return v

    @field_validator("sha256")
    @classmethod
    def _sha256_shape(cls, v: str) -> str:
        return _validate_hex256(v, "sha256")

    @field_validator("feature_schema_hash")
    @classmethod
    def _feature_schema_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "feature_schema_hash")

    @field_validator("label_schema_hash")
    @classmethod
    def _label_schema_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "label_schema_hash")

    @field_validator("size_bytes")
    @classmethod
    def _size_bytes_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"size_bytes must be >= 0; got {v}")
        return v

    @field_validator("created_at_ns")
    @classmethod
    def _created_at_ns_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"created_at_ns must be >= 0; got {v}")
        return v

    @field_validator("model_family")
    @classmethod
    def _model_family_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_family must be non-empty")
        return v


class FeatureSnapshot(BaseModel):
    """A frozen, point-in-time snapshot of feature rows.

    ``decision_time_ns`` is the as-of timestamp that gates which rows are
    eligible (rows with ``ts > decision_time_ns`` must not appear).  The
    ``feature_schema_hash`` binds the snapshot to the feature schema that
    defines the keys of each row's ``features`` dict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    feature_schema_version: int = 1
    decision_time_ns: int
    rows: list[FeatureRow]
    feature_schema_hash: str

    @field_validator("decision_time_ns")
    @classmethod
    def _decision_time_ns_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"decision_time_ns must be >= 0; got {v}")
        return v

    @field_validator("feature_schema_hash")
    @classmethod
    def _feature_schema_hash_shape(cls, v: str) -> str:
        return _validate_hex256(v, "feature_schema_hash")

    @model_validator(mode="after")
    def _no_lookahead(self) -> FeatureSnapshot:
        for row in self.rows:
            if row.ts > self.decision_time_ns:
                raise ValueError(
                    f"FeatureRow ts={row.ts} exceeds decision_time_ns="
                    f"{self.decision_time_ns} (look-ahead violation)"
                )
        return self
