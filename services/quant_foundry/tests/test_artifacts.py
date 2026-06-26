"""
Tests for TASK-0503: Add Artifact Import From Object Storage.

TDD red phase — these tests extend the artifact import tests from TASK-0403
with S3/object storage URI support, size limits, content type validation,
quarantine/staging path, and security receipts.

Acceptance criteria covered:
- Bad hash rejects import.
- Oversized artifact rejects import.
- Unsupported URI rejects import.
- Valid artifact gets a dossier candidate record.

Additional checks from the spec:
- S3 URI scheme is allowlisted (not rejected as unsupported).
- Content type validation rejects invalid types.
- Quarantine/staging path is used (artifact is downloaded to staging
  before hash verification).
- Security receipt is emitted on rejection (audit trail).
- Path traversal rejected for S3 keys (defense-in-depth).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile
from typing import Any

import pytest
from quant_foundry.artifacts import (
    ArtifactContentTypeError,
    ArtifactHashMismatchError,
    ArtifactRecord,
    ArtifactSizeError,
    SecurityReceipt,
    UnsupportedUriError,
    import_artifact,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_temp_artifact(data: bytes) -> tuple[str, str]:
    """Write bytes to a temp file and return (file_uri, sha256)."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "model.pkl")
    pathlib.Path(path).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    uri = f"file:///{path}"
    return uri, sha


def _make_mock_s3_reader(data: bytes):
    """Return a callable that mimics reading from S3 (for test injection)."""

    def _reader(bucket: str, key: str) -> bytes:
        return data

    return _reader


# ---------------------------------------------------------------------------
# TASK-0403 regression — existing file:// import still works
# ===========================================================================


class TestFileImportRegression:
    """Ensure TASK-0403 file:// import still works after TASK-0503 extensions."""

    def test_file_import_succeeds(self) -> None:
        data = b"model-bytes-v1"
        uri, sha = _write_temp_artifact(data)
        record = import_artifact(
            uri=uri,
            expected_sha256=sha,
            artifact_id="art-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
        )
        assert record.artifact_id == "art-1"
        assert record.sha256 == sha
        assert record.size_bytes == len(data)

    def test_file_import_rejects_bad_hash(self) -> None:
        data = b"model-bytes-v1"
        uri, _sha = _write_temp_artifact(data)
        with pytest.raises(ArtifactHashMismatchError):
            import_artifact(
                uri=uri,
                expected_sha256="b" * 64,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
            )

    def test_file_import_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(UnsupportedUriError):
            import_artifact(
                uri="http://evil.example.com/model.pkl",
                expected_sha256="a" * 64,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
            )


# ---------------------------------------------------------------------------
# TASK-0503 — S3 URI support
# ===========================================================================


class TestS3UriSupport:
    """S3 URI scheme is allowlisted and can be imported via an injected reader."""

    def test_s3_uri_is_allowlisted(self) -> None:
        """An s3:// URI should NOT be rejected as unsupported."""
        data = b"model-bytes-s3"
        sha = hashlib.sha256(data).hexdigest()
        reader = _make_mock_s3_reader(data)
        record = import_artifact(
            uri="s3://my-bucket/models/model.pkl",
            expected_sha256=sha,
            artifact_id="art-s3-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            s3_reader=reader,
        )
        assert record.artifact_id == "art-s3-1"
        assert record.sha256 == sha
        assert record.size_bytes == len(data)
        assert record.uri == "s3://my-bucket/models/model.pkl"

    def test_s3_uri_rejects_bad_hash(self) -> None:
        """Bad hash on S3 import is rejected (fail closed)."""
        data = b"model-bytes-s3"
        reader = _make_mock_s3_reader(data)
        with pytest.raises(ArtifactHashMismatchError):
            import_artifact(
                uri="s3://my-bucket/models/model.pkl",
                expected_sha256="b" * 64,
                artifact_id="art-s3-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                s3_reader=reader,
            )

    def test_s3_uri_rejects_traversal_key(self) -> None:
        """S3 key with '..' traversal is rejected (defense-in-depth)."""
        data = b"model-bytes-s3"
        reader = _make_mock_s3_reader(data)
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsupportedUriError):
            import_artifact(
                uri="s3://my-bucket/../etc/passwd",
                expected_sha256=sha,
                artifact_id="art-s3-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                s3_reader=reader,
            )

    def test_s3_uri_without_reader_raises(self) -> None:
        """S3 import without an s3_reader callable raises a clear error."""
        data = b"model-bytes-s3"
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(UnsupportedUriError, match="s3_reader"):
            import_artifact(
                uri="s3://my-bucket/models/model.pkl",
                expected_sha256=sha,
                artifact_id="art-s3-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
            )


# ---------------------------------------------------------------------------
# TASK-0503 — Size limits
# ===========================================================================


