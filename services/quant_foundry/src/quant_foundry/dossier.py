"""
quant_foundry.dossier â€” model dossier record + builder (TASK-0403).

A "dossier" is the complete reproducibility + evaluation record for a candidate
model. It is the core of the promotion decision: without a dossier, a model
artifact is an opaque file. With a dossier, every model is reviewable and
promotable only with evidence.

What a dossier carries (cross-cutting rigor Â§3 â€” reproducibility):
- dataset / feature / label schema hashes
- code git SHA, lockfile hash, container image digest
- random seed(s), hardware class
- the artifact manifest id + artifact sha256 it was built from
- training metrics (accuracy, brier, etc.)
- **trial_count** for the model family (so the tournament can deflate Sharpe â€”
  cross-cutting rigor Â§2)
- **blocking_issues** list that the sentinel (TASK-0406) and tournament
  (TASK-0404) write into; a blocking issue is a hard gate on promotion
- **status** â€” the promotion lifecycle state

Invariants:
- ``DossierRecord`` is frozen + extra='forbid' (audit integrity).
- ``content_hash`` is a deterministic hash of the record's canonical JSON; it is
  the immutability key used by the registry (same content -> same hash; a content
  change -> a different hash -> a new version).
- A dossier references settlement evidence and shadow predictions by id/ref, NOT
  by importing ``settlement.py`` / ``shadow_ledger.py`` (keeps file-disjoint from
  Builder 1's tracks).

File-disjoint from all active builders (see BUILDER3.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_foundry.artifacts import ArtifactRecord
from quant_foundry.ids import hash_payload


class DossierStatus(StrEnum):
    """Promotion lifecycle states for a model dossier.

    A model moves candidate -> research_approved -> shadow_approved -> paper_approved
    -> limited_live_approved only with dossiers, settlement evidence, tournament
    scores, receipts, and human approval. ``rejected`` and ``retired`` are
    terminal â€” no promotion path out of either.
    """

    CANDIDATE = "candidate"
    RESEARCH_APPROVED = "research_approved"
    SHADOW_APPROVED = "shadow_approved"
    PAPER_APPROVED = "paper_approved"
    LIMITED_LIVE_APPROVED = "limited_live_approved"
    REJECTED = "rejected"
    # C7: retired is terminal â€” no promotion path out of retired.
    RETIRED = "retired"


class DossierRecord(BaseModel):
    """Immutable, reproducibility-complete record for a candidate model.

    Frozen + extra='forbid'. ``content_hash`` is computed at construction from the
    canonical JSON of the record (excluding ``content_hash`` itself and
    ``blocking_issues`` / ``registered_at_ns`` which are registry-managed) so that
    the registry can enforce immutability-by-version/hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    model_id: str
    artifact_manifest_id: str
    artifact_sha256: str
    dataset_manifest_id: str
    dataset_manifest_ref: str | None = None
    feature_schema_hash: str
    label_schema_hash: str
    code_git_sha: str | None = None
    lockfile_hash: str | None = None
    container_image_digest: str | None = None
    random_seed: int | None = None
    hardware_class: str | None = None
    trial_count: int = 1
    training_metrics: dict[str, float] = Field(default_factory=dict)
    status: DossierStatus = DossierStatus.CANDIDATE
    # Evidence refs (logical links, not code imports):
    settlement_evidence_refs: list[str] = Field(default_factory=list)
    shadow_prediction_refs: list[str] = Field(default_factory=list)
    # Registry-managed (set by DossierRegistry, not by the builder):
    blocking_issues: list[dict[str, Any]] = Field(default_factory=list)
    registered_at_ns: int | None = None
    # Immutability key â€” computed from the canonical content (see _compute_content_hash).
    content_hash: str = ""

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id must be non-empty")
        return v

    @field_validator("trial_count")
    @classmethod
    def _trial_count_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("trial_count must be >= 0")
        return v

    def model_post_init(self, __context: Any) -> None:
        """Always (re)compute ``content_hash`` from the canonical content.

        Uses ``object.__setattr__`` because the model is frozen. The content hash
        is ALWAYS derived from the record's content â€” never trusted from input â€”
        so a ``model_copy`` that changes content fields produces a new hash, and a
        reload from JSON recomputes the same hash (content is unchanged). The hash
        excludes ``content_hash`` (self), ``blocking_issues``, and
        ``registered_at_ns`` so that registry-managed fields do not affect the
        immutability key.
        """
        ch = _compute_content_hash(self)
        object.__setattr__(self, "content_hash", ch)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> DossierRecord:
        """Override to recompute ``content_hash`` after a copy.

        Pydantic v2's ``model_copy`` does NOT re-run ``model_post_init``, so without
        this override a copy that changes content fields would carry the stale
        ``content_hash`` â€” breaking the immutability invariant. We recompute it here.
        """
        copied = super().model_copy(update=update, deep=deep)
        ch = _compute_content_hash(copied)
        object.__setattr__(copied, "content_hash", ch)
        return copied


def _compute_content_hash(record: DossierRecord) -> str:
    """Deterministic sha256 of the dossier's canonical content (excluding registry-managed fields)."""
    import json

    content = {
        "schema_version": record.schema_version,
        "model_id": record.model_id,
        "artifact_manifest_id": record.artifact_manifest_id,
        "artifact_sha256": record.artifact_sha256,
        "dataset_manifest_id": record.dataset_manifest_id,
        "dataset_manifest_ref": record.dataset_manifest_ref,
        "feature_schema_hash": record.feature_schema_hash,
        "label_schema_hash": record.label_schema_hash,
        "code_git_sha": record.code_git_sha,
        "lockfile_hash": record.lockfile_hash,
        "container_image_digest": record.container_image_digest,
        "random_seed": record.random_seed,
        "hardware_class": record.hardware_class,
        "trial_count": record.trial_count,
        "training_metrics": dict(sorted(record.training_metrics.items())),
        "status": record.status.value,
        "settlement_evidence_refs": list(record.settlement_evidence_refs),
        "shadow_prediction_refs": list(record.shadow_prediction_refs),
    }
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hash_payload(payload)


class DossierBuilder:
    """Assemble a ``DossierRecord`` from an artifact + dataset manifest ref + training metadata.

    The builder pulls reproducibility fields from the ``ArtifactRecord`` so the
    dossier and artifact cannot drift. It defaults ``status`` to ``CANDIDATE`` and
    ``trial_count`` to 1.
    """

    def build(
        self,
        *,
        artifact: ArtifactRecord,
        model_id: str,
        dataset_manifest_id: str,
        dataset_manifest_ref: str | None = None,
        random_seed: int | None = None,
        hardware_class: str | None = None,
        trial_count: int = 1,
        training_metrics: dict[str, float] | None = None,
        settlement_evidence_refs: list[str] | None = None,
        shadow_prediction_refs: list[str] | None = None,
        status: DossierStatus = DossierStatus.CANDIDATE,
    ) -> DossierRecord:
        if not model_id or not model_id.strip():
            raise ValueError("model_id must be non-empty")
        return DossierRecord(
            model_id=model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=dataset_manifest_id,
            dataset_manifest_ref=dataset_manifest_ref,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=artifact.code_git_sha,
            lockfile_hash=artifact.lockfile_hash,
            container_image_digest=artifact.container_image_digest,
            random_seed=random_seed,
            hardware_class=hardware_class,
            trial_count=trial_count,
            training_metrics=dict(training_metrics or {}),
            settlement_evidence_refs=list(settlement_evidence_refs or []),
            shadow_prediction_refs=list(shadow_prediction_refs or []),
            status=status,
        )
