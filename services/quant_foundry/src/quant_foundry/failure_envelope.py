"""
quant_foundry.failure_envelope — signed failure envelopes for worker failure reporting.

Standardizes failure reporting from untrusted (RunPod/mock) workers so the trusted
side can distinguish:
  - MISSING_CALLBACK: no response from worker (silent drop / crash)
  - SIGNED_FAILURE: worker reported a failure with a valid HMAC signature
  - TAMPERED_FAILURE: envelope present but signature invalid (possible tampering)

Design invariants (non-negotiable):
- Envelopes are immutable (Pydantic frozen=True, extra="forbid").
- context_hash is a deterministic SHA-256 over canonical JSON of the FailureContext.
- envelope_hash is a deterministic SHA-256 over canonical JSON of all envelope
  fields EXCLUDING signature and envelope_hash itself (so the hash is self-certifying).
- signature is HMAC-SHA256(envelope_hash) keyed by the callback secret; None when
  no secret is configured (unsigned envelopes are still valid for hash checks but
  cannot be classified as SIGNED_FAILURE).
- validate_envelope fails closed (raises ValueError) on any hash mismatch.
- verify_signature raises ValueError on mismatch (never returns False silently for
  a present-but-wrong signature; missing signature returns False).
- Canonical JSON uses json.dumps(sort_keys=True, separators=(",", ":")) for
  byte-stable determinism across processes and platforms.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FailureStage(StrEnum):
    """Pipeline stage at which a failure occurred.

    Used by the trusted side to route failures and apply stage-specific retry policy.
    """

    DATASET_FETCH = "dataset_fetch"
    DATA_VALIDATION = "data_validation"
    QUALITY_GATE = "quality_gate"
    MODEL_LOAD = "model_load"
    TRAINING = "training"
    INFERENCE = "inference"
    ARTIFACT_WRITE = "artifact_write"
    CALLBACK_SEND = "callback_send"
    SECURITY_PREFLIGHT = "security_preflight"
    UNKNOWN = "unknown"


class FailureCode(StrEnum):
    """Enumerated failure codes for machine-readable failure classification.

    Stable string values — never rename without coordinated trusted-side migration.
    """

    DATASET_NOT_FOUND = "dataset_not_found"
    DATASET_CHECKSUM_MISMATCH = "dataset_checksum_mismatch"
    DATASET_FORMAT_ERROR = "dataset_format_error"
    MANIFEST_MISMATCH = "manifest_mismatch"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    MODEL_LOAD_ERROR = "model_load_error"
    TRAINING_ERROR = "training_error"
    TRAINING_OOM = "training_oom"
    INFERENCE_ERROR = "inference_error"
    ARTIFACT_WRITE_ERROR = "artifact_write_error"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    CALLBACK_ERROR = "callback_error"
    SECURITY_VIOLATION = "security_violation"
    ENV_VAR_FORBIDDEN = "env_var_forbidden"
    GPU_UNAVAILABLE = "gpu_unavailable"
    UNKNOWN_ERROR = "unknown_error"


# Set of retryable failure codes (transient / infrastructure failures).
_RETRYABLE_CODES: frozenset[FailureCode] = frozenset(
    {
        FailureCode.TRAINING_OOM,
        FailureCode.GPU_UNAVAILABLE,
        FailureCode.CALLBACK_ERROR,
    }
)

# Explicit non-retryable codes (deterministic policy / security failures).
_NON_RETRYABLE_CODES: frozenset[FailureCode] = frozenset(
    {
        FailureCode.SECURITY_VIOLATION,
        FailureCode.DATASET_NOT_FOUND,
        FailureCode.QUALITY_GATE_FAILED,
    }
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class FailureContext(BaseModel):
    """Immutable context attached to a failure envelope.

    Captures the provenance fields needed to reproduce / attribute a failure:
    job identity, dataset/model, pipeline stage, wall-clock timestamp, and the
    container/code provenance (git sha, image digest, container user).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    dataset_id: str | None = None
    model_family: str | None = None
    stage: FailureStage
    timestamp: str  # ISO-8601 datetime string
    container_user: str | None = None
    git_sha: str | None = None
    image_digest: str | None = None
    context_hash: str  # SHA-256 of canonical context JSON (64-char hex)


