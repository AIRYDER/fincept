"""
quant_foundry.dataset_manifest — point-in-time dataset manifest for the feature lake.

TASK-0405: Build Feature Lake Builder MVP.
Phase 2 / T-2.1: Split manifest URI from data URI with DatasetLoadSpec.

This module defines the *rich* manifest that the feature lake emits for a
point-in-time dataset export. It intentionally lives here (and NOT in
``schemas.py``) so that ownership stays file-disjoint from the Quant Foundry
core-schema track (Builder 2). The base ``DatasetManifest`` in ``schemas.py``
is a minimal cross-boundary contract; this module extends it with the
leakage-safe fold structure, embargo, point-in-time proof flag, and a stable
content hash that training jobs reference instead of DB credentials.

Cross-cutting quant rigor enforced here (NEXT_STEPS_PLAN §1, §3):
- Point-in-time proof is mandatory: ``pit_proof_verified`` is only True after
  the builder has asserted every feature value's ``observed_at <= decision_time``.
- Purged-k-fold + embargo boundaries are part of the manifest so training and
  the tournament use the *same* leakage-safe folds.
- Embargo length must be >= the maximum label horizon in the dataset.
- Reproducibility: the manifest hash covers every field that affects a
  training run (schema hashes, universe, row count, checksum, folds, PIT flag).

Phase 2 — manifest-first loading (T-2.1):
- The manifest URI and the data URI are now **separate** fields. Previously
  ``dataset_manifest_ref`` (on ``RunPodTrainingRequest``) was overloaded as
  both a manifest-ish id *and* a raw data path. That made it impossible to
  verify the manifest before reading the data — a worker had to open the
  file to discover what it was training on.
- ``FeatureLakeManifest`` now carries explicit ``manifest_uri`` and
  ``data_uri`` fields (plus ``data_format``, ``data_sha256``,
  ``quality_report_uri``, ``quality_report_sha256``). These are optional for
  backward compat with canary/research mode (which may still pass a raw
  parquet path), but **production mode requires both**.
- ``DatasetLoadSpec`` is the new contract a worker receives to load a
  dataset manifest-first: it fetches the manifest from ``manifest_uri``,
  verifies ``manifest_sha256``, then reads the data from the manifest-declared
  ``data_uri``, verifies ``data_sha256``, and checks row count + schema hashes.
- **Direct CSV/parquet is only for canary mode.** Production and research
  runs must go through a manifest. The ``DatasetLoadSpec`` validator rejects
  a raw data path as ``manifest_uri`` in production mode.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FoldBoundary(BaseModel):
    """A single purged-k-fold split boundary.

    All timestamps are nanoseconds since epoch. The ``purge_*`` window is the
    gap between train and validation that prevents overlapping labels from
    leaking across the fold boundary (López de Prado purged k-fold).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    fold_id: int
    train_start: int
    train_end: int
    val_start: int
    val_end: int
    purge_start: int
    purge_end: int

    @model_validator(mode="after")
    def _check_ordering(self) -> FoldBoundary:
        if not (self.train_start <= self.train_end):
            raise ValueError(f"fold {self.fold_id}: train_start > train_end")
        if not (self.val_start <= self.val_end):
            raise ValueError(f"fold {self.fold_id}: val_start > val_end")
        # The purge window must sit between train_end and val_start and be
        # wide enough to prevent label overlap (embargo enforced at the spec
        # level, but the boundary itself must not have train bleeding into val
        # with zero purge).
        if self.val_start < self.purge_end:
            raise ValueError(
                f"fold {self.fold_id}: validation starts before purge ends "
                f"(val_start={self.val_start}, purge_end={self.purge_end}) — "
                "label leak / overlap detected"
            )
        if self.purge_start < self.train_end:
            raise ValueError(f"fold {self.fold_id}: purge starts before train ends — overlap")
        if self.purge_end <= self.purge_start:
            raise ValueError(
                f"fold {self.fold_id}: purge window must be non-empty "
                f"(purge_start={self.purge_start}, purge_end={self.purge_end})"
            )
        return self


