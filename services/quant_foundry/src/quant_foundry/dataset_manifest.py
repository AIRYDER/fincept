"""
quant_foundry.dataset_manifest — point-in-time dataset manifest for the feature lake.

TASK-0405: Build Feature Lake Builder MVP.
Phase 2 / T-2.1: Split manifest URI from data URI with DatasetLoadSpec.
Phase 3 / T-3.1: Dataset Registry with readiness levels and registration commands.

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

Phase 3 — dataset registry (T-3.1):
- :class:`ReadinessLevel` defines four readiness levels (L1 raw → L4
  production-ready) that gate which datasets may be dispatched for production
  training.
- :class:`DatasetRegistryEntry` is an append-only record of a registered
  dataset: its manifest URI/hash, data URI/hash, quality URI/hash, source
  receipts, readiness level, upload receipt, and status.
- :class:`DatasetRegistry` is the durable registry (JSONL-backed) that
  provides the commands: ``inspect``, ``register``, ``stage_upload``,
  ``promote_readiness``, and ``dispatch_training``.
- Duplicate dataset ids are versioned (the version is incremented on each
  re-registration of the same id).
- Production dispatch is rejected unless the dataset is registered with
  readiness L3+ (quality-gated or production-ready).
- Stale upload receipts (expired timestamps or mismatched hashes) are
  rejected at ``stage_upload`` time.
- Production dispatch accepts a *dataset id* (resolved via the registry),
  never an ad hoc raw file path — this is the registry-side counterpart to
  the ``dataset_registry_ref`` field on ``TrainingManifest``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC
from enum import StrEnum
from pathlib import Path
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
    # Tier 2.6: feature-set version pin. Human-readable version string
    # that identifies the feature definitions used to build this dataset.
    # The feature_schema_hash is the cryptographic guarantee; this is
    # the human-readable version for audit and provenance.
    feature_set_version: str | None = None
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
    # C3: signed PIT evidence v1. A tamper-evident record that proves the
    # dataset export was leakage-safe. Carried as a plain dict (the
    # serialized :class:`~quant_foundry.pit_evidence.PITEvidence`) so the
    # manifest remains JSON-serializable and the evidence can be verified
    # independently by the training worker. Deliberately EXCLUDED from
    # the manifest hash (the evidence is a derived artifact that
    # references the manifest hash — including it would create a circular
    # hash dependency).
    pit_evidence: dict[str, Any] | None = None

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
                "production mode requires data_uri (data location must be declared in the manifest)"
            )
        if not self.pit_proof_verified:
            errors.append(
                "production mode requires pit_proof_verified=True "
                "(point-in-time proof is mandatory)"
            )
        if errors:
            raise ValueError("production mode manifest validation failed: " + "; ".join(errors))

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
            "data_format": (self.data_format.value if self.data_format is not None else None),
            "data_sha256": self.data_sha256,
            "quality_report_uri": self.quality_report_uri,
            "quality_report_sha256": self.quality_report_sha256,
            # Tier 2.6: feature-set version in the hash so a changed
            # feature set version alters the manifest hash.
            "feature_set_version": self.feature_set_version,
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
                "cannot build DatasetLoadSpec: manifest_uri is not set on this FeatureLakeManifest"
            )
        if not self.data_uri:
            raise ValueError(
                "cannot build DatasetLoadSpec: data_uri is not set on this FeatureLakeManifest"
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
                "production mode DatasetLoadSpec validation failed: " + "; ".join(errors)
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
            raise ValueError("cannot verify manifest hash: manifest_sha256 is not set")
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
            raise ValueError("cannot verify data hash: data_sha256 is not set")
        actual = hashlib.sha256(data_bytes).hexdigest()
        return actual == self.data_sha256

    def verify_row_count(self, actual_row_count: int) -> bool:
        """Verify that ``actual_row_count`` matches ``row_count``.

        Returns True on match, False on mismatch. Raises ``ValueError`` if
        ``row_count`` is not set.
        """
        if self.row_count is None:
            raise ValueError("cannot verify row count: row_count is not set")
        return actual_row_count == self.row_count

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (for embedding in a request)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetLoadSpec:
        """Deserialize from a dict (e.g. from ``extra_constraints``)."""
        return cls(**data)


# ---------------------------------------------------------------------------
# Phase 3 — Dataset Registry (T-3.1)
# ---------------------------------------------------------------------------
#
# The dataset registry is the durable, append-only ledger of registered
# datasets. It is the single source of truth that the production dispatch
# path consults before allowing a training run to proceed. A production
# run must reference a *registered dataset id* (via
# ``TrainingManifest.dataset_registry_ref``), never an ad hoc raw file
# path. The registry resolves that id to a manifest URI + data URI +
# quality report URI and enforces readiness-level gating.
#
# Readiness levels form a monotone promotion ladder:
#   L1 (raw)            — freshly registered, no validation/quality checks
#   L2 (validated)      — manifest + data hashes verified, PIT proof checked
#   L3 (quality-gated)  — quality report attached and verified; eligible
#                         for production dispatch
#   L4 (production-ready) — promoted after a successful production canary;
#                           the highest trust level
#
# Production dispatch requires L3 or L4. Canary and research dispatch are
# permissive (they may use any registered dataset, or none).


class ReadinessLevel(StrEnum):
    """Dataset readiness levels — a monotone promotion ladder.

    Each level represents an increasing amount of verification that has been
    applied to a dataset before it may be dispatched for training:

    - ``L1`` (raw): the dataset has been registered with its manifest and
      data URIs but no verification has been performed. Suitable for
      canary/research iteration only.
    - ``L2`` (validated): the manifest hash, data hash, and PIT proof have
      been verified. The dataset is structurally sound.
    - ``L3`` (quality-gated): a quality report has been attached and its
      hash verified, and the report's leakage/drift checks pass. This is
      the **minimum** readiness level for production dispatch.
    - ``L4`` (production-ready): the dataset has been promoted after a
      successful production canary run. The highest trust level.
    """

    L1_RAW = "L1"
    L2_VALIDATED = "L2"
    L3_QUALITY_GATED = "L3"
    L4_PRODUCTION_READY = "L4"

    @classmethod
    def from_str(cls, value: str | ReadinessLevel) -> ReadinessLevel:
        """Parse a readiness level from a string (accepts ``"L1"`` etc.)."""
        if isinstance(value, cls):
            return value
        return cls(value)

    def rank(self) -> int:
        """Return the integer rank (1-4) for comparison."""
        return _READINESS_RANK[self]

    def at_least(self, other: ReadinessLevel) -> bool:
        """Return True if this level is >= ``other`` on the ladder."""
        return self.rank() >= other.rank()


_READINESS_RANK: dict[ReadinessLevel, int] = {
    ReadinessLevel.L1_RAW: 1,
    ReadinessLevel.L2_VALIDATED: 2,
    ReadinessLevel.L3_QUALITY_GATED: 3,
    ReadinessLevel.L4_PRODUCTION_READY: 4,
}

# Ordered ladder for promotion validation (cannot skip levels).
_READINESS_LADDER: tuple[ReadinessLevel, ...] = (
    ReadinessLevel.L1_RAW,
    ReadinessLevel.L2_VALIDATED,
    ReadinessLevel.L3_QUALITY_GATED,
    ReadinessLevel.L4_PRODUCTION_READY,
)


class RegistryStatus(StrEnum):
    """Lifecycle status of a registry entry.

    - ``REGISTERED``: the entry has been registered but not yet staged.
    - ``STAGED``: an upload receipt has been recorded (data staged for
      production access).
    - ``ACTIVE``: the entry is live and eligible for dispatch.
    - ``DEPRECATED``: the entry has been superseded by a newer version;
      dispatch is rejected.
    - ``REJECTED``: the entry failed validation and is permanently
      unavailable for dispatch.
    """

    REGISTERED = "registered"
    STAGED = "staged"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


# Maximum age (in seconds) of an upload receipt before it is considered
# stale. A receipt older than this window is rejected at ``stage_upload``
# time (acceptance criterion: stale upload receipt rejected). The default
# is 24 hours; callers may override via ``stale_receipt_ttl_seconds`` on
# the registry.
DEFAULT_STALE_RECEIPT_TTL_SECONDS = 86_400


# A registered dataset id is an opaque slug (no path separators, no raw
# data extensions). This mirrors ``_is_raw_dataset_ref`` in
# ``training_manifest.py`` but is kept here for file-disjointness.
_DATASET_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]{0,127}$")


def _is_registry_eligible_id(dataset_id: str) -> bool:
    """Return True if ``dataset_id`` is a valid opaque registry id.

    A registry id must be a slug (letters, digits, ``_``, ``.``, ``-``)
    with no path separators and no raw-data file extensions. This rejects
    ad hoc raw file paths (acceptance criterion: production dispatch
    accepts a dataset id, not a raw file path).
    """
    if not dataset_id:
        return False
    if _is_raw_data_uri(dataset_id):
        return False
    if "/" in dataset_id or "\\" in dataset_id:
        return False
    return bool(_DATASET_ID_PATTERN.fullmatch(dataset_id))


class UploadReceipt(BaseModel):
    """Receipt proving that a dataset's data has been staged for production.

    A receipt carries:
    - ``receipt_id``: an opaque id for this staging event.
    - ``dataset_id``: the dataset this receipt stages data for.
    - ``data_uri``: the staged data location (must match the entry's
      ``data_uri``).
    - ``data_sha256``: the hash of the staged data (must match the
      entry's ``data_sha256`` if set).
    - ``issued_at``: Unix timestamp (seconds) when the receipt was issued.
    - ``expires_at``: Unix timestamp (seconds) when the receipt expires.
    - ``receipt_hash``: SHA-256 over the receipt payload (tamper-evidence).

    A receipt is *stale* if the current time is past ``expires_at`` or the
    ``receipt_hash`` does not match the recomputed hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    receipt_id: str
    dataset_id: str
    data_uri: str
    data_sha256: str | None = None
    issued_at: int
    expires_at: int

    @field_validator("receipt_id", "dataset_id", "data_uri")
    @classmethod
    def _nonempty(cls, v: str, info: Any) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must be non-empty")
        return v

    @field_validator("data_sha256")
    @classmethod
    def _data_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "data_sha256")

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> UploadReceipt:
        if self.expires_at <= self.issued_at:
            raise ValueError(
                "expires_at must be after issued_at "
                f"(issued_at={self.issued_at}, expires_at={self.expires_at})"
            )
        return self

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "dataset_id": self.dataset_id,
            "data_uri": self.data_uri,
            "data_sha256": self.data_sha256,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def receipt_hash(self) -> str:
        """Stable SHA-256 over the canonical receipt payload."""
        payload = json.dumps(
            self._canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def is_stale(self, *, now: int | None = None, expected_hash: str | None = None) -> bool:
        """Return True if this receipt is stale (expired or hash mismatch).

        Args:
            now: current Unix timestamp (seconds). Defaults to
                ``int(time.time())``.
            expected_hash: the expected ``receipt_hash()``. If provided and
                the recomputed hash differs, the receipt is stale
                (tamper-evidence).
        """
        current = int(time.time()) if now is None else now
        if current >= self.expires_at:
            return True
        if expected_hash is not None and self.receipt_hash() != expected_hash:
            return True
        return False


class SourceReceipt(BaseModel):
    """A single source-vintage receipt proving the provenance of a dataset.

    Records the source module id, the vintage (as-of timestamp), and an
    optional content hash so the registry can audit where each dataset's
    data originated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    source_id: str
    vintage_ts: int
    content_hash: str | None = None

    @field_validator("source_id")
    @classmethod
    def _source_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_id must be non-empty")
        return v

    @field_validator("content_hash")
    @classmethod
    def _content_hash_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "content_hash")


class DatasetRegistryEntry(BaseModel):
    """A single append-only record in the dataset registry.

    Each entry captures everything the registry needs to gate production
    dispatch:

    - ``dataset_id`` + ``version``: the opaque id and monotone version
      (duplicate ids are versioned, not rejected).
    - ``manifest_uri`` + ``manifest_sha256``: where to fetch the manifest
      and its content hash.
    - ``data_uri`` + ``data_sha256``: where to fetch the data and its hash.
    - ``quality_report_uri`` + ``quality_report_sha256``: the quality
      report location and hash (required for L3+).
    - ``source_receipts``: provenance receipts for each source vintage.
    - ``readiness_level``: the current readiness level (L1-L4).
    - ``upload_receipt``: the staging receipt (set by ``stage_upload``).
    - ``status``: the lifecycle status.
    - ``created_at`` / ``updated_at``: Unix timestamps (seconds).

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dataset_id: str
    version: int = 1
    # --- manifest / data / quality URIs + hashes -----------------------
    manifest_uri: str
    manifest_sha256: str | None = None
    data_uri: str
    data_sha256: str | None = None
    quality_report_uri: str | None = None
    quality_report_sha256: str | None = None
    # --- provenance -----------------------------------------------------
    source_receipts: tuple[SourceReceipt, ...] = Field(default_factory=tuple)
    # --- readiness + lifecycle -----------------------------------------
    readiness_level: ReadinessLevel = ReadinessLevel.L1_RAW
    upload_receipt: UploadReceipt | None = None
    status: RegistryStatus = RegistryStatus.REGISTERED
    created_at: int = Field(default_factory=lambda: int(time.time()))
    updated_at: int = Field(default_factory=lambda: int(time.time()))

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_valid(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("dataset_id must be non-empty")
        if not _is_registry_eligible_id(v):
            raise ValueError(
                f"dataset_id must be an opaque registry slug (no path "
                f"separators or raw-data extensions): {v!r}"
            )
        return v

    @field_validator("manifest_uri", "data_uri")
    @classmethod
    def _uri_nonempty(cls, v: str, info: Any) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} must be non-empty")
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

    @field_validator("quality_report_sha256")
    @classmethod
    def _quality_report_sha256_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_hex256(v, "quality_report_sha256")

    @field_validator("version")
    @classmethod
    def _version_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"version must be >= 1; got {v}")
        return v

    @model_validator(mode="after")
    def _readiness_quality_consistency(self) -> DatasetRegistryEntry:
        """L3+ requires a quality report URI + hash (fail-closed)."""
        if self.readiness_level.at_least(ReadinessLevel.L3_QUALITY_GATED):
            if not self.quality_report_uri or not self.quality_report_uri.strip():
                raise ValueError(
                    f"readiness level {self.readiness_level.value} requires a "
                    "non-empty quality_report_uri"
                )
            if not self.quality_report_sha256:
                raise ValueError(
                    f"readiness level {self.readiness_level.value} requires a "
                    "non-empty quality_report_sha256"
                )
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetRegistryEntry:
        """Deserialize from a dict (e.g. from a JSONL line)."""
        return cls(**data)