class FailureEnvelope(BaseModel):
    """Signed failure envelope emitted by a worker on failure.

    The envelope is self-certifying: envelope_hash covers all fields except
    signature and envelope_hash itself, so any tampering with fields breaks
    the hash. The optional HMAC signature binds the envelope to the callback
    secret, letting the trusted side distinguish genuine worker failures
    from tampering or missing callbacks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_id: str
    failure_code: FailureCode
    failure_message: str
    retryable: bool
    stage: FailureStage
    context: FailureContext
    context_hash: str  # SHA-256 of context (mirrored for fast verification)
    signature: str | None = None  # HMAC-SHA256 of envelope_hash, None if unsigned
    envelope_hash: str  # SHA-256 over all fields excluding signature + envelope_hash
    created_at: str

    @field_validator("envelope_id")
    @classmethod
    def _envelope_id_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("envelope_id must be non-empty")
        return v

    @field_validator("context_hash")
    @classmethod
    def _context_hash_hex64(cls, v: str) -> str:
        _validate_hex64(v, "context_hash")
        return v

    @field_validator("envelope_hash")
    @classmethod
    def _envelope_hash_hex64(cls, v: str) -> str:
        _validate_hex64(v, "envelope_hash")
        return v

    @field_validator("signature")
    @classmethod
    def _signature_hex64_if_present(cls, v: str | None) -> str | None:
        if v is None:
            return v
        _validate_hex64(v, "signature")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_hex64(value: str, field_name: str) -> None:
    """Validate that value is a 64-char lowercase hex string (SHA-256 digest)."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a 64-char hex string")
    if len(value) != 64:
        raise ValueError(f"{field_name} must be 64 chars, got {len(value)}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be hex") from exc