class TestSizeLimits:
    """Oversized artifact rejects import."""

    def test_oversized_artifact_rejected(self) -> None:
        """An artifact exceeding max_size_bytes is rejected."""
        data = b"x" * 100  # 100 bytes
        uri, sha = _write_temp_artifact(data)
        with pytest.raises(ArtifactSizeError):
            import_artifact(
                uri=uri,
                expected_sha256=sha,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                max_size_bytes=50,  # limit 50 bytes, artifact is 100
            )

    def test_size_limit_not_exceeded_passes(self) -> None:
        """An artifact within the size limit passes."""
        data = b"x" * 100
        uri, sha = _write_temp_artifact(data)
        record = import_artifact(
            uri=uri,
            expected_sha256=sha,
            artifact_id="art-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            max_size_bytes=200,
        )
        assert record.size_bytes == 100

    def test_s3_oversized_rejected(self) -> None:
        """S3 artifact exceeding max_size_bytes is rejected."""
        data = b"x" * 100
        reader = _make_mock_s3_reader(data)
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ArtifactSizeError):
            import_artifact(
                uri="s3://my-bucket/models/model.pkl",
                expected_sha256=sha,
                artifact_id="art-s3-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                s3_reader=reader,
                max_size_bytes=50,
            )


# ---------------------------------------------------------------------------
# TASK-0503 — Content type validation
# ===========================================================================


class TestContentTypeValidation:
    """Content type validation rejects invalid types."""

    def test_valid_content_type_passes(self) -> None:
        """An artifact with a valid content type passes."""
        data = b"model-bytes-v1"
        uri, sha = _write_temp_artifact(data)
        record = import_artifact(
            uri=uri,
            expected_sha256=sha,
            artifact_id="art-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            allowed_content_types=frozenset({".pkl", ".onnx", ".pt"}),
        )
        assert record.artifact_id == "art-1"

    def test_invalid_content_type_rejected(self) -> None:
        """An artifact with an invalid content type is rejected."""
        data = b"model-bytes-v1"
        # Write with .exe extension.
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "model.exe")
        pathlib.Path(path).write_bytes(data)
        uri = f"file:///{path}"
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ArtifactContentTypeError):
            import_artifact(
                uri=uri,
                expected_sha256=sha,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                allowed_content_types=frozenset({".pkl", ".onnx", ".pt"}),
            )

    def test_s3_invalid_content_type_rejected(self) -> None:
        """S3 artifact with invalid content type is rejected."""
        data = b"model-bytes-s3"
        reader = _make_mock_s3_reader(data)
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ArtifactContentTypeError):
            import_artifact(
                uri="s3://my-bucket/models/model.exe",
                expected_sha256=sha,
                artifact_id="art-s3-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                s3_reader=reader,
                allowed_content_types=frozenset({".pkl", ".onnx", ".pt"}),
            )


# ---------------------------------------------------------------------------
# TASK-0503 — Quarantine / staging path
# ===========================================================================


class TestQuarantinePath:
    """Artifact is downloaded to a quarantine/staging path before registration."""

    def test_quarantine_dir_used_for_file_import(self) -> None:
        """The artifact is copied to the quarantine dir before hash verification."""
        data = b"model-bytes-v1"
        uri, sha = _write_temp_artifact(data)
        quarantine_dir = tempfile.mkdtemp()
        record = import_artifact(
            uri=uri,
            expected_sha256=sha,
            artifact_id="art-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            quarantine_dir=quarantine_dir,
        )
        assert record.artifact_id == "art-1"
        # The quarantine dir should contain the staged artifact.
        staged_files = list(pathlib.Path(quarantine_dir).iterdir())
        assert len(staged_files) >= 1

    def test_quarantine_dir_used_for_s3_import(self) -> None:
        """S3 artifact is downloaded to quarantine dir before hash verification."""
        data = b"model-bytes-s3"
        reader = _make_mock_s3_reader(data)
        sha = hashlib.sha256(data).hexdigest()
        quarantine_dir = tempfile.mkdtemp()
        record = import_artifact(
            uri="s3://my-bucket/models/model.pkl",
            expected_sha256=sha,
            artifact_id="art-s3-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            s3_reader=reader,
            quarantine_dir=quarantine_dir,
        )
        assert record.artifact_id == "art-s3-1"
        staged_files = list(pathlib.Path(quarantine_dir).iterdir())
        assert len(staged_files) >= 1


# ---------------------------------------------------------------------------
# TASK-0503 — Security receipts
# ===========================================================================


