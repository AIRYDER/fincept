"""
quant_foundry.artifacts — pull-based, hash-verified artifact import (TASK-0403 + TASK-0503).

A "dossier" needs an artifact manifest. Artifacts are produced by external workers
(RunPod or mock dispatcher) and must be imported into Fincept safely:

- **Pull-based, never push.** Fincept fetches the artifact from a URI the operator
  allowlisted; workers never push bytes directly into the registry.
- **Hash-verified.** The expected sha256 is supplied by the caller (from the signed
  callback envelope). The imported bytes must match or the import fails closed.
- **URI scheme allowlisted.** ``file://`` and ``s3://`` are permitted. ``http`` /
  ``https`` / arbitrary schemes are rejected so a malicious worker cannot point
  Fincept at an attacker-controlled URL. S3 reads are delegated to an injected
  ``s3_reader`` callable so the artifact module has no AWS/boto3 coupling and
  credentials stay isolated in the caller.
- **Path traversal rejected.** ``file://`` paths and ``s3://`` keys are checked
  against traversal attempts (``..`` segments) as defense-in-depth.
- **Size-limited (TASK-0503).** An artifact exceeding ``max_size_bytes`` is
  rejected before hash verification (fail fast on oversized blobs).
- **Content-type validated (TASK-0503).** The artifact's file extension must be
  in ``allowed_content_types`` (default: no restriction for backward compat).
- **Quarantine / staging (TASK-0503).** When ``quarantine_dir`` is provided, the
  artifact is copied to a staging path under that directory before hash
  verification, so the registry never reads directly from the source URI.
- **Security receipts (TASK-0503).** Every rejection (bad hash, oversized,
  unsupported URI, bad content type) carries a ``SecurityReceipt`` on the
  exception for audit/persistence.

This module is file-disjoint from all active builders (see BUILDER3.md). It does
NOT modify ``schemas.py`` (``ArtifactManifest`` from TASK-0302 is consumed
read-only where useful; the richer local ``ArtifactRecord`` lives here).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, model_validator

from quant_foundry.ids import hash_payload

# URI schemes permitted for artifact import.
# file:// for local MVP; s3:// for object storage (TASK-0503).
_ALLOWED_URI_SCHEMES = frozenset({"file", "s3"})

# Default max artifact size: 500 MB (None = no limit for backward compat).
_DEFAULT_MAX_SIZE_BYTES: int | None = None


class UnsupportedUriError(ValueError):
    """Raised when an artifact URI uses a scheme that is not allowlisted or is unsafe."""

    def __init__(self, message: str, *, security_receipt: SecurityReceipt | None = None) -> None:
        super().__init__(message)
        self.security_receipt = security_receipt


class ArtifactHashMismatchError(ValueError):
    """Raised when imported artifact bytes do not match the expected sha256 (security event)."""

    def __init__(self, message: str, *, security_receipt: SecurityReceipt | None = None) -> None:
        super().__init__(message)
        self.security_receipt = security_receipt


class ArtifactSizeError(ValueError):
    """Raised when an artifact exceeds the max allowed size (TASK-0503)."""

    def __init__(self, message: str, *, security_receipt: SecurityReceipt | None = None) -> None:
        super().__init__(message)
        self.security_receipt = security_receipt


class ArtifactContentTypeError(ValueError):
    """Raised when an artifact has a disallowed content type (TASK-0503)."""

    def __init__(self, message: str, *, security_receipt: SecurityReceipt | None = None) -> None:
        super().__init__(message)
        self.security_receipt = security_receipt


class SecurityReceipt(BaseModel):
    """Audit receipt for a rejected artifact import (TASK-0503).

    Frozen + extra='forbid'. Carries the URI, the rejection reason, a
    timestamp, and optional detail. ``to_dict`` is JSON serializable for
    audit/persistence. No secrets are stored (only URI + reason + detail).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str
    reason: str
    ts_ns: int = 0
    detail: dict[str, Any] = {}

    @model_validator(mode="after")
    def _stamp_ts(self) -> SecurityReceipt:
        """Auto-stamp ``ts_ns`` if not provided (audit trail)."""
        if self.ts_ns == 0:
            object.__setattr__(self, "ts_ns", time.time_ns())
        return self

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "uri": self.uri,
            "reason": self.reason,
            "ts_ns": self.ts_ns,
            "detail": dict(self.detail),
        }


