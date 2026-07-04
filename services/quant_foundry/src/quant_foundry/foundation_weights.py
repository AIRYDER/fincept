"""
quant_foundry.foundation_weights — Foundation model weight policy (T-11.1).

Pins foundation model weights (e.g. Chronos, Moirai, TimesFM) by id and
SHA-256 hash, prevents surprise network downloads in production, and
records weight hashes in runtime fingerprints.

Design invariants (non-negotiable, fail-closed):
- Weights are pinned by ``model_id`` + ``weight_hash`` (SHA-256, 64-char hex).
- ``WeightSource.FORBIDDEN_NETWORK`` is never an allowed source. Any spec or
  policy that includes it is rejected at construction / registration time.
- ``offline_mode`` (default True) blocks any network URL from being used as a
  weight source. ``validate_no_network_download`` raises ``ValueError`` on a
  network URL when the policy is offline.
- Missing weight hash, missing approval, or hash mismatch all raise
  ``ValueError`` (fail-closed) — never silently degrade.
- Weight hashes are surfaced via ``WeightManager.get_fingerprint_data`` so they
  are recorded in the runtime fingerprint (reproducibility / audit).

Public surface:
  - WeightSource (enum)
  - FoundationWeightSpec, WeightPolicy, WeightReceipt (Pydantic v2 models)
  - compute_weight_hash, verify_weight (functions)
  - WeightManager (class)
  - validate_no_network_download (function)
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A SHA-256 hex digest is exactly 64 lowercase hex characters.
_HEX256_RE = re.compile(r"^[0-9a-f]{64}$")

# Recognized network URL schemes. A weight URI / attempted source starting with
# one of these is treated as a network download.
_NETWORK_SCHEMES = ("http://", "https://", "ftp://", "s3://", "gs://", "azure://")


class WeightSource(StrEnum):
    """Allowed provenance for a foundation model weight file.

    ``BAKED``        — shipped inside the package/image (immutable).
    ``CACHED``       — present in an approved local cache directory.
    ``LOCAL``        — supplied by the operator from a local path.
    ``FORBIDDEN_NETWORK`` — any network download. NEVER allowed; included only
                       so it can be explicitly rejected (fail-closed).
    """

    BAKED = "baked"
    CACHED = "cached"
    LOCAL = "local"
    FORBIDDEN_NETWORK = "forbidden_network"


class FoundationWeightSpec(BaseModel):
    """Pin for a single foundation model weight artifact.

    Frozen + extra-forbid so a pinned spec cannot be mutated or extended with
    surprise fields after the fact. The ``weight_hash`` must be a 64-char
    lowercase hex SHA-256 digest, and the source must not be
    ``FORBIDDEN_NETWORK`` (fail-closed at construction).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    model_family: str
    weight_hash: str
    weight_uri: str
    source: WeightSource
    size_bytes: int = Field(ge=0)
    pinned_at: str
    approved_by: str

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("model_family")
    @classmethod
    def _model_family_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_family must be a non-empty string")
        return v

    @field_validator("weight_hash")
    @classmethod
    def _weight_hash_hex256(cls, v: str) -> str:
        if not isinstance(v, str) or not _HEX256_RE.match(v):
            raise ValueError("weight_hash must be a 64-character lowercase hex SHA-256 digest")
        return v

    @field_validator("weight_uri")
    @classmethod
    def _weight_uri_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("weight_uri must be a non-empty string")
        return v

    @field_validator("approved_by")
    @classmethod
    def _approved_by_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("approved_by must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _reject_forbidden_network(self) -> FoundationWeightSpec:
        # Fail-closed: a FORBIDDEN_NETWORK source can never be pinned.
        if self.source is WeightSource.FORBIDDEN_NETWORK:
            raise ValueError("FORBIDDEN_NETWORK source is never allowed for a foundation weight")
        return self


class WeightPolicy(BaseModel):
    """Policy governing how foundation weights may be registered and loaded.

    Frozen + extra-forbid. ``allowed_sources`` must never include
    ``FORBIDDEN_NETWORK``. ``offline_mode`` (default True) blocks network
    downloads entirely.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_sources: list[WeightSource] = Field(default_factory=list)
    require_hash: bool = True
    require_approval: bool = True
    cache_dir: str | None = None
    offline_mode: bool = True

    @model_validator(mode="after")
    def _no_forbidden_network(self) -> WeightPolicy:
        if WeightSource.FORBIDDEN_NETWORK in self.allowed_sources:
            raise ValueError("FORBIDDEN_NETWORK must not appear in allowed_sources")
        return self


class WeightReceipt(BaseModel):
    """Evidence packet produced when a weight is registered or loaded.

    Carries the verified spec, whether the on-disk hash matched, the timestamp
    of verification, the fingerprint hash to embed in the runtime fingerprint,
    and whether the registration complied with the active policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: FoundationWeightSpec
    verified: bool
    verified_at: str
    fingerprint_hash: str
    policy_compliant: bool


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def compute_weight_hash(file_path: str) -> str:
    """Compute the SHA-256 hash of a weight file and return hex digest.

    Reads the file in 1 MiB chunks so large weight files do not blow up memory.

    Args:
        file_path: Absolute or relative path to the weight file on disk.

    Returns:
        64-character lowercase hex SHA-256 digest.

    Raises:
        FileNotFoundError: if the file does not exist.
        OSError: on any read error.
    """
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("file_path must be a non-empty string")
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_weight(spec: FoundationWeightSpec, actual_file_path: str) -> bool:
    """Verify that the on-disk weight file matches the spec's pinned hash.

    Args:
        spec: The pinned weight specification (carries the expected hash).
        actual_file_path: Path to the weight file on disk.

    Returns:
        True if the computed hash equals ``spec.weight_hash``, False otherwise.
        Returns False (does not raise) when the file is missing, so callers can
        treat a missing artifact as a verification failure rather than a crash.
    """
    if not isinstance(spec, FoundationWeightSpec):
        raise TypeError("spec must be a FoundationWeightSpec")
    if not isinstance(actual_file_path, str) or not actual_file_path:
        raise ValueError("actual_file_path must be a non-empty string")
    if not os.path.exists(actual_file_path):
        return False
    try:
        actual = compute_weight_hash(actual_file_path)
    except OSError:
        return False
    return actual == spec.weight_hash


def _is_network_url(uri: str) -> bool:
    """Return True if ``uri`` looks like a network URL (not a local path)."""
    if not isinstance(uri, str):
        return False
    lowered = uri.lower()
    return lowered.startswith(_NETWORK_SCHEMES)


def validate_no_network_download(policy: WeightPolicy, attempted_source: str) -> bool:
    """Validate that an attempted weight source respects the offline policy.

    When ``policy.offline_mode`` is True and ``attempted_source`` is a network
    URL, this raises ``ValueError`` (fail-closed) — preventing surprise network
    downloads in production.

    Args:
        policy: The active weight policy.
        attempted_source: The URI / path being attempted as a weight source.

    Returns:
        True if the attempt is compliant with the policy.

    Raises:
        ValueError: if offline mode is on and the source is a network URL.
    """
    if not isinstance(policy, WeightPolicy):
        raise TypeError("policy must be a WeightPolicy")
    if not isinstance(attempted_source, str) or not attempted_source:
        raise ValueError("attempted_source must be a non-empty string")
    if policy.offline_mode and _is_network_url(attempted_source):
        raise ValueError(
            f"network download attempted while offline_mode is enabled: {attempted_source!r}"
        )
    return True


class WeightManager:
    """Registry + policy enforcer for pinned foundation model weights.

    Holds a set of ``FoundationWeightSpec`` entries keyed by ``model_id`` and
    enforces the active ``WeightPolicy`` on registration and load. All
    policy violations raise ``ValueError`` (fail-closed).
    """

    def __init__(self, policy: WeightPolicy) -> None:
        """Create a manager bound to ``policy``.

        Args:
            policy: The weight policy to enforce for all registrations/loads.
        """
        if not isinstance(policy, WeightPolicy):
            raise TypeError("policy must be a WeightPolicy")
        self._policy: WeightPolicy = policy
        # model_id -> spec. Insertion order preserved for deterministic listing.
        self._weights: dict[str, FoundationWeightSpec] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_weight(self, spec: FoundationWeightSpec) -> WeightReceipt:
        """Register a pinned weight spec and return a verification receipt.

        Enforces (fail-closed, raises ``ValueError``):
        - ``spec.source`` must be in ``policy.allowed_sources``.
        - ``spec.source`` must not be ``FORBIDDEN_NETWORK`` (also enforced at
          spec construction; double-checked here for defense in depth).
        - When ``policy.require_hash`` is True, ``spec.weight_hash`` must be a
          non-empty 64-char hex digest (enforced by the spec model; checked
          again here).
        - When ``policy.require_approval`` is True, ``spec.approved_by`` must be
          a non-empty string (enforced by the spec model; checked again here).

        If the weight file at ``spec.weight_uri`` exists on disk, its hash is
        verified against ``spec.weight_hash`` and the receipt's ``verified``
        flag reflects the result. A hash mismatch does NOT raise here — it is
        recorded as ``verified=False`` / ``policy_compliant=False`` so the
        caller can decide how to surface it. ``load_weight`` DOES raise on
        mismatch (fail-closed at load time).

        Args:
            spec: The pinned weight specification to register.

        Returns:
            A ``WeightReceipt`` recording the verification outcome.

        Raises:
            TypeError: if ``spec`` is not a ``FoundationWeightSpec``.
            ValueError: on any policy violation (fail-closed).
        """
        if not isinstance(spec, FoundationWeightSpec):
            raise TypeError("spec must be a FoundationWeightSpec")

        # Defense in depth: FORBIDDEN_NETWORK can never be registered.
        if spec.source is WeightSource.FORBIDDEN_NETWORK:
            raise ValueError("FORBIDDEN_NETWORK source is never allowed for a foundation weight")

        # Source must be in the policy's allow-list.
        if self._policy.allowed_sources and spec.source not in self._policy.allowed_sources:
            raise ValueError(
                f"weight source {spec.source!r} is not in allowed_sources "
                f"{[s.value for s in self._policy.allowed_sources]!r}"
            )

        # Fail-closed: hash required by policy.
        if self._policy.require_hash:
            if not spec.weight_hash or not _HEX256_RE.match(spec.weight_hash):
                raise ValueError(
                    "policy requires a weight hash but none (or an invalid one) was provided"
                )

        # Fail-closed: approval required by policy.
        if self._policy.require_approval:
            if not spec.approved_by or not spec.approved_by.strip():
                raise ValueError("policy requires an approver but approved_by is empty")

        # Offline policy: a network URI may never be registered.
        if self._policy.offline_mode and _is_network_url(spec.weight_uri):
            raise ValueError(
                "network weight URI is forbidden while offline_mode is enabled: "
                f"{spec.weight_uri!r}"
            )

        # Verify on-disk hash if the file exists.
        verified = False
        if os.path.exists(spec.weight_uri):
            verified = verify_weight(spec, spec.weight_uri)

        # A weight is policy-compliant only if the on-disk hash verified (when
        # a file was present) OR no file was present yet (pre-registration of a
        # baked/cached weight that will be materialized later). A present-but-
        # mismatched file is non-compliant.
        policy_compliant = True
        if os.path.exists(spec.weight_uri) and not verified:
            policy_compliant = False

        fingerprint_hash = spec.weight_hash
        receipt = WeightReceipt(
            spec=spec,
            verified=verified,
            verified_at=_now_iso(),
            fingerprint_hash=fingerprint_hash,
            policy_compliant=policy_compliant,
        )

        self._weights[spec.model_id] = spec
        return receipt

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_weight(self, model_id: str) -> WeightReceipt:
        """Look up a registered weight by ``model_id`` and re-verify its hash.

        Fail-closed (raises ``ValueError``):
        - ``model_id`` is not registered.
        - The weight file is missing on disk.
        - The on-disk hash does not match the pinned ``weight_hash``.

        Args:
            model_id: The id of the registered weight to load.

        Returns:
            A ``WeightReceipt`` with ``verified=True`` and ``policy_compliant=True``.

        Raises:
            ValueError: if the model is unregistered, the file is missing, or the
                hash mismatches (fail-closed).
        """
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id must be a non-empty string")
        if model_id not in self._weights:
            raise ValueError(f"no registered weight for model_id={model_id!r}")
        spec = self._weights[model_id]

        if not os.path.exists(spec.weight_uri):
            raise ValueError(
                f"weight file missing on disk for model_id={model_id!r}: {spec.weight_uri!r}"
            )

        if not verify_weight(spec, spec.weight_uri):
            raise ValueError(
                f"weight hash mismatch for model_id={model_id!r}: "
                "pinned hash does not match the on-disk file"
            )

        return WeightReceipt(
            spec=spec,
            verified=True,
            verified_at=_now_iso(),
            fingerprint_hash=spec.weight_hash,
            policy_compliant=True,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_weights(self) -> list[FoundationWeightSpec]:
        """Return all registered weight specs in registration order."""
        return list(self._weights.values())

    def get_fingerprint_data(self) -> dict[str, str]:
        """Return ``{model_id: weight_hash}`` for runtime fingerprint inclusion.

        This is the artifact that gets merged into the runtime fingerprint so
        that a run's pinned weights are reproducibly recorded.
        """
        return {mid: spec.weight_hash for mid, spec in self._weights.items()}

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def policy(self) -> WeightPolicy:
        """The active weight policy (read-only)."""
        return self._policy

    def __len__(self) -> int:
        return len(self._weights)

    def __contains__(self, model_id: Any) -> bool:
        return isinstance(model_id, str) and model_id in self._weights