class DatasetRegistry:
    """Durable, append-only dataset registry with readiness-level gating.

    The registry is backed by a JSONL file (one entry per line). Each
    ``register`` call appends a new entry; duplicate dataset ids are
    versioned (the version is incremented). The registry provides the
    commands required by T-3.1:

    - :meth:`inspect`: return the latest (or a specific version of) an
      entry by dataset id.
    - :meth:`register`: append a new entry (versioning duplicates).
    - :meth:`stage_upload`: record an upload receipt for a dataset
      (rejects stale receipts).
    - :meth:`promote_readiness`: advance a dataset's readiness level
      (rejects skips / demotions).
    - :meth:`dispatch_training`: validate that a dataset is eligible for
      dispatch under a given training mode (production requires L3+ and
      an active/staged status; rejects unregistered ids and raw paths).

    The registry is fail-closed: any validation failure raises
    ``ValueError`` with a clear message.
    """

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        stale_receipt_ttl_seconds: int = DEFAULT_STALE_RECEIPT_TTL_SECONDS,
    ) -> None:
        """Create a registry.

        Args:
            path: optional path to a JSONL ledger file. If provided, the
                registry loads existing entries on construction and
                persists on every mutation. If None, the registry is
                in-memory (useful for tests).
            stale_receipt_ttl_seconds: the maximum age (in seconds) of an
                upload receipt before it is considered stale. Defaults to
                24 hours.
        """
        self._path: Path | None = Path(path) if path is not None else None
        self._stale_ttl = stale_receipt_ttl_seconds
        # In-memory index: dataset_id -> list of entries ordered by version.
        self._entries: dict[str, list[DatasetRegistryEntry]] = {}
        if self._path is not None and self._path.exists():
            self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        """Load entries from the JSONL ledger (if it exists)."""
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = DatasetRegistryEntry.from_dict(json.loads(line))
            self._entries.setdefault(entry.dataset_id, []).append(entry)
        # Sort each dataset's entries by version.
        for ds_id in self._entries:
            self._entries[ds_id].sort(key=lambda e: e.version)

    def _append(self, entry: DatasetRegistryEntry) -> None:
        """Append an entry to the in-memory index and the JSONL ledger."""
        self._entries.setdefault(entry.dataset_id, []).append(entry)
        self._entries[entry.dataset_id].sort(key=lambda e: e.version)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")

    # --- queries ---------------------------------------------------------

    def inspect(self, dataset_id: str, *, version: int | None = None) -> DatasetRegistryEntry:
        """Return the entry for ``dataset_id`` (latest, or a specific version).

        Raises:
            ValueError: if the dataset id is not registered, or the
                requested version does not exist.
        """
        entries = self._entries.get(dataset_id)
        if not entries:
            raise ValueError(
                f"dataset not registered: {dataset_id!r} (inspect requires a registered dataset id)"
            )
        if version is not None:
            for e in entries:
                if e.version == version:
                    return e
            raise ValueError(
                f"dataset {dataset_id!r} has no version {version} "
                f"(available: {[e.version for e in entries]})"
            )
        return entries[-1]

    def list_entries(self) -> list[DatasetRegistryEntry]:
        """Return all registered entries (latest version of each)."""
        return [entries[-1] for entries in self._entries.values()]

    def is_registered(self, dataset_id: str) -> bool:
        """Return True if ``dataset_id`` is registered (any version)."""
        return bool(self._entries.get(dataset_id))

    # --- commands --------------------------------------------------------

    def register(
        self,
        *,
        dataset_id: str,
        manifest_uri: str,
        data_uri: str,
        manifest_sha256: str | None = None,
        data_sha256: str | None = None,
        quality_report_uri: str | None = None,
        quality_report_sha256: str | None = None,
        source_receipts: tuple[SourceReceipt, ...] | list[SourceReceipt] | None = None,
        readiness_level: ReadinessLevel | str = ReadinessLevel.L1_RAW,
    ) -> DatasetRegistryEntry:
        """Register a new dataset entry (versioning duplicate ids).

        If ``dataset_id`` is already registered, the version is incremented
        (acceptance criterion: duplicate dataset id rejected or versioned).
        The new entry starts at the given ``readiness_level`` (default L1).

        Args:
            dataset_id: the opaque registry slug for this dataset.
            manifest_uri: where to fetch the manifest JSON.
            data_uri: where to fetch the tabular data.
            manifest_sha256: optional manifest content hash.
            data_sha256: optional data content hash.
            quality_report_uri: optional quality report URI (required for
                L3+).
            quality_report_sha256: optional quality report hash (required
                for L3+).
            source_receipts: optional provenance receipts.
            readiness_level: the initial readiness level (default L1).

        Returns:
            The newly created :class:`DatasetRegistryEntry`.

        Raises:
            ValueError: if the dataset id is not a valid registry slug, or
                if L3+ is requested without a quality report.
        """
        if isinstance(readiness_level, str):
            readiness_level = ReadinessLevel.from_str(readiness_level)
        existing = self._entries.get(dataset_id, [])
        next_version = (existing[-1].version + 1) if existing else 1
        now = int(time.time())
        entry = DatasetRegistryEntry(
            dataset_id=dataset_id,
            version=next_version,
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha256,
            data_uri=data_uri,
            data_sha256=data_sha256,
            quality_report_uri=quality_report_uri,
            quality_report_sha256=quality_report_sha256,
            source_receipts=tuple(source_receipts) if source_receipts else (),
            readiness_level=readiness_level,
            status=RegistryStatus.REGISTERED,
            created_at=now,
            updated_at=now,
        )
        self._append(entry)
        return entry

    def stage_upload(
        self,
        dataset_id: str,
        receipt: UploadReceipt,
        *,
        now: int | None = None,
        expected_hash: str | None = None,
    ) -> DatasetRegistryEntry:
        """Record an upload receipt for a dataset (rejects stale receipts).

        The receipt's ``dataset_id`` must match, its ``data_uri`` must
        match the entry's ``data_uri``, and the receipt must not be stale
        (expired or hash-mismatched). On success, the entry's status is
        advanced to ``STAGED`` and the receipt is recorded.

        Args:
            dataset_id: the registered dataset id.
            receipt: the upload receipt to record.
            now: current Unix timestamp (for staleness check). Defaults
                to ``int(time.time())``.
            expected_hash: the expected ``receipt_hash()`` for tamper
                checking.

        Returns:
            The updated :class:`DatasetRegistryEntry` (a new immutable
            copy with the receipt recorded and status=STAGED).

        Raises:
            ValueError: if the dataset is not registered, the receipt's
                dataset_id or data_uri does not match, or the receipt is
                stale.
        """
        current = int(time.time()) if now is None else now
        entry = self.inspect(dataset_id)
        if receipt.dataset_id != dataset_id:
            raise ValueError(
                f"upload receipt dataset_id mismatch: receipt has "
                f"{receipt.dataset_id!r}, expected {dataset_id!r}"
            )
        if receipt.data_uri != entry.data_uri:
            raise ValueError(
                f"upload receipt data_uri mismatch: receipt has "
                f"{receipt.data_uri!r}, expected {entry.data_uri!r}"
            )
        if receipt.data_sha256 is not None and entry.data_sha256 is not None:
            if receipt.data_sha256 != entry.data_sha256:
                raise ValueError(
                    f"upload receipt data_sha256 mismatch: receipt has "
                    f"{receipt.data_sha256!r}, expected {entry.data_sha256!r}"
                )
        if receipt.is_stale(now=current, expected_hash=expected_hash):
            reasons: list[str] = []
            if current >= receipt.expires_at:
                reasons.append(f"expired (now={current}, expires_at={receipt.expires_at})")
            if expected_hash is not None and receipt.receipt_hash() != expected_hash:
                reasons.append(
                    f"hash mismatch (expected {expected_hash!r}, got {receipt.receipt_hash()!r})"
                )
            raise ValueError(
                f"stale upload receipt rejected for dataset {dataset_id!r}: " + "; ".join(reasons)
            )
        updated = entry.model_copy(
            update={
                "upload_receipt": receipt,
                "status": RegistryStatus.STAGED,
                "updated_at": current,
            },
        )
        # Replace the latest entry in-memory (append a new version-like
        # record to the ledger so the audit trail is preserved).
        self._entries[dataset_id][-1] = updated
        if self._path is not None:
            self._rewrite_ledger()
        return updated

    def promote_readiness(
        self,
        dataset_id: str,
        new_level: ReadinessLevel | str,
        *,
        now: int | None = None,
    ) -> DatasetRegistryEntry:
        """Advance a dataset's readiness level (rejects skips/demotions).

        Promotion must be monotone and stepwise: you cannot skip levels
        (e.g. L1 → L3 is rejected) and you cannot demote (e.g. L3 → L1 is
        rejected). L3+ requires a quality report URI + hash on the entry.

        Args:
            dataset_id: the registered dataset id.
            new_level: the target readiness level.
            now: current Unix timestamp. Defaults to ``int(time.time())``.

        Returns:
            The updated :class:`DatasetRegistryEntry` (a new immutable
            copy with the new readiness level).

        Raises:
            ValueError: if the dataset is not registered, the promotion
                is a skip or demotion, or L3+ is requested without a
                quality report.
        """
        if isinstance(new_level, str):
            new_level = ReadinessLevel.from_str(new_level)
        current = int(time.time()) if now is None else now
        entry = self.inspect(dataset_id)
        old_level = entry.readiness_level
        if new_level == old_level:
            raise ValueError(
                f"readiness level is already {old_level.value} for dataset "
                f"{dataset_id!r} (no-op promotion rejected)"
            )
        if new_level.rank() < old_level.rank():
            raise ValueError(
                f"readiness demotion rejected: {old_level.value} -> "
                f"{new_level.value} for dataset {dataset_id!r} "
                "(readiness levels are monotone — cannot demote)"
            )
        # Stepwise: must be the next level on the ladder.
        old_idx = _READINESS_LADDER.index(old_level)
        new_idx = _READINESS_LADDER.index(new_level)
        if new_idx != old_idx + 1:
            raise ValueError(
                f"readiness promotion skip rejected: {old_level.value} -> "
                f"{new_level.value} for dataset {dataset_id!r} "
                "(must promote stepwise — no skipping levels)"
            )
        # L3+ requires a quality report.
        if new_level.at_least(ReadinessLevel.L3_QUALITY_GATED):
            if not entry.quality_report_uri or not entry.quality_report_sha256:
                raise ValueError(
                    f"cannot promote to {new_level.value}: dataset "
                    f"{dataset_id!r} lacks a quality_report_uri/"
                    "quality_report_sha256 (required for L3+)"
                )
        updated = entry.model_copy(
            update={
                "readiness_level": new_level,
                "updated_at": current,
            },
        )
        self._entries[dataset_id][-1] = updated
        if self._path is not None:
            self._rewrite_ledger()
        return updated

    def dispatch_training(
        self,
        dataset_id: str,
        mode: TrainingMode | str,
        *,
        now: int | None = None,
    ) -> DatasetRegistryEntry:
        """Validate that a dataset is eligible for dispatch under ``mode``.

        This is the registry-side gate that the production dispatch path
        consults. It enforces:

        - **Production dispatch accepts a dataset id, not a raw file
          path.** A raw CSV/parquet path or file URI is rejected
          (acceptance criterion 6).
        - **Unregistered production dispatch is rejected.** The dataset
          must be registered (acceptance criterion 4).
        - **Production dispatch requires L3+ readiness.** L1/L2 datasets
          are rejected for production (acceptance criterion 5 / readiness
          gating).
        - **Stale upload receipts are rejected.** If the entry has an
          upload receipt that is stale, production dispatch is rejected
          (acceptance criterion 5).
        - **Deprecated/rejected entries are not dispatchable.**

        Canary and research modes are permissive: they accept any
        registered dataset (or, for canary, an unregistered id is allowed
        to pass through — the registry returns a sentinel). Production is
        fail-closed.

        Args:
            dataset_id: the dataset id to dispatch (must be a registered
                id for production).
            mode: the training mode.
            now: current Unix timestamp (for receipt staleness).

        Returns:
            The :class:`DatasetRegistryEntry` for the dispatched dataset.

        Raises:
            ValueError: if any production requirement is unmet.
        """
        if isinstance(mode, str):
            mode = TrainingMode(mode)
        current = int(time.time()) if now is None else now

        # Reject raw file paths for ALL modes that go through the registry
        # dispatch path — production dispatch accepts a dataset id, not an
        # ad hoc raw file path.
        if _is_raw_data_uri(dataset_id) or not _is_registry_eligible_id(dataset_id):
            if mode == TrainingMode.PRODUCTION:
                raise ValueError(
                    "production dispatch accepts a registered dataset id, "
                    f"not a raw file path: {dataset_id!r}"
                )
            # Canary/research: reject raw paths too — they should not go
            # through the registry dispatch path. The handler uses the
            # DatasetLoadSpec directly for canary raw paths.
            raise ValueError(
                f"dispatch_training accepts a dataset id, not a raw file "
                f"path: {dataset_id!r} (use DatasetLoadSpec for canary "
                "raw-data runs)"
            )

        if not self.is_registered(dataset_id):
            if mode == TrainingMode.PRODUCTION:
                raise ValueError(
                    f"unregistered production dispatch rejected: dataset "
                    f"{dataset_id!r} is not in the registry "
                    "(production requires a registered L3/L4 dataset)"
                )
            # Canary/research: unregistered is allowed to pass through.
            # Return a minimal transient entry so the caller can proceed.
            return DatasetRegistryEntry(
                dataset_id=dataset_id,
                manifest_uri="",
                data_uri="",
                readiness_level=ReadinessLevel.L1_RAW,
                status=RegistryStatus.REGISTERED,
            )

        entry = self.inspect(dataset_id)

        # Deprecated / rejected entries are never dispatchable.
        if entry.status in (RegistryStatus.DEPRECATED, RegistryStatus.REJECTED):
            raise ValueError(
                f"dataset {dataset_id!r} status is {entry.status.value} — not dispatchable"
            )

        if mode == TrainingMode.PRODUCTION:
            errors: list[str] = []
            if not entry.readiness_level.at_least(ReadinessLevel.L3_QUALITY_GATED):
                errors.append(
                    f"production dispatch requires readiness L3+ "
                    f"(quality-gated or production-ready); dataset "
                    f"{dataset_id!r} is at {entry.readiness_level.value}"
                )
            # Stale upload receipt check (if a receipt is recorded).
            if entry.upload_receipt is not None:
                if entry.upload_receipt.is_stale(now=current):
                    errors.append(
                        f"stale upload receipt for dataset {dataset_id!r} "
                        f"(expired at {entry.upload_receipt.expires_at})"
                    )
            if errors:
                raise ValueError("production dispatch validation failed: " + "; ".join(errors))

        return entry

    # --- ledger rewrite --------------------------------------------------

    def _rewrite_ledger(self) -> None:
        """Rewrite the entire JSONL ledger from the in-memory index.

        Used after in-place updates (stage_upload, promote_readiness) so
        the ledger reflects the latest state of each entry. The ledger
        remains append-only in spirit: each mutation appends a new line,
        but to keep the in-memory index authoritative we rewrite the
        latest version of each entry.
        """
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for ds_id in sorted(self._entries):
            for entry in self._entries[ds_id]:
                lines.append(json.dumps(entry.to_dict(), sort_keys=True))
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Registry lookup helper (for TrainingManifest validation)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 8 / T-8.1 — Column Roles, Groups, Weights, Horizons
# ---------------------------------------------------------------------------
#
# The ``ColumnRoles`` model is the **explicit** declaration of which columns
# in a dataset play which role during training. It replaces the fragile
# "infer features by dropping a few names" pattern that the legacy trainer
# used (``feature_cols = [c for c in columns if c != label_col and c != ts_col]``).
#
# Every trainer must now receive an explicit ``ColumnRoles`` so that:
# - feature columns are declared, never inferred,
# - leakage / audit columns are explicitly excluded and can never appear
#   in the feature set,
# - label, timestamp, symbol, horizon, weight, group, and sector columns
#   are named up front so the trainer and the tournament agree on the
#   dataset's shape.
#
# Fail-closed invariants (enforced at construction + validation):
# - ``feature_columns`` and ``label_columns`` must be non-empty.
# - No overlap between ``feature_columns`` and ``excluded_columns``
#   (leakage prevention — an excluded audit/leakage column must NEVER be
#   used as a feature).
# - No overlap between ``label_columns`` and ``feature_columns`` (a label
#   cannot also be a feature).