def _canonical_json(obj: Any) -> str:
    """Return canonical JSON encoding (sorted keys, compact separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(data: str) -> str:
    """Return SHA-256 hex digest of the UTF-8 encoded string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _envelope_id(job_id: str, timestamp: str) -> str:
    """Deterministic envelope id = SHA-256(job_id + timestamp), hex."""
    return _sha256_hex(f"{job_id}|{timestamp}")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class FailureEnvelopeBuilder:
    """Builds signed FailureEnvelope instances.

    The builder owns the callback secret (if any) and centralizes hash + signature
    computation so callers cannot accidentally produce inconsistent envelopes.

    Usage:
        builder = FailureEnvelopeBuilder(callback_secret="s3cret")
        envelope = builder.build(
            failure_code=FailureCode.DATASET_NOT_FOUND,
            failure_message="dataset ds-123 missing",
            retryable=False,
            stage=FailureStage.DATASET_FETCH,
            job_id="job-1",
            dataset_id="ds-123",
        )
    """

    def __init__(self, callback_secret: str | None = None) -> None:
        """Initialize builder with an optional HMAC callback secret."""
        self._callback_secret = callback_secret

    # -- hashing -----------------------------------------------------------

    def compute_context_hash(self, context: FailureContext) -> str:
        """Deterministic SHA-256 over canonical JSON of the context fields.

        Excludes the context_hash field itself from the hash input (it is the
        output, not an input). The remaining fields are serialized with sorted
        keys for byte-stable determinism.
        """
        ctx_data = context.model_dump(exclude={"context_hash"})
        return _sha256_hex(_canonical_json(ctx_data))

    def compute_envelope_hash(self, envelope_data: dict) -> str:
        """Deterministic SHA-256 over canonical JSON of envelope fields.

        Excludes signature and envelope_hash from the input (signature is
        computed over the hash; envelope_hash is the output). All other fields
        are included with sorted keys for order-independence.
        """
        data = {k: v for k, v in envelope_data.items() if k not in {"signature", "envelope_hash"}}
        return _sha256_hex(_canonical_json(data))

    # -- signing -----------------------------------------------------------

    def sign_envelope(self, envelope_hash: str) -> str | None:
        """HMAC-SHA256 of envelope_hash keyed by the callback secret.

        Returns None if no callback secret was configured (unsigned envelope).
        Raises ValueError if envelope_hash is not a valid 64-char hex string.
        """
        _validate_hex64(envelope_hash, "envelope_hash")
        if not self._callback_secret:
            return None
        return hmac.new(
            self._callback_secret.encode("utf-8"),
            envelope_hash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # -- build -------------------------------------------------------------

    def build(
        self,
        *,
        failure_code: FailureCode,
        failure_message: str,
        retryable: bool,
        stage: FailureStage,
        job_id: str,
        dataset_id: str | None = None,
        model_family: str | None = None,
        container_user: str | None = None,
        git_sha: str | None = None,
        image_digest: str | None = None,
    ) -> FailureEnvelope:
        """Build a fully hashed + signed FailureEnvelope.

        Steps:
          1. Construct FailureContext (with a placeholder context_hash).
          2. Compute the real context_hash and rebuild context with it.
          3. Build the envelope dict (excluding signature + envelope_hash).
          4. Compute envelope_hash.
          5. Sign envelope_hash if a secret is configured.
          6. Construct and return the immutable FailureEnvelope.
        """
        timestamp = _now_iso()
        created_at = timestamp

        # Step 1+2: context with computed hash.
        context = FailureContext(
            job_id=job_id,
            dataset_id=dataset_id,
            model_family=model_family,
            stage=stage,
            timestamp=timestamp,
            container_user=container_user,
            git_sha=git_sha,
            image_digest=image_digest,
            context_hash="0" * 64,  # placeholder, replaced below
        )
        context_hash = self.compute_context_hash(context)
        context = context.model_copy(update={"context_hash": context_hash})

        # Step 3: envelope payload (without signature + envelope_hash).
        envelope_id = _envelope_id(job_id, timestamp)
        envelope_data: dict[str, Any] = {
            "envelope_id": envelope_id,
            "failure_code": failure_code.value,
            "failure_message": failure_message,
            "retryable": retryable,
            "stage": stage.value,
            "context": context.model_dump(),
            "context_hash": context_hash,
            "created_at": created_at,
        }

        # Step 4: envelope hash.
        envelope_hash = self.compute_envelope_hash(envelope_data)

        # Step 5: sign.
        signature = self.sign_envelope(envelope_hash)

        # Step 6: assemble final envelope.
        return FailureEnvelope(
            envelope_id=envelope_id,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
            stage=stage,
            context=context,
            context_hash=context_hash,
            signature=signature,
            envelope_hash=envelope_hash,
            created_at=created_at,
        )


# ---------------------------------------------------------------------------
# Validation / verification
# ---------------------------------------------------------------------------


def validate_envelope(envelope: FailureEnvelope) -> None:
    """Validate envelope integrity (fail-closed).

    Verifies:
      - context_hash matches the computed hash from envelope.context.
      - envelope_hash matches the computed hash over all envelope fields
        excluding signature and envelope_hash.

    Raises ValueError on any mismatch. Returns None on success.
    """
    builder = FailureEnvelopeBuilder()

    # Recompute context hash from the stored context (excluding its hash field).
    expected_context_hash = builder.compute_context_hash(envelope.context)
    # First verify the context's own context_hash is self-consistent.
    if not hmac.compare_digest(expected_context_hash, envelope.context.context_hash):
        raise ValueError("context_hash mismatch: context.context_hash has been tampered with")
    # Then verify the envelope-level mirror matches the context's hash.
    if not hmac.compare_digest(expected_context_hash, envelope.context_hash):
        raise ValueError("context_hash mismatch: envelope context_hash has been tampered with")

    # Recompute envelope hash over all fields except signature + envelope_hash.
    envelope_data = envelope.model_dump(exclude={"signature", "envelope_hash"})
    # Normalize enum fields to their string values for canonical JSON stability.
    envelope_data = _normalize_envelope_data(envelope_data)
    expected_envelope_hash = builder.compute_envelope_hash(envelope_data)
    if not hmac.compare_digest(expected_envelope_hash, envelope.envelope_hash):
        raise ValueError("envelope_hash mismatch: envelope has been tampered with")


def _normalize_envelope_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize enum values to plain strings for canonical JSON hashing.

    Pydantic model_dump() of StrEnum returns the enum member; json.dumps with
    default=str would render it as the enum repr. We coerce to .value strings
    so the hash matches the builder's input (which uses .value explicitly).
    """
    normalized: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, StrEnum):
            normalized[k] = v.value
        elif isinstance(v, dict):
            normalized[k] = _normalize_dict(v)
        else:
            normalized[k] = v
    return normalized


def _normalize_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively normalize a nested dict's StrEnum values to strings."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, StrEnum):
            out[k] = v.value
        elif isinstance(v, dict):
            out[k] = _normalize_dict(v)
        else:
            out[k] = v
    return out


def verify_signature(envelope: FailureEnvelope, secret: str) -> bool:
    """Verify the envelope's HMAC signature against the provided secret.

    Returns True if the signature is present and valid.
    Raises ValueError if a signature is present but does not match (fail-closed
    on tampering rather than silently returning False).
    Returns False if no signature is present (unsigned envelope).
    """
    if envelope.signature is None:
        return False
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    expected = hmac.new(
        secret.encode("utf-8"),
        envelope.envelope_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, envelope.signature):
        raise ValueError("envelope signature verification failed")
    return True


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def is_retryable(code: FailureCode) -> bool:
    """Return True if the failure code is retryable (transient/infra failure).

    Retryable codes: TRAINING_OOM, GPU_UNAVAILABLE, CALLBACK_ERROR.
    Non-retryable codes: SECURITY_VIOLATION, DATASET_NOT_FOUND, QUALITY_GATE_FAILED.
    All other codes default to non-retryable (conservative — fail closed).
    """
    if code in _RETRYABLE_CODES:
        return True
    return False


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_envelope(envelope: FailureEnvelope) -> str:
    """Serialize an envelope to a canonical JSON string."""
    return envelope.model_dump_json()


def deserialize_envelope(json_str: str) -> FailureEnvelope:
    """Deserialize a JSON string into a FailureEnvelope.

    Raises pydantic.ValidationError on malformed input (fail-closed).
    """
    return FailureEnvelope.model_validate_json(json_str)


# ---------------------------------------------------------------------------
# Trusted-side classification
# ---------------------------------------------------------------------------


def distinguish_missing_callback_vs_signed_failure(
    envelope: FailureEnvelope | None,
    *,
    secret: str | None = None,
) -> str:
    """Classify a (possibly absent) envelope for the trusted side.

    Returns one of:
      - "MISSING_CALLBACK": envelope is None (no response from worker).
      - "SIGNED_FAILURE": envelope present and signature valid under secret.
      - "TAMPERED_FAILURE": envelope present but signature invalid/missing.

    If no secret is provided, a present envelope with a signature cannot be
    verified and is classified as TAMPERED_FAILURE (fail-closed). A present
    envelope with no signature is also TAMPERED_FAILURE (untrusted).

    Args:
        envelope: The envelope to classify, or None.
        secret: The callback secret for signature verification. If None,
            any present envelope is treated as untrusted (TAMPERED_FAILURE).

    Returns:
        The classification string.
    """
    if envelope is None:
        return "MISSING_CALLBACK"
    if secret is None:
        # Cannot verify signature -> treat as tampered/untrusted.
        return "TAMPERED_FAILURE"
    try:
        if verify_signature(envelope, secret):
            return "SIGNED_FAILURE"
    except ValueError:
        # Signature present but invalid -> tampering.
        return "TAMPERED_FAILURE"
    # verify_signature returned False (no signature present) -> untrusted.
    return "TAMPERED_FAILURE"


__all__ = [
    "FailureCode",
    "FailureContext",
    "FailureEnvelope",
    "FailureEnvelopeBuilder",
    "FailureStage",
    "deserialize_envelope",
    "distinguish_missing_callback_vs_signed_failure",
    "is_retryable",
    "serialize_envelope",
    "validate_envelope",
    "verify_signature",
]