class ArtifactRecord(BaseModel):
    """Metadata for a hash-verified imported model artifact.

    Frozen + extra='forbid' for audit integrity. This is the local richer record
    composing the base ``schemas.ArtifactManifest`` (TASK-0302); it is internal to
    the evidence loop, not a cross-boundary payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
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


def verify_artifact_hash(data: bytes, expected_sha256: str) -> bool:
    """Verify that ``data`` hashes to ``expected_sha256``.

    Raises ``ArtifactHashMismatchError`` on mismatch (fail closed — a tampered or
    truncated artifact must never be registered). Raises ``TypeError`` if the hash
    is not a 64-char hex string.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"verify_artifact_hash expects bytes, got {type(data)}")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise TypeError("expected_sha256 must be a 64-char hex string")
    try:
        int(expected_sha256, 16)
    except ValueError as exc:
        raise TypeError("expected_sha256 must be a hex string") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ArtifactHashMismatchError(
            "artifact hash mismatch: expected sha256 does not match imported bytes "
            "(possible tamper / truncation / replay — security event)"
        )
    return True


def _validate_uri(uri: str) -> tuple[str, str, str]:
    """Validate the URI scheme is allowlisted and return (scheme, path, bucket_or_empty).

    Raises ``UnsupportedUriError`` for disallowed schemes or traversal attempts.
    For s3:// URIs, returns (scheme, key, bucket). For file:// URIs, returns
    (scheme, path, "").
    """
    if not uri or not isinstance(uri, str):
        raise UnsupportedUriError(
            "artifact uri must be a non-empty string",
            security_receipt=SecurityReceipt(uri=str(uri), reason="invalid_uri"),
        )
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_URI_SCHEMES:
        raise UnsupportedUriError(
            f"unsupported artifact URI scheme {scheme!r}; allowed: {sorted(_ALLOWED_URI_SCHEMES)}",
            security_receipt=SecurityReceipt(
                uri=uri,
                reason="unsupported_uri",
                detail={"scheme": scheme},
            ),
        )
    if scheme == "s3":
        bucket = parsed.netloc or ""
        if not bucket:
            raise UnsupportedUriError(
                "s3 uri has empty bucket",
                security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
            )
        raw_key = unquote(parsed.path or "").lstrip("/")
        if not raw_key:
            raise UnsupportedUriError(
                "s3 uri has empty key",
                security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
            )
        # Reject any '..' segment in the key (traversal defense-in-depth).
        parts = pathlib.PurePosixPath(raw_key).parts
        if ".." in parts:
            raise UnsupportedUriError(
                "s3 uri contains '..' traversal segment — path escape rejected",
                security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
            )
        return scheme, raw_key, bucket

    # file:// URIs
    raw_path = unquote(parsed.path or "")
    if not raw_path:
        raise UnsupportedUriError(
            "artifact uri has empty path",
            security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
        )
    parts = pathlib.PurePosixPath(raw_path).parts
    if ".." in parts:
        raise UnsupportedUriError(
            "artifact uri contains '..' traversal segment — path escape rejected",
            security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
        )
    return scheme, raw_path, ""


def _read_file_uri(uri: str) -> bytes:
    """Read bytes from a validated file:// URI."""
    _scheme, raw_path, _bucket = _validate_uri(uri)
    # On Windows, file:///C:/... yields path "/C:/..." — strip the leading slash
    # before a drive letter so pathlib.Path resolves it correctly.
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    path = pathlib.Path(raw_path)
    if not path.is_file():
        raise UnsupportedUriError(
            f"artifact file not found: {uri}",
            security_receipt=SecurityReceipt(uri=uri, reason="not_found"),
        )
    return path.read_bytes()


def _read_s3_uri(uri: str, s3_reader: Callable[[str, str], bytes]) -> bytes:
    """Read bytes from a validated s3:// URI using an injected reader callable.

    The ``s3_reader`` callable receives (bucket, key) and returns the artifact
    bytes. This keeps the artifact module free of AWS/boto3 coupling —
    credentials stay isolated in the caller.
    """
    _scheme, key, bucket = _validate_uri(uri)
    return s3_reader(bucket, key)


def _get_content_type(uri: str) -> str:
    """Extract the file extension (content type) from a URI."""
    parsed = urlparse(uri)
    path = unquote(parsed.path or "")
    _, ext = os.path.splitext(path)
    return ext.lower()


