"""
quant_foundry.artifacts — pull-based, hash-verified artifact import (TASK-0403).

A "dossier" needs an artifact manifest. Artifacts are produced by external workers
(RunPod or mock dispatcher) and must be imported into Fincept safely:

- **Pull-based, never push.** Fincept fetches the artifact from a URI the operator
  allowlisted; workers never push bytes directly into the registry.
- **Hash-verified.** The expected sha256 is supplied by the caller (from the signed
  callback envelope). The imported bytes must match or the import fails closed.
- **URI scheme allowlisted.** Only ``file://`` is permitted for the local MVP. A
  future task may add ``s3://`` / ``gs://`` with credential isolation. ``http`` /
  ``https`` / arbitrary schemes are rejected so a malicious worker cannot point
  Fincept at an attacker-controlled URL.
- **Path traversal rejected.** ``file://`` URIs are resolved and checked against
  traversal attempts (``..`` segments) as defense-in-depth.

This module is file-disjoint from all active builders (see BUILDER3.md). It does
NOT modify ``schemas.py`` (``ArtifactManifest`` from TASK-0302 is consumed
read-only where useful; the richer local ``ArtifactRecord`` lives here).
"""

from __future__ import annotations

import hashlib
import pathlib
import time
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict

from quant_foundry.ids import hash_payload

# URI schemes permitted for artifact import. Local MVP is file-only.
# A future task (TASK-0503) may extend this to s3:// / gs:// with credential isolation.
_ALLOWED_URI_SCHEMES = frozenset({"file"})


class UnsupportedUriError(ValueError):
    """Raised when an artifact URI uses a scheme that is not allowlisted or is unsafe."""


class ArtifactHashMismatchError(ValueError):
    """Raised when imported artifact bytes do not match the expected sha256 (security event)."""


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


def _validate_uri(uri: str) -> tuple[str, str]:
    """Validate the URI scheme is allowlisted and return (scheme, path).

    Raises ``UnsupportedUriError`` for disallowed schemes or traversal attempts.
    """
    if not uri or not isinstance(uri, str):
        raise UnsupportedUriError("artifact uri must be a non-empty string")
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_URI_SCHEMES:
        raise UnsupportedUriError(
            f"unsupported artifact URI scheme {scheme!r}; allowed: "
            f"{sorted(_ALLOWED_URI_SCHEMES)} (MVP is file-only)"
        )
    # For file:// URIs, decode and reject traversal.
    raw_path = unquote(parsed.path or "")
    if not raw_path:
        raise UnsupportedUriError("artifact uri has empty path")
    # Reject any '..' segment (traversal) regardless of absolute/relative form.
    parts = pathlib.PurePosixPath(raw_path).parts
    if ".." in parts:
        raise UnsupportedUriError(
            "artifact uri contains '..' traversal segment — path escape rejected"
        )
    return scheme, raw_path


def _read_file_uri(uri: str) -> bytes:
    """Read bytes from a validated file:// URI."""
    _scheme, raw_path = _validate_uri(uri)
    # On Windows, file:///C:/... yields path "/C:/..." — strip the leading slash
    # before a drive letter so pathlib.Path resolves it correctly.
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    path = pathlib.Path(raw_path)
    if not path.is_file():
        raise UnsupportedUriError(f"artifact file not found: {uri}")
    return path.read_bytes()


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
) -> ArtifactRecord:
    """Pull an artifact from ``uri``, hash-verify it, and return an ``ArtifactRecord``.

    Security invariants:
    - URI scheme must be allowlisted (file:// only for MVP).
    - Path traversal is rejected.
    - The imported bytes must hash to ``expected_sha256`` or the import fails closed.
    - No secrets are stored (only hashes + metadata).
    """
    data = _read_file_uri(uri)
    verify_artifact_hash(data, expected_sha256)
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
