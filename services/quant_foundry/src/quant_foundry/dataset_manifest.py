"""
quant_foundry.dataset_manifest — point-in-time dataset manifest for the feature lake.

TASK-0405: Build Feature Lake Builder MVP.

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
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class FeatureLakeManifest(BaseModel):
    """Point-in-time dataset manifest emitted by the feature lake builder.

    This is the *only* thing a RunPod training worker references — never a DB
    connection. The ``manifest_hash`` is a stable content hash over every field
    that affects reproducibility, so two exports of the same PIT dataset
    produce the same hash and a single changed row changes the hash.
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