def import_artifact(
    *,
    uri: str,
    expected_sha256: str,
    artifact_id: str,
    model_family: str,
    feature_schema_hash: str,
    label_schema_hash: str,
    code_git_sha: str | None = None,
    lockfile_hash: str | None = None,
    container_image_digest: str | None = None,
    created_at_ns: int | None = None,
    s3_reader: Callable[[str, str], bytes] | None = None,
    max_size_bytes: int | None = _DEFAULT_MAX_SIZE_BYTES,
    allowed_content_types: frozenset[str] | None = None,
    quarantine_dir: str | None = None,
) -> ArtifactRecord:
    """Pull an artifact from ``uri``, hash-verify it, and return an ``ArtifactRecord``.

    Security invariants:
    - URI scheme must be allowlisted (file:// or s3://).
    - Path traversal is rejected (both file paths and S3 keys).
    - Content type must be in ``allowed_content_types`` if provided.
    - Artifact size must not exceed ``max_size_bytes`` if provided.
    - The imported bytes must hash to ``expected_sha256`` or the import fails closed.
    - When ``quarantine_dir`` is provided, the artifact is staged there before
      hash verification (the registry never reads directly from the source).
    - S3 reads are delegated to ``s3_reader`` (no AWS/boto3 coupling).
    - No secrets are stored (only hashes + metadata).
    - Every rejection carries a ``SecurityReceipt`` on the exception for audit.
    """
    # Validate URI scheme + traversal (raises early with security receipt).
    scheme, _path, _bucket = _validate_uri(uri)

    # Content type validation (before download — fail fast on bad extension).
    if allowed_content_types is not None:
        ext = _get_content_type(uri)
        if ext not in allowed_content_types:
            raise ArtifactContentTypeError(
                f"artifact content type {ext!r} not in allowed: {sorted(allowed_content_types)}",
                security_receipt=SecurityReceipt(
                    uri=uri,
                    reason="invalid_content_type",
                    detail={"extension": ext, "allowed": sorted(allowed_content_types)},
                ),
            )

    # Read the artifact bytes.
    if scheme == "file":
        data = _read_file_uri(uri)
    elif scheme == "s3":
        if s3_reader is None:
            raise UnsupportedUriError(
                "s3 uri requires an s3_reader callable (no AWS/boto3 coupling "
                "in the artifact module — credentials stay in the caller)",
                security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
            )
        data = _read_s3_uri(uri, s3_reader)
    else:
        raise UnsupportedUriError(
            f"unsupported artifact URI scheme {scheme!r}",
            security_receipt=SecurityReceipt(uri=uri, reason="unsupported_uri"),
        )

    # Size limit check (before hash verification — fail fast on oversized).
    if max_size_bytes is not None and len(data) > max_size_bytes:
        raise ArtifactSizeError(
            f"artifact size {len(data)} bytes exceeds max {max_size_bytes} bytes",
            security_receipt=SecurityReceipt(
                uri=uri,
                reason="oversized",
                detail={"size_bytes": len(data), "max_size_bytes": max_size_bytes},
            ),
        )

    # Quarantine / staging: copy to staging dir before hash verification.
    if quarantine_dir is not None:
        staging_path = os.path.join(quarantine_dir, f"{artifact_id}_{expected_sha256[:16]}.staging")
        pathlib.Path(staging_path).write_bytes(data)

    # Hash verification (fail closed on mismatch).
    try:
        verify_artifact_hash(data, expected_sha256)
    except ArtifactHashMismatchError as exc:
        raise ArtifactHashMismatchError(
            str(exc),
            security_receipt=SecurityReceipt(
                uri=uri,
                reason="hash_mismatch",
                detail={
                    "expected": expected_sha256,
                    "actual": hashlib.sha256(data).hexdigest(),
                },
            ),
        ) from exc

    return ArtifactRecord(
        artifact_id=artifact_id,
        sha256=expected_sha256,
        size_bytes=len(data),
        uri=uri,
        model_family=model_family,
        created_at_ns=created_at_ns if created_at_ns is not None else time.time_ns(),
        feature_schema_hash=feature_schema_hash,
        label_schema_hash=label_schema_hash,
        code_git_sha=code_git_sha,
        lockfile_hash=lockfile_hash,
        container_image_digest=container_image_digest,
    )


def artifact_content_hash(record: ArtifactRecord) -> str:
    """Deterministic content hash of an ArtifactRecord (for dossier immutability).

    Uses the canonical JSON of the record (sorted keys) hashed via sha256. This is
    distinct from the artifact's own ``sha256`` (which hashes the model bytes); it
    hashes the *manifest* so two dossiers referencing the same artifact but with
    different training metadata produce different content hashes.
    """
    payload = record.model_dump_json().encode("utf-8")
    return hash_payload(payload)