class PurgedFoldSpec(BaseModel):
    """Full purged-k-fold specification for a dataset.

    ``embargo_ns`` is the embargo length applied *after* each validation fold;
    it must be >= ``max_label_horizon_ns`` so that no training row's label
    window overlaps a validation row's feature window.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    folds: tuple[FoldBoundary, ...]
    embargo_ns: int
    max_label_horizon_ns: int

    @model_validator(mode="after")
    def _check_embargo(self) -> PurgedFoldSpec:
        if self.embargo_ns < self.max_label_horizon_ns:
            raise ValueError(
                f"embargo ({self.embargo_ns}ns) must be >= "
                f"max label horizon ({self.max_label_horizon_ns}ns)"
            )
        if not self.folds:
            raise ValueError("at least one fold is required")
        return self


# ---------------------------------------------------------------------------
# Phase 2 — manifest-first loading primitives
# ---------------------------------------------------------------------------

# 64-char lowercase hex (SHA-256). Accept any-case input and normalise to
# lowercase so hash comparisons are stable (same pattern as schemas.py).
_HEX256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _validate_hex256(value: str, field_name: str) -> str:
    """Require a 64-char hex SHA-256, return lowercase."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty 64-char hex string")
    if not _HEX256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-char hex SHA-256; got {value!r}")
    return value.lower()


class DataFormat(StrEnum):
    """Supported on-disk formats for the data file referenced by a manifest.

    The worker's ``ManifestDatasetLoader`` (T-2.2) dispatches on this value
    to select the correct reader (pyarrow for parquet, numpy/pandas for csv).
    """

    PARQUET = "parquet"
    CSV = "csv"


class TrainingMode(StrEnum):
    """Training mode — mirrors ``quant_foundry.training_manifest.TrainingMode``.

    Re-declared here (rather than imported) to keep ``dataset_manifest.py``
    file-disjoint from ``training_manifest.py`` (which is owned by a different
    builder). The values must stay in sync.
    """

    CANARY = "canary"
    RESEARCH = "research"
    PRODUCTION = "production"


# Substrings that suggest a URI is a raw data file rather than a manifest
# JSON. Used by the production-mode validator to reject raw CSV/parquet as
# a manifest URI (the worker must read the manifest FIRST, not the data).
_RAW_DATA_EXTENSIONS = (".csv", ".parquet", ".csv.gz", ".parquet.gz", ".feather")


def _is_raw_data_uri(uri: str) -> bool:
    """Return True if ``uri`` looks like a raw data file rather than a manifest.

    A manifest URI should point at a JSON manifest (``.manifest.json`` or
    ``.json``). A raw data URI ends in ``.csv``, ``.parquet``, etc. or uses
    ``inline://`` (inline CSV payload).
    """
    if not uri:
        return False
    low = uri.lower()
    if low.startswith("inline://"):
        return True
    if any(low.endswith(ext) for ext in _RAW_DATA_EXTENSIONS):
        return True
    return False