class TestSecurityReceipt:
    """Security receipt is emitted on rejection (audit trail)."""

    def test_security_receipt_for_bad_hash(self) -> None:
        """A bad-hash rejection emits a security receipt."""
        data = b"model-bytes-v1"
        uri, _sha = _write_temp_artifact(data)
        with pytest.raises(ArtifactHashMismatchError) as exc_info:
            import_artifact(
                uri=uri,
                expected_sha256="b" * 64,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
            )
        # The exception should carry a security receipt.
        receipt = getattr(exc_info.value, "security_receipt", None)
        assert receipt is not None
        assert isinstance(receipt, SecurityReceipt)
        assert receipt.uri == uri
        assert receipt.reason == "hash_mismatch"
        assert receipt.ts_ns > 0

    def test_security_receipt_for_oversized(self) -> None:
        """An oversized rejection emits a security receipt."""
        data = b"x" * 100
        uri, sha = _write_temp_artifact(data)
        with pytest.raises(ArtifactSizeError) as exc_info:
            import_artifact(
                uri=uri,
                expected_sha256=sha,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                max_size_bytes=50,
            )
        receipt = getattr(exc_info.value, "security_receipt", None)
        assert receipt is not None
        assert receipt.reason == "oversized"
        assert receipt.uri == uri

    def test_security_receipt_for_unsupported_uri(self) -> None:
        """An unsupported URI rejection emits a security receipt."""
        with pytest.raises(UnsupportedUriError) as exc_info:
            import_artifact(
                uri="http://evil.example.com/model.pkl",
                expected_sha256="a" * 64,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
            )
        receipt = getattr(exc_info.value, "security_receipt", None)
        assert receipt is not None
        assert receipt.reason == "unsupported_uri"

    def test_security_receipt_for_bad_content_type(self) -> None:
        """A bad content type rejection emits a security receipt."""
        data = b"model-bytes-v1"
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "model.exe")
        pathlib.Path(path).write_bytes(data)
        uri = f"file:///{path}"
        sha = hashlib.sha256(data).hexdigest()
        with pytest.raises(ArtifactContentTypeError) as exc_info:
            import_artifact(
                uri=uri,
                expected_sha256=sha,
                artifact_id="art-1",
                model_family="gbm",
                feature_schema_hash="f" * 64,
                label_schema_hash="l" * 64,
                allowed_content_types=frozenset({".pkl", ".onnx"}),
            )
        receipt = getattr(exc_info.value, "security_receipt", None)
        assert receipt is not None
        assert receipt.reason == "invalid_content_type"

    def test_security_receipt_to_dict_is_json_serializable(self) -> None:
        """The security receipt can be serialized for audit/persistence."""
        import json

        receipt = SecurityReceipt(
            uri="s3://bucket/key",
            reason="hash_mismatch",
            detail={"expected": "a" * 64, "actual": "b" * 64},
        )
        d = receipt.to_dict()
        json.dumps(d)
        assert d["uri"] == "s3://bucket/key"
        assert d["reason"] == "hash_mismatch"


# ---------------------------------------------------------------------------
# TASK-0503 — Valid artifact gets a dossier candidate record
# ===========================================================================


class TestValidArtifactGetsDossierCandidate:
    """A valid artifact import can feed a dossier candidate record."""

    def test_valid_s3_artifact_record_can_build_dossier(self) -> None:
        """A valid S3 artifact record has all fields needed for a dossier."""
        from quant_foundry.dossier import DossierBuilder

        data = b"model-bytes-s3"
        reader = _make_mock_s3_reader(data)
        sha = hashlib.sha256(data).hexdigest()
        record = import_artifact(
            uri="s3://my-bucket/models/model.pkl",
            expected_sha256=sha,
            artifact_id="art-s3-1",
            model_family="gbm",
            feature_schema_hash="f" * 64,
            label_schema_hash="l" * 64,
            s3_reader=reader,
        )
        # The record can feed a DossierBuilder.
        dossier = DossierBuilder().build(
            artifact=record,
            model_id="m1",
            dataset_manifest_id="ds-1",
        )
        assert dossier.model_id == "m1"
        assert dossier.artifact_sha256 == sha


# ---------------------------------------------------------------------------
# Cross-cutting: no secrets in artifact output
# ===========================================================================


class TestNoSecretsInArtifactOutput:
    """Artifact records and security receipts must not leak secrets."""

    @pytest.mark.parametrize(
        "secret_field",
        [
            "api_key",
            "token",
            "secret",
            "password",
            "broker_account",
            "credential",
        ],
    )
    def test_artifact_record_has_no_secret_fields(self, secret_field: str) -> None:
        """ArtifactRecord must not have any secret-named field."""
        fields = set(ArtifactRecord.model_fields.keys())
        assert secret_field not in fields

    def test_security_receipt_to_dict_has_no_secret_keys(self) -> None:

        receipt = SecurityReceipt(
            uri="s3://bucket/key",
            reason="hash_mismatch",
            detail={"expected": "a" * 64, "actual": "b" * 64},
        )
        d = receipt.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