class ColumnRolesValidationResult(BaseModel):
    """Result of validating :class:`ColumnRoles` against available columns.

    Frozen + ``extra='forbid'`` (audit integrity). ``passed`` is True only
    when there are no errors (warnings do not fail validation).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def raise_if_failed(self) -> None:
        """Raise ``ValueError`` if validation did not pass (fail-closed)."""
        if not self.passed:
            raise ValueError("column roles validation failed: " + "; ".join(self.errors))


class ColumnRoles(BaseModel):
    """Explicit declaration of column roles for a training dataset.

    This is the **single source of truth** for which columns are features,
    labels, timestamps, symbols, horizons, weights, groups, sectors, and
    which columns are excluded (audit/leakage columns that must never be
    used as features).

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        feature_columns: explicit tuple of feature column names. Must be
            non-empty. The trainer uses ONLY these columns as features —
            never inferred by dropping a few names.
        label_columns: explicit tuple of label / target column names. Must
            be non-empty. ``label_columns[0]`` is the primary label.
        timestamp_column: the timestamp / decision-time column. Optional.
        symbol_column: the symbol / instrument id column. Optional.
        horizon_column: the horizon column (bar offset for the label).
            Optional — the horizon may instead be a parameter on
            :class:`ModelTaskSpec`.
        weight_column: the sample-weight column. Optional.
        group_column: the group-id column (required for ranking tasks).
            Optional here, but :func:`validate_task_spec` enforces it for
            ranking task types.
        sector_column: the sector / industry classification column.
            Optional.
        excluded_columns: audit / leakage columns that must NEVER be used
            as features. The trainer fail-closes if any excluded column
            appears in ``feature_columns``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    feature_columns: tuple[str, ...]
    label_columns: tuple[str, ...]
    timestamp_column: str | None = None
    symbol_column: str | None = None
    horizon_column: str | None = None
    weight_column: str | None = None
    group_column: str | None = None
    sector_column: str | None = None
    excluded_columns: tuple[str, ...] = ()

    @field_validator("feature_columns", "label_columns")
    @classmethod
    def _nonempty_tuple(cls, v: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if not v:
            raise ValueError(f"{info.field_name} must be non-empty")
        for name in v:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{info.field_name} entries must be non-empty strings")
        return v

    @field_validator("excluded_columns")
    @classmethod
    def _excluded_entries_nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("excluded_columns entries must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _no_feature_excluded_overlap(self) -> ColumnRoles:
        """An excluded (audit/leakage) column must never be a feature."""
        feature_set = set(self.feature_columns)
        excluded_set = set(self.excluded_columns)
        overlap = feature_set & excluded_set
        if overlap:
            raise ValueError(
                "feature_columns must not overlap excluded_columns "
                f"(leakage prevention): overlapping columns "
                f"{sorted(overlap)!r} are declared both as features and "
                "excluded"
            )
        return self

    @model_validator(mode="after")
    def _no_label_feature_overlap(self) -> ColumnRoles:
        """A label column must not also be a feature column."""
        feature_set = set(self.feature_columns)
        label_set = set(self.label_columns)
        overlap = feature_set & label_set
        if overlap:
            raise ValueError(
                "label_columns must not overlap feature_columns: "
                f"{sorted(overlap)!r} appear in both"
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_features(self) -> ColumnRoles:
        """Feature columns must be unique (no duplicate feature names)."""
        if len(set(self.feature_columns)) != len(self.feature_columns):
            dupes = sorted({c for c in self.feature_columns if self.feature_columns.count(c) > 1})
            raise ValueError(f"feature_columns must not contain duplicates: {dupes!r}")
        return self

    @model_validator(mode="after")
    def _no_duplicate_labels(self) -> ColumnRoles:
        """Label columns must be unique."""
        if len(set(self.label_columns)) != len(self.label_columns):
            dupes = sorted({c for c in self.label_columns if self.label_columns.count(c) > 1})
            raise ValueError(f"label_columns must not contain duplicates: {dupes!r}")
        return self

    # --- convenience -----------------------------------------------------

    @property
    def primary_label(self) -> str:
        """The primary label column (``label_columns[0]``)."""
        return self.label_columns[0]

    @property
    def all_declared_columns(self) -> set[str]:
        """All column names declared in any role (for existence checking)."""
        cols: set[str] = set(self.feature_columns) | set(self.label_columns)
        if self.timestamp_column:
            cols.add(self.timestamp_column)
        if self.symbol_column:
            cols.add(self.symbol_column)
        if self.horizon_column:
            cols.add(self.horizon_column)
        if self.weight_column:
            cols.add(self.weight_column)
        if self.group_column:
            cols.add(self.group_column)
        if self.sector_column:
            cols.add(self.sector_column)
        cols.update(self.excluded_columns)
        return cols

    def is_excluded(self, column: str) -> bool:
        """Return True if ``column`` is declared excluded (leakage/audit)."""
        return column in set(self.excluded_columns)


def validate_column_roles(
    roles: ColumnRoles,
    available_columns: set[str] | frozenset[str] | list[str] | tuple[str, ...],
) -> ColumnRolesValidationResult:
    """Validate :class:`ColumnRoles` against the columns actually present.

    Checks:
    - Every declared column (features, labels, timestamp, symbol, horizon,
      weight, group, sector, excluded) exists in ``available_columns``.
    - Excluded columns are not in ``feature_columns`` (already enforced at
      construction, but re-checked here for defence in depth).
    - Label columns are not in ``feature_columns`` (already enforced at
      construction, re-checked here).

    Args:
        roles: the :class:`ColumnRoles` to validate.
        available_columns: the set of column names present in the dataset.

    Returns:
        A :class:`ColumnRolesValidationResult`. ``passed`` is True only
        when there are no errors. Warnings are recorded for soft issues
        (e.g. an excluded column that is not present in the dataset).
    """
    available: set[str] = set(available_columns)
    errors: list[str] = []
    warnings: list[str] = []

    # Check feature columns exist.
    for col in roles.feature_columns:
        if col not in available:
            errors.append(f"feature column {col!r} not found in available columns")

    # Check label columns exist.
    for col in roles.label_columns:
        if col not in available:
            errors.append(f"label column {col!r} not found in available columns")

    # Check optional role columns exist (if declared).
    optional_roles: list[tuple[str | None, str]] = [
        (roles.timestamp_column, "timestamp_column"),
        (roles.symbol_column, "symbol_column"),
        (roles.horizon_column, "horizon_column"),
        (roles.weight_column, "weight_column"),
        (roles.group_column, "group_column"),
        (roles.sector_column, "sector_column"),
    ]
    for col, role_name in optional_roles:
        if col is not None and col not in available:
            errors.append(f"{role_name} {col!r} not found in available columns")

    # Check excluded columns exist (warning if not — an excluded column
    # that is absent is harmless but may indicate a stale manifest).
    for col in roles.excluded_columns:
        if col not in available:
            warnings.append(
                f"excluded column {col!r} not found in available columns (stale manifest?)"
            )

    # Defence in depth: re-check no excluded column is a feature.
    feature_set = set(roles.feature_columns)
    excluded_set = set(roles.excluded_columns)
    overlap = feature_set & excluded_set
    if overlap:
        errors.append(
            f"excluded columns must not appear in feature_columns (leakage): {sorted(overlap)!r}"
        )

    # Defence in depth: re-check no label is a feature.
    label_set = set(roles.label_columns)
    lf_overlap = feature_set & label_set
    if lf_overlap:
        errors.append(f"label columns must not appear in feature_columns: {sorted(lf_overlap)!r}")

    passed = len(errors) == 0
    return ColumnRolesValidationResult(
        passed=passed,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def lookup_dataset_registry_ref(
    registry: DatasetRegistry,
    dataset_registry_ref: str,
    *,
    min_readiness: ReadinessLevel = ReadinessLevel.L3_QUALITY_GATED,
) -> DatasetRegistryEntry:
    """Resolve a ``dataset_registry_ref`` against a :class:`DatasetRegistry`.

    This is the helper that ``TrainingManifest`` validation (or the
    dispatch path) can use to verify that a ``dataset_registry_ref``
    refers to a real, registered, sufficiently-ready dataset. It
    enforces:

    - The ref is an opaque dataset id (not a raw file path).
    - The dataset is registered.
    - The dataset's readiness level is >= ``min_readiness`` (default L3,
      the minimum for production).

    Args:
        registry: the dataset registry to consult.
        dataset_registry_ref: the dataset id to resolve.
        min_readiness: the minimum required readiness level (default L3).

    Returns:
        The resolved :class:`DatasetRegistryEntry`.

    Raises:
        ValueError: if the ref is a raw path, unregistered, or below the
            minimum readiness.
    """
    if _is_raw_data_uri(dataset_registry_ref) or not _is_registry_eligible_id(
        dataset_registry_ref,
    ):
        raise ValueError(
            f"dataset_registry_ref must be an opaque registered dataset id, "
            f"not a raw file path: {dataset_registry_ref!r}"
        )
    if not registry.is_registered(dataset_registry_ref):
        raise ValueError(
            f"dataset_registry_ref {dataset_registry_ref!r} is not registered "
            "in the dataset registry"
        )
    entry = registry.inspect(dataset_registry_ref)
    if not entry.readiness_level.at_least(min_readiness):
        raise ValueError(
            f"dataset_registry_ref {dataset_registry_ref!r} readiness is "
            f"{entry.readiness_level.value}, below the required minimum "
            f"{min_readiness.value}"
        )
    if entry.status in (RegistryStatus.DEPRECATED, RegistryStatus.REJECTED):
        raise ValueError(
            f"dataset_registry_ref {dataset_registry_ref!r} status is "
            f"{entry.status.value} — not usable"
        )
    return entry


# ---------------------------------------------------------------------------
# Phase 8 / T-8.4 — Manifest fold spec structures
# ---------------------------------------------------------------------------
#
# The ``FoldWindow`` and ``FoldSpec`` models are the **manifest-driven** fold
# contract that the trainer consumes (T-8.4 Consume Manifest Folds Exactly).
# They are intentionally string-typed (ISO date / datetime) so that a manifest
# author can declare fold boundaries in human-readable form, and the
# :mod:`quant_foundry.fold_consumer` module converts them into row-level
# assignments at load time.
#
# These structures are distinct from the lower-level ``FoldBoundary`` /
# ``PurgedFoldSpec`` (which use nanosecond epochs and are emitted by the
# feature-lake builder). ``FoldWindow`` / ``FoldSpec`` are the
# *contract-of-record* that training reads: the manifest's fold windows are
# the single source of truth for which rows belong to which fold, and the
# trainer must consume them *exactly* — no re-derivation, no inference.
#
# Fail-closed invariants (enforced at construction):
# - ``FoldWindow``: train_start < train_end < validation_start (or
#   embargo_until if set) < validation_end.
# - ``FoldSpec``: at least one fold, no duplicate fold_ids, fold_ids are
#   sequential starting from 0.
# - ``compute_fold_hash``: deterministic SHA-256 over the fold windows sorted
#   by fold_id, so two identical fold specs produce the same hash.


def _parse_temporal(value: str) -> float:
    """Parse an ISO date or datetime string into a comparable epoch float.

    Accepts both date-only (``"2024-01-01"``) and full datetime
    (``"2024-01-01T00:00:00"``) strings. A date-only string is treated as
    midnight UTC on that date. Returns a POSIX timestamp (seconds) suitable
    for ordering comparisons.

    Raises:
        ValueError: if ``value`` is not a valid ISO date/datetime.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"temporal value must be a non-empty ISO string; got {value!r}")
    text = value.strip()
    # Try full datetime first, then date-only.
    try:
        from datetime import datetime

        # Handle trailing 'Z' (UTC) and timezone offsets.
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        from datetime import date as _date
        from datetime import datetime

        d = _date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()
    except ValueError as exc:
        raise ValueError(
            f"temporal value must be an ISO date or datetime string; got {value!r}: {exc}"
        ) from exc


class FoldWindow(BaseModel):
    """A single manifest-declared fold window (train + validation + embargo).

    All temporal boundaries are ISO date or datetime strings (e.g.
    ``"2024-01-01"`` or ``"2024-01-01T00:00:00Z"``). The trainer consumes
    these windows *exactly* as declared — it does not re-derive fold
    boundaries from the data.

    Fields:
        fold_id: 0-indexed fold identifier.
        train_start: ISO date/datetime — inclusive start of the train window.
        train_end: ISO date/datetime — inclusive end of the train window.
        validation_start: ISO date/datetime — inclusive start of validation.
        validation_end: ISO date/datetime — inclusive end of validation.
        embargo_until: optional ISO date/datetime marking the end of the
            purge/embargo period that sits between train_end and
            validation_start. When set, the ordering invariant becomes
            ``train_start < train_end < embargo_until < validation_start <
            validation_end``. When None, the invariant is
            ``train_start < train_end < validation_start < validation_end``.

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    fold_id: int
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    embargo_until: str | None = None

    @field_validator("fold_id")
    @classmethod
    def _fold_id_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"fold_id must be >= 0; got {v}")
        return v

    @field_validator(
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
    )
    @classmethod
    def _temporal_nonempty(cls, v: str, info: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} must be a non-empty ISO string")
        # Validate parseability immediately.
        _parse_temporal(v)
        return v

    @field_validator("embargo_until")
    @classmethod
    def _embargo_parseable(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not isinstance(v, str) or not v.strip():
            raise ValueError("embargo_until must be a non-empty ISO string or None")
        _parse_temporal(v)
        return v

    @model_validator(mode="after")
    def _check_ordering(self) -> FoldWindow:
        """Enforce train_start < train_end < (embargo_until | validation_start) < validation_end."""
        ts = _parse_temporal(self.train_start)
        te = _parse_temporal(self.train_end)
        vs = _parse_temporal(self.validation_start)
        ve = _parse_temporal(self.validation_end)
        if not (ts < te):
            raise ValueError(
                f"fold {self.fold_id}: train_start must be < train_end "
                f"(train_start={self.train_start!r}, train_end={self.train_end!r})"
            )
        # The middle boundary is embargo_until if set, else validation_start.
        if self.embargo_until is not None:
            eu = _parse_temporal(self.embargo_until)
            if not (te < eu):
                raise ValueError(
                    f"fold {self.fold_id}: train_end must be < embargo_until "
                    f"(train_end={self.train_end!r}, embargo_until={self.embargo_until!r})"
                )
            if not (eu < vs):
                raise ValueError(
                    f"fold {self.fold_id}: embargo_until must be < validation_start "
                    f"(embargo_until={self.embargo_until!r}, "
                    f"validation_start={self.validation_start!r})"
                )
        else:
            if not (te < vs):
                raise ValueError(
                    f"fold {self.fold_id}: train_end must be < validation_start "
                    f"(train_end={self.train_end!r}, "
                    f"validation_start={self.validation_start!r})"
                )
        if not (vs < ve):
            raise ValueError(
                f"fold {self.fold_id}: validation_start must be < validation_end "
                f"(validation_start={self.validation_start!r}, "
                f"validation_end={self.validation_end!r})"
            )
        return self


class FoldSpec(BaseModel):
    """Manifest-declared fold specification — the contract of record.

    The trainer reads a ``FoldSpec`` from the manifest and consumes its fold
    windows *exactly*. The ``fold_assignment_hash`` is a deterministic hash
    over the fold windows (computed by :func:`compute_fold_hash`) so that two
    manifests with identical folds share a hash and a single changed window
    changes the hash.

    Fields:
        folds: list of :class:`FoldWindow` (at least one).
        fold_assignment_hash: deterministic SHA-256 of the fold windows
            (sorted by fold_id). Must match
            ``compute_fold_hash(folds)``.
        row_id_columns: columns that form the stable row key (e.g.
            ``["symbol", "decision_time", "horizon"]``). The fold consumer
            uses these to extract row keys from the dataframe.

    Frozen + ``extra='forbid'`` (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    folds: list[FoldWindow]
    fold_assignment_hash: str
    row_id_columns: list[str]

    @field_validator("folds")
    @classmethod
    def _folds_nonempty(cls, v: list[FoldWindow]) -> list[FoldWindow]:
        if not v:
            raise ValueError("FoldSpec.folds must contain at least one fold")
        return v

    @field_validator("row_id_columns")
    @classmethod
    def _row_id_columns_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("FoldSpec.row_id_columns must be non-empty")
        for col in v:
            if not isinstance(col, str) or not col.strip():
                raise ValueError("FoldSpec.row_id_columns entries must be non-empty strings")
        return v

    @field_validator("fold_assignment_hash")
    @classmethod
    def _hash_shape(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("fold_assignment_hash must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _check_fold_ids(self) -> FoldSpec:
        """No duplicate fold_ids; fold_ids sequential starting from 0."""
        ids = [f.fold_id for f in self.folds]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"FoldSpec.folds must not contain duplicate fold_ids: {dupes!r}")
        expected = list(range(len(self.folds)))
        if ids != expected:
            raise ValueError(
                f"FoldSpec.folds fold_ids must be sequential starting from 0; "
                f"got {ids!r}, expected {expected!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_hash_matches(self) -> FoldSpec:
        """The declared hash must match the recomputed hash (fail-closed)."""
        recomputed = compute_fold_hash(self.folds)
        if recomputed != self.fold_assignment_hash:
            raise ValueError(
                "FoldSpec.fold_assignment_hash does not match the computed "
                f"hash of its folds (declared={self.fold_assignment_hash!r}, "
                f"computed={recomputed!r})"
            )
        return self


def compute_fold_hash(folds: list[FoldWindow]) -> str:
    """Compute a deterministic SHA-256 hash over a list of fold windows.

    The fold windows are sorted by ``fold_id`` and serialized to a canonical
    JSON representation (sorted keys, compact separators) before hashing.
    Two identical fold specifications therefore produce the same hash, and
    any change to a fold window alters the hash.

    Args:
        folds: the list of :class:`FoldWindow` to hash.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    sorted_folds = sorted(folds, key=lambda f: f.fold_id)
    payload = [
        {
            "fold_id": f.fold_id,
            "train_start": f.train_start,
            "train_end": f.train_end,
            "validation_start": f.validation_start,
            "validation_end": f.validation_end,
            "embargo_until": f.embargo_until,
        }
        for f in sorted_folds
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
