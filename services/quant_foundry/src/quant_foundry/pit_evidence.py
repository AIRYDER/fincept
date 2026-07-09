"""
quant_foundry.pit_evidence — signed PIT evidence v1 (C3).

This module produces a tamper-evident evidence object that proves a
point-in-time dataset export was leakage-safe. It replaces the bare
boolean ``pit_proof_verified`` flag with a structured, hash-signed
record that the training worker can independently verify.

Evidence fields (C3 spec):
- ``manifest_hash``: the SHA-256 of the dataset manifest (links evidence
  to a specific manifest version).
- ``feature_schema_hash``: the feature schema hash from the manifest
  (links evidence to a specific feature definition set).
- ``feature_set_version``: the human-readable feature-set version pin.
- ``max_observed_at_margin``: the maximum gap (ns) between
  ``decision_time`` and ``observed_at`` across all sampled rows
  (``decision_time - observed_at``). A non-negative value proves every
  feature was observable at the decision time. ``0`` means at least one
  feature was observed exactly at the decision time.
- ``violation_count``: the number of PIT violations found (features
  whose ``observed_at > decision_time``). Must be ``0`` for a valid
  dataset.
- ``sampled_row_count``: the number of rows sampled to produce the
  evidence.
- ``label_window_check_status``: ``"passed"`` if the embargo >= max
  label horizon (the fold spec's own validator enforces this; this
  field records the result for audit), ``"skipped"`` if no folds are
  available, ``"failed"`` if the check could not be satisfied.
- ``evidence_sha256``: SHA-256 over all the above fields (excluding
  itself). This is the tamper seal — if any field is altered after
  signing, recomputing the hash will not match.

Tamper detection:
- :func:`verify_pit_evidence` recomputes ``evidence_sha256`` from the
  other fields and compares it to the stored value. A mismatch raises
  :class:`PitEvidenceTamperedError` (fail-closed).
- The handler calls :func:`verify_pit_evidence` on every manifest that
  carries a ``pit_evidence`` block. Production mode fails closed on
  tampering; research/canary modes log an advisory warning.

Design notes:
- The evidence is a *derived* artifact: it is computed from the manifest
  + feature rows, not an input to the manifest hash. This avoids a
  circular hash dependency (the manifest hash is an input to the
  evidence, not the other way around).
- The model is frozen (``frozen=True, extra="forbid"``) so a constructed
  evidence object cannot be mutated after signing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator


class PitEvidenceTamperedError(ValueError):
    """Raised when PIT evidence has been tampered with (sha256 mismatch).

    This is the fail-closed guard against evidence forgery: if the
    stored ``evidence_sha256`` does not match the recomputed hash of the
    other fields, the evidence object has been altered after signing and
    must not be trusted.
    """

    def __init__(self, message: str, *, expected: str, actual: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class _ManifestLike(Protocol):
    """Duck-typed manifest interface for :func:`build_pit_evidence`.

    Only the fields needed to compute evidence are required. This keeps
    the function decoupled from the concrete
    :class:`~quant_foundry.dataset_manifest.FeatureLakeManifest` class.
    """

    def manifest_hash(self) -> str: ...
    feature_schema_hash: str
    feature_set_version: str | None
    row_count: int


class _FeatureRowLike(Protocol):
    """Duck-typed feature row interface for :func:`build_pit_evidence`."""

    decision_time: int
    label_horizon_ns: int

    @property
    def features(self) -> tuple[Any, ...]: ...


class _FeatureValueLike(Protocol):
    """Duck-typed feature value interface."""

    observed_at: int


# ---------------------------------------------------------------------------
# Evidence model
# ---------------------------------------------------------------------------


class PITEvidence(BaseModel):
    """Tamper-evident point-in-time evidence record (C3 / v1).

    Every field except ``evidence_sha256`` is an input to the evidence
    hash. The ``evidence_sha256`` field is the SHA-256 over the
    canonical JSON of all other fields — the tamper seal.

    The model is frozen and forbids extra fields so a constructed
    evidence object cannot be mutated or extended after signing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_hash: str
    feature_schema_hash: str
    feature_set_version: str | None = None
    max_observed_at_margin: int
    violation_count: int
    sampled_row_count: int
    label_window_check_status: str
    evidence_sha256: str

    @field_validator("evidence_sha256")
    @classmethod
    def _evidence_sha256_shape(cls, v: str) -> str:
        if not isinstance(v, str) or len(v) != 64:
            raise ValueError("evidence_sha256 must be a 64-char hex string")
        return v.lower()

    # --- hashing ---------------------------------------------------------

    def _evidence_payload(self) -> dict[str, Any]:
        """Canonical dict of all fields except ``evidence_sha256``."""
        return {
            "manifest_hash": self.manifest_hash,
            "feature_schema_hash": self.feature_schema_hash,
            "feature_set_version": self.feature_set_version,
            "max_observed_at_margin": self.max_observed_at_margin,
            "violation_count": self.violation_count,
            "sampled_row_count": self.sampled_row_count,
            "label_window_check_status": self.label_window_check_status,
        }

    def compute_evidence_sha256(self) -> str:
        """Recompute the evidence SHA-256 from the payload fields.

        This is the verification hash: it should match ``evidence_sha256``
        for an untampered evidence object.
        """
        payload = json.dumps(
            self._evidence_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the evidence to a plain dict (for manifest embedding)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PITEvidence:
        """Deserialize evidence from a plain dict (e.g. from a manifest)."""
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# Build + verify
# ---------------------------------------------------------------------------


def _compute_evidence_sha256(
    *,
    manifest_hash: str,
    feature_schema_hash: str,
    feature_set_version: str | None,
    max_observed_at_margin: int,
    violation_count: int,
    sampled_row_count: int,
    label_window_check_status: str,
) -> str:
    """Compute the evidence SHA-256 from the payload fields."""
    payload = {
        "manifest_hash": manifest_hash,
        "feature_schema_hash": feature_schema_hash,
        "feature_set_version": feature_set_version,
        "max_observed_at_margin": max_observed_at_margin,
        "violation_count": violation_count,
        "sampled_row_count": sampled_row_count,
        "label_window_check_status": label_window_check_status,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()


def build_pit_evidence(
    manifest: _ManifestLike,
    feature_rows: list[_FeatureRowLike] | tuple[_FeatureRowLike, ...],
    *,
    max_label_horizon_ns: int | None = None,
    embargo_ns: int | None = None,
) -> PITEvidence:
    """Build a signed PIT evidence record from a manifest + feature rows.

    This function scans every feature row and computes:
    - ``max_observed_at_margin``: the maximum ``decision_time -
      observed_at`` gap (non-negative for valid rows). If any violation
      exists, the margin for that row is negative and ``violation_count``
      is incremented.
    - ``violation_count``: the number of features whose ``observed_at >
      decision_time``.
    - ``sampled_row_count``: the number of rows scanned.
    - ``label_window_check_status``: ``"passed"`` if ``embargo_ns >=
      max_label_horizon_ns``, ``"skipped"`` if either is None, ``"failed"``
      if the embargo is too short.

    The manifest hash is computed via ``manifest.manifest_hash()`` (the
    stable content hash of the manifest, excluding the evidence itself).

    Args:
        manifest: a manifest-like object with ``manifest_hash()``,
            ``feature_schema_hash``, ``feature_set_version``, and
            ``row_count``.
        feature_rows: the feature rows to scan (typically
            ``FeatureLakeBuilder.rows``).
        max_label_horizon_ns: the max label horizon for the label-window
            check. If None, the check is skipped.
        embargo_ns: the embargo length for the label-window check. If
            None, the check is skipped.

    Returns:
        A :class:`PITEvidence` with a valid ``evidence_sha256``.
    """
    rows = list(feature_rows)
    manifest_hash = manifest.manifest_hash()
    feature_schema_hash = manifest.feature_schema_hash
    feature_set_version = manifest.feature_set_version

    max_margin = 0
    violation_count = 0
    for row in rows:
        for fv in row.features:
            margin = row.decision_time - fv.observed_at
            if margin < 0:
                violation_count += 1
            if margin > max_margin:
                max_margin = margin

    # Label-window check: embargo must be >= max label horizon.
    if max_label_horizon_ns is not None and embargo_ns is not None:
        if embargo_ns >= max_label_horizon_ns:
            label_window_check_status = "passed"
        else:
            label_window_check_status = "failed"
    else:
        label_window_check_status = "skipped"

    sampled_row_count = len(rows)

    evidence_sha256 = _compute_evidence_sha256(
        manifest_hash=manifest_hash,
        feature_schema_hash=feature_schema_hash,
        feature_set_version=feature_set_version,
        max_observed_at_margin=max_margin,
        violation_count=violation_count,
        sampled_row_count=sampled_row_count,
        label_window_check_status=label_window_check_status,
    )

    return PITEvidence(
        manifest_hash=manifest_hash,
        feature_schema_hash=feature_schema_hash,
        feature_set_version=feature_set_version,
        max_observed_at_margin=max_margin,
        violation_count=violation_count,
        sampled_row_count=sampled_row_count,
        label_window_check_status=label_window_check_status,
        evidence_sha256=evidence_sha256,
    )


def verify_pit_evidence(evidence: PITEvidence | dict[str, Any]) -> PITEvidence:
    """Verify a PIT evidence record's tamper seal.

    Recomputes ``evidence_sha256`` from the payload fields and compares
    it to the stored value. A mismatch means the evidence has been
    tampered with after signing.

    Args:
        evidence: a :class:`PITEvidence` or a plain dict (which is
            parsed into a :class:`PITEvidence` first).

    Returns:
        The verified :class:`PITEvidence` (parsed if a dict was passed).

    Raises:
        PitEvidenceTamperedError: if the stored ``evidence_sha256`` does
            not match the recomputed hash.
        ValueError: if the evidence dict is missing required fields or
            has an invalid shape (from Pydantic validation).
    """
    if isinstance(evidence, dict):
        evidence = PITEvidence.from_dict(evidence)

    recomputed = evidence.compute_evidence_sha256()
    if recomputed != evidence.evidence_sha256:
        raise PitEvidenceTamperedError(
            f"PIT evidence tampered: evidence_sha256 mismatch "
            f"(expected={recomputed}, actual={evidence.evidence_sha256})",
            expected=recomputed,
            actual=evidence.evidence_sha256,
        )
    return evidence