class FeatureLakeManifest(BaseModel):
    """Point-in-time dataset manifest emitted by the feature lake builder.

    This is the *only* thing a RunPod training worker references — never a DB
    connection. The ``manifest_hash`` is a stable content hash over every field
    that affects reproducibility, so two exports of the same PIT dataset
    produce the same hash and a single changed row changes the hash.

    Phase 2 (T-2.1) — manifest/data URI split:
    ``manifest_uri`` is where *this manifest* can be fetched (a JSON file).
    ``data_uri`` is where the *actual tabular data* lives (parquet/csv).
    These are deliberately separate so a worker can fetch + verify the
    manifest BEFORE opening the data file. Both are optional for backward
    compat with canary/research mode (which may still pass a raw parquet
    path as ``dataset_manifest_ref``), but **production mode requires both**
    (enforced on :class:`DatasetLoadSpec` and via
    :meth:`validate_for_mode`).

    .. note::

        **Direct parquet is only for canary mode.** A canary or research run
        may pass a raw ``.parquet``/``.csv`` path, but a production run MUST
        go through a manifest. Call :meth:`validate_for_mode` with
        ``mode=production`` to enforce this.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    feature_schema_hash: str
    label_schema_hash: str
    as_of_ts: int
    universe_hash: str
    row_count: int
    checksum: str
    folds: PurgedFoldSpec
    pit_proof_verified: bool
    source_vintage_refs: list[str] = Field(default_factory=list)
    quality_report_hash: str | None = None
    # --- Phase 2: explicit manifest / data URI split -------------------
    # ``manifest_uri``: where to fetch THIS manifest (JSON). None for
    #   canary/research backward compat (the ref is passed inline).
    # ``data_uri``: where to fetch the actual tabular data. None for
    #   canary/research backward compat (the ref IS the data path).
    manifest_uri: str | None = None
    data_uri: str | None = None
    data_format: DataFormat | None = None
    data_sha256: str | None = None
    quality_report_uri: str | None = None
    quality_report_sha256: str | None = None

    # --- validators -----------------------------------------------------

    @field_validator("data_sha256")
    @classmethod
    def _data_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "data_sha256")

    @field_validator("quality_report_sha256")
    @classmethod
    def _quality_report_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "quality_report_sha256")

    @model_validator(mode="after")
    def _data_format_consistency(self) -> FeatureLakeManifest:
        """If ``data_uri`` is set, ``data_format`` should be inferable or set."""
        if self.data_uri is not None and self.data_format is None:
            low = self.data_uri.lower()
            if low.endswith(".parquet") or low.endswith(".parquet.gz"):
                # Don't auto-set (frozen); just skip — the loader infers.
                pass
            elif low.endswith(".csv") or low.endswith(".csv.gz"):
                pass
        return self

    # --- mode validation ------------------------------------------------

    def validate_for_mode(self, mode: TrainingMode | str) -> None:
        """Validate this manifest against the rules of ``mode``.

        Production mode fails closed:
        - ``manifest_uri`` must be present (the worker reads the manifest
          first, not the raw data file).
        - ``data_uri`` must be present (the data location is declared in
          the manifest, not passed out-of-band).
        - ``manifest_uri`` must NOT be a raw CSV/parquet path (it must be
          a manifest JSON).
        - ``pit_proof_verified`` must be True.

        Canary and research modes are permissive (they allow direct
        parquet for local iteration and contract proofs).

        Raises:
            ValueError: if any production requirement is unmet.
        """
        if isinstance(mode, str):
            mode = TrainingMode(mode)
        if mode != TrainingMode.PRODUCTION:
            return
        errors: list[str] = []
        if not self.manifest_uri or not self.manifest_uri.strip():
            errors.append(
                "production mode requires manifest_uri "
                "(worker must read the manifest first, not the raw data file)"
            )
        elif _is_raw_data_uri(self.manifest_uri):
            errors.append(
                "production mode requires manifest_uri to point at a manifest "
                f"JSON, not a raw data file: {self.manifest_uri!r}"
            )
        if not self.data_uri or not self.data_uri.strip():
            errors.append(
                "production mode requires data_uri "
                "(data location must be declared in the manifest)"
            )
        if not self.pit_proof_verified:
            errors.append(
                "production mode requires pit_proof_verified=True "
                "(point-in-time proof is mandatory)"
            )
        if errors:
            raise ValueError(
                "production mode manifest validation failed: " + "; ".join(errors)
            )

    # --- hashing ---------------------------------------------------------

    def _canonical_payload(self) -> dict[str, Any]:
        """Canonical, sorted-key dict representation for stable hashing."""
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "feature_schema_hash": self.feature_schema_hash,
            "label_schema_hash": self.label_schema_hash,
            "as_of_ts": self.as_of_ts,
            "universe_hash": self.universe_hash,
            "row_count": self.row_count,
            "checksum": self.checksum,
            "folds": self.folds.model_dump(mode="json"),
            "pit_proof_verified": self.pit_proof_verified,
            "source_vintage_refs": list(self.source_vintage_refs),
            "quality_report_hash": self.quality_report_hash,
            # Phase 2: include the manifest/data URI split in the hash so
            # a changed data location or format alters the manifest hash.
            "manifest_uri": self.manifest_uri,
            "data_uri": self.data_uri,
            "data_format": (
                self.data_format.value if self.data_format is not None else None
            ),
            "data_sha256": self.data_sha256,
            "quality_report_uri": self.quality_report_uri,
            "quality_report_sha256": self.quality_report_sha256,
        }

    def manifest_hash(self) -> str:
        """Stable SHA-256 hex digest over the canonical manifest payload.

        Deterministic for identical inputs; any field change alters the hash.
        Used as the training-reference identifier (no DB credentials involved).
        """
        payload = json.dumps(self._canonical_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        """Serialize the manifest (including its hash) to a stable JSON string."""
        body = self._canonical_payload()
        body["manifest_hash"] = self.manifest_hash()
        return json.dumps(body, sort_keys=True, indent=2)

    def training_reference(self) -> dict[str, Any]:
        """Return the reference a training job uses INSTEAD of DB credentials.

        Deliberately contains only ``dataset_id`` + ``manifest_hash`` plus a
        ``kind`` marker. No DSN, password, or connection string is ever present.
        """
        return {
            "kind": "feature_lake_manifest_ref",
            "dataset_id": self.dataset_id,
            "manifest_hash": self.manifest_hash(),
        }

    def to_load_spec(
        self,
        *,
        manifest_sha256: str | None = None,
    ) -> DatasetLoadSpec:
        """Build a :class:`DatasetLoadSpec` from this manifest.

        The resulting spec carries the manifest URI, data URI, and all
        verification hashes so a worker can load the dataset manifest-first.

        Args:
            manifest_sha256: the SHA-256 of the serialized manifest JSON
                (as produced by :meth:`to_json`). If omitted, the hash of
                ``to_json()`` is computed automatically.

        Raises:
            ValueError: if ``manifest_uri`` or ``data_uri`` is missing
                (they are required to build a load spec).
        """
        if not self.manifest_uri:
            raise ValueError(
                "cannot build DatasetLoadSpec: manifest_uri is not set on "
                "this FeatureLakeManifest"
            )
        if not self.data_uri:
            raise ValueError(
                "cannot build DatasetLoadSpec: data_uri is not set on "
                "this FeatureLakeManifest"
            )
        if manifest_sha256 is None:
            manifest_sha256 = hashlib.sha256(
                self.to_json().encode("utf-8"),
            ).hexdigest()
        return DatasetLoadSpec(
            manifest_uri=self.manifest_uri,
            manifest_sha256=manifest_sha256,
            data_uri=self.data_uri,
            data_sha256=self.data_sha256,
            data_format=self.data_format,
            row_count=self.row_count,
            feature_schema_hash=self.feature_schema_hash,
            label_schema_hash=self.label_schema_hash,
            quality_report_uri=self.quality_report_uri,
            quality_report_sha256=self.quality_report_sha256,
        )


class DatasetLoadSpec(BaseModel):
    """Manifest-first dataset load contract for RunPod training workers.

    This is the **explicit** contract that replaces the overloaded
    ``dataset_manifest_ref`` field. A worker receives a ``DatasetLoadSpec``
    and loads the dataset in the correct order:

    1. **Fetch the manifest** from ``manifest_uri``.
    2. **Verify the manifest hash** against ``manifest_sha256`` (fail on
       mismatch — acceptance criterion: manifest hash mismatch fails).
    3. **Read the data URI** from the verified manifest (not from an
       out-of-band path — acceptance criterion: worker reads manifest first).
    4. **Fetch the data** from ``data_uri``.
    5. **Verify the data hash** against ``data_sha256``.
    6. **Verify row count** against ``row_count``.
    7. **Verify schema hashes** against ``feature_schema_hash`` and
       ``label_schema_hash``.

    Production mode validation (enforced at construction):
    - ``manifest_uri`` must be present and must NOT be a raw CSV/parquet
      path (acceptance criterion: direct CSV/parquet in production fails).
    - ``data_uri`` must be present (acceptance criterion: production
      request without manifest URI fails — and data URI too).
    - ``manifest_sha256`` must be present (acceptance criterion: manifest
      hash mismatch fails — the hash must be declared up front).

    Canary mode is permissive: ``manifest_uri`` and ``data_uri`` may both
    point at the same raw parquet file (direct parquet is only for canary
    mode).

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    # Where to fetch the manifest (JSON). Required for production.
    manifest_uri: str
    # SHA-256 of the serialized manifest JSON. Required for production
    # (the worker verifies this before trusting the manifest's data_uri).
    manifest_sha256: str | None = None
    # Where to fetch the actual tabular data. Required for production.
    data_uri: str
    # SHA-256 of the data file. Optional (verified if present).
    data_sha256: str | None = None
    # Format of the data file (parquet or csv). If None, inferred from
    # the data_uri extension by the loader.
    data_format: DataFormat | None = None
    # Expected row count (verified after loading).
    row_count: int | None = None
    # Expected feature schema hash (verified after loading).
    feature_schema_hash: str | None = None
    # Expected label schema hash (verified after loading).
    label_schema_hash: str | None = None
    # Quality report URI + hash (optional, verified if present).
    quality_report_uri: str | None = None
    quality_report_sha256: str | None = None
    # Training mode — selects which validation rules apply. Defaults to
    # canary (the most lenient mode) for backward compat.
    mode: TrainingMode = TrainingMode.CANARY

    # --- validators -----------------------------------------------------

    @field_validator("manifest_uri")
    @classmethod
    def _manifest_uri_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("manifest_uri must be non-empty")
        return v

    @field_validator("data_uri")
    @classmethod
    def _data_uri_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("data_uri must be non-empty")
        return v

    @field_validator("manifest_sha256")
    @classmethod
    def _manifest_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "manifest_sha256")

    @field_validator("data_sha256")
    @classmethod
    def _data_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "data_sha256")

    @field_validator("feature_schema_hash")
    @classmethod
    def _feature_schema_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "feature_schema_hash")

    @field_validator("label_schema_hash")
    @classmethod
    def _label_schema_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "label_schema_hash")

    @field_validator("quality_report_sha256")
    @classmethod
    def _quality_report_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "quality_report_sha256")

    @field_validator("row_count")
    @classmethod
    def _row_count_nonnegative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"row_count must be >= 0; got {v}")
        return v

    @model_validator(mode="after")
    def _validate_mode_rules(self) -> DatasetLoadSpec:
        """Enforce per-mode validation rules.

        Production mode fails closed:
        - ``manifest_uri`` must NOT be a raw CSV/parquet path (the worker
          reads the manifest first, not the raw data file).
        - ``manifest_sha256`` must be present (manifest hash verification
          is mandatory — mismatch fails).
        - ``data_uri`` must be present (already enforced by the field
          validator, but the error message is clearer here).

        Canary and research modes are permissive.
        """
        if self.mode != TrainingMode.PRODUCTION:
            return self
        errors: list[str] = []
        # manifest_uri must not be a raw data file.
        if _is_raw_data_uri(self.manifest_uri):
            errors.append(
                "production mode requires manifest_uri to point at a manifest "
                f"JSON, not a raw data file: {self.manifest_uri!r} "
                "(direct CSV/parquet is only for canary mode)"
            )
        # manifest_sha256 must be present for hash verification.
        if not self.manifest_sha256:
            errors.append(
                "production mode requires manifest_sha256 "
                "(manifest hash verification is mandatory — mismatch fails)"
            )
        if errors:
            raise ValueError(
                "production mode DatasetLoadSpec validation failed: "
                + "; ".join(errors)
            )
        return self

    # --- manifest hash verification -------------------------------------

    def verify_manifest_hash(self, manifest_json: str | bytes) -> bool:
        """Verify that ``manifest_json`` matches ``manifest_sha256``.

        Computes SHA-256 over the raw manifest bytes and compares to the
        declared ``manifest_sha256``. Returns True on match, False on
        mismatch. Raises ``ValueError`` if ``manifest_sha256`` is not set
        (it must be declared before verification).

        This implements acceptance criterion 2: manifest hash mismatch
        fails. The caller (the loader) should raise ``TrainingFailure`` on
        a False return.

        Args:
            manifest_json: the raw manifest content (str or bytes). If str,
                encoded as UTF-8 before hashing.
        """
        if not self.manifest_sha256:
            raise ValueError(
                "cannot verify manifest hash: manifest_sha256 is not set"
            )
        if isinstance(manifest_json, str):
            manifest_bytes = manifest_json.encode("utf-8")
        else:
            manifest_bytes = manifest_json
        actual = hashlib.sha256(manifest_bytes).hexdigest()
        return actual == self.manifest_sha256

    def verify_data_hash(self, data_bytes: bytes) -> bool:
        """Verify that ``data_bytes`` matches ``data_sha256``.

        Returns True on match, False on mismatch. Raises ``ValueError`` if
        ``data_sha256`` is not set.
        """
        if not self.data_sha256:
            raise ValueError(
                "cannot verify data hash: data_sha256 is not set"
            )
        actual = hashlib.sha256(data_bytes).hexdigest()
        return actual == self.data_sha256

    def verify_row_count(self, actual_row_count: int) -> bool:
        """Verify that ``actual_row_count`` matches ``row_count``.

        Returns True on match, False on mismatch. Raises ``ValueError`` if
        ``row_count`` is not set.
        """
        if self.row_count is None:
            raise ValueError(
                "cannot verify row count: row_count is not set"
            )
        return actual_row_count == self.row_count

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (for embedding in a request)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetLoadSpec:
        """Deserialize from a dict (e.g. from ``extra_constraints``)."""
        return cls(**data)
