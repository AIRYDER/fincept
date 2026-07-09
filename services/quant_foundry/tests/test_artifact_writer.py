"""Tests for the Phase 1 / T-1.2 artifact writer interface.

Tests verify the acceptance criteria:
1. Two backends: VolumeArtifactWriter (canary/operator fallback) and
   PresignedUploadArtifactWriter (production path), plus FakeArtifactWriter
   for testing.
2. Writer returns URI/hash/size/format (+ signed write receipt).
3. Worker signs returned artifact metadata (write receipt HMAC).
4. Fake writer computes expected sha without writing.
5. Writer failure produces a signed failure envelope.
6. Disallowed URI scheme is rejected.

The handler module lives in ``runpod/quant-foundry-training/handler.py``
(outside the quant_foundry package), so tests add that directory to
``sys.path`` and import the writer classes directly.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")


@pytest.fixture(scope="module")
def handler_module():
    """Import the handler module (adding its dir to sys.path)."""
    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)
    return importlib.import_module("handler")


# --- ArtifactWriteResult model --------------------------------------------- #


def test_artifact_write_result_is_frozen(handler_module):
    """ArtifactWriteResult is frozen + extra='forbid' (audit integrity)."""
    result = handler_module.ArtifactWriteResult(
        artifact_uri="artifact://fake/test1",
        artifact_sha256=hashlib.sha256(b"x").hexdigest(),
        artifact_size_bytes=1,
        artifact_format="pickle",
        write_receipt="r" * 64,
    )
    with pytest.raises(Exception):
        result.artifact_uri = "artifact://fake/tampered"  # frozen


def test_artifact_write_result_rejects_unknown_fields(handler_module):
    """extra='forbid' rejects unknown fields (fail-closed)."""
    with pytest.raises(Exception):
        handler_module.ArtifactWriteResult(
            artifact_uri="artifact://fake/test1",
            artifact_sha256=hashlib.sha256(b"x").hexdigest(),
            artifact_size_bytes=1,
            artifact_format="pickle",
            write_receipt="r" * 64,
            evil_field="tamper",
        )


def test_write_receipt_verifies(handler_module):
    """verify_receipt() recomputes the HMAC and matches (constant-time)."""
    secret = "test-secret"
    uri = "artifact://fake/verify1"
    sha = hashlib.sha256(b"model-bytes").hexdigest()
    size = len(b"model-bytes")
    fmt = "pickle"
    receipt = handler_module._sign_artifact_metadata(
        artifact_uri=uri,
        artifact_sha256=sha,
        artifact_size_bytes=size,
        artifact_format=fmt,
        secret=secret,
    )
    result = handler_module.ArtifactWriteResult(
        artifact_uri=uri,
        artifact_sha256=sha,
        artifact_size_bytes=size,
        artifact_format=fmt,
        write_receipt=receipt,
    )
    assert result.verify_receipt(secret=secret) is True
    # Wrong secret → mismatch (fail-closed).
    assert result.verify_receipt(secret="wrong-secret") is False
    # Empty secret → fail-closed.
    assert result.verify_receipt(secret="") is False


# --- URI scheme validation ------------------------------------------------- #


def test_allowed_uri_schemes_accepted(handler_module):
    """file://, https://, artifact:// are allowed."""
    for uri in (
        "file:///runpod-volume/artifacts/a/model.pkl",
        "https://s3.example.com/bucket/key",
        "artifact://fake/test1",
    ):
        # Should not raise.
        handler_module._validate_artifact_uri_scheme(uri)


def test_disallowed_uri_scheme_rejected(handler_module):
    """http://, ftp://, and arbitrary schemes are rejected (fail-closed)."""
    for uri in (
        "http://insecure.example.com/artifact.pkl",
        "ftp://ftp.example.com/artifact.pkl",
        "s3://bucket/key",
        "evil://attacker/payload",
    ):
        with pytest.raises(ValueError, match=r"disallowed|no scheme|non-empty"):
            handler_module._validate_artifact_uri_scheme(uri)


def test_empty_uri_rejected(handler_module):
    """Empty URI is rejected (fail-closed)."""
    with pytest.raises(ValueError):
        handler_module._validate_artifact_uri_scheme("")
    with pytest.raises(ValueError):
        handler_module._validate_artifact_uri_scheme("   ")


# --- FakeArtifactWriter ---------------------------------------------------- #


def test_fake_writer_computes_expected_sha(handler_module):
    """FakeArtifactWriter computes expected sha256 without writing."""
    secret = "fake-secret"
    writer = handler_module.FakeArtifactWriter(callback_secret=secret)
    model_bytes = b"fake-model-bytes-for-testing"
    result = writer.write_artifact(
        model_bytes=model_bytes,
        artifact_id="artifact:deadbeef",
        artifact_format="pickle",
    )
    expected_sha = hashlib.sha256(model_bytes).hexdigest()
    assert result.artifact_sha256 == expected_sha
    assert result.artifact_size_bytes == len(model_bytes)
    assert result.artifact_format == "pickle"
    assert result.artifact_uri == "artifact://fake/artifact:deadbeef"
    # Write receipt verifies with the secret.
    assert result.verify_receipt(secret=secret) is True


def test_fake_writer_rejects_empty_bytes(handler_module):
    """FakeArtifactWriter fails closed on empty bytes."""
    writer = handler_module.FakeArtifactWriter(callback_secret="s")
    with pytest.raises(ValueError, match=r"empty"):
        writer.write_artifact(b"", "artifact:x", "pickle")


def _runpod_request_for_family(model_family: str, extra: dict[str, str] | None = None):
    from quant_foundry.schemas import RunPodTrainingRequest

    return RunPodTrainingRequest(
        job_id=f"route-{model_family}",
        dataset_manifest_ref="/tmp/route.csv",
        model_family=model_family,
        search_space={"n_estimators": [3], "max_depth": [2], "learning_rate": [0.1]},
        random_seed=7,
        hardware_class="test-gpu",
        extra_constraints=extra or {},
    )


def _tree_route_extra() -> dict[str, str]:
    return {
        "column_roles": json.dumps(
            {
                "feature_columns": ["f1", "f2"],
                "label_columns": ["label"],
                "timestamp_column": "ts",
            }
        ),
        "task_spec": json.dumps(
            {
                "task_type": "binary",
                "label_column": "label",
            }
        ),
    }


def test_runpod_real_trainer_routes_catboost_gpu(handler_module, monkeypatch):
    monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

    trainer = handler_module._build_trainer(
        _runpod_request_for_family("catboost_gpu", _tree_route_extra()),
        n_folds=2,
    )

    assert trainer.backend == "catboost"
    assert trainer.column_roles.feature_columns == ("f1", "f2")
    assert trainer.task_spec.task_type == "binary"
    assert trainer.n_folds == 2


def test_runpod_real_trainer_routes_xgboost_gpu(handler_module, monkeypatch):
    monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

    trainer = handler_module._build_trainer(
        _runpod_request_for_family("xgboost_gpu", _tree_route_extra()),
        n_folds=2,
    )

    assert trainer.backend == "xgboost"
    assert trainer.column_roles.label_columns == ("label",)
    assert trainer.task_spec.label_column == "label"


def test_runpod_real_trainer_rejects_unrouted_family(handler_module, monkeypatch):
    from quant_foundry.runpod_training import TrainingFailure

    monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

    with pytest.raises(TrainingFailure, match="does not yet share"):
        handler_module._build_trainer(
            _runpod_request_for_family("tabm_gpu", _tree_route_extra()),
        )


def test_runpod_tree_gpu_family_requires_roles(handler_module, monkeypatch):
    from quant_foundry.runpod_training import TrainingFailure

    monkeypatch.setenv("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

    with pytest.raises(TrainingFailure, match="requires explicit column_roles"):
        handler_module._build_trainer(_runpod_request_for_family("catboost_gpu"))


# --- VolumeArtifactWriter -------------------------------------------------- #


def test_volume_writer_writes_and_verifies(handler_module, tmp_path):
    """VolumeArtifactWriter writes bytes, verifies sha, returns file:// URI."""
    secret = "volume-secret"
    out_dir = tmp_path / "artifacts" / "test1"
    writer = handler_module.VolumeArtifactWriter(
        output_dir=out_dir,
        callback_payload_bytes=json.dumps({"payload": {}}).encode(),
        artifact_manifest_dict={"artifact_id": "a"},
        dossier_dict={"model_id": "m"},
        callback_secret=secret,
    )
    model_bytes = b"volume-model-bytes"
    result = writer.write_artifact(
        model_bytes=model_bytes,
        artifact_id="artifact:abc123",
        artifact_format="pickle",
    )
    expected_sha = hashlib.sha256(model_bytes).hexdigest()
    assert result.artifact_sha256 == expected_sha
    assert result.artifact_size_bytes == len(model_bytes)
    assert result.artifact_uri.startswith("file://")
    assert result.artifact_uri.endswith("model.pkl")
    # The model file was actually written.
    model_path = out_dir / "model.pkl"
    assert model_path.exists()
    assert model_path.read_bytes() == model_bytes
    # Sidecars written.
    assert (out_dir / "callback_envelope.json").exists()
    assert (out_dir / "artifact_manifest.json").exists()
    assert (out_dir / "dossier.json").exists()
    # Write receipt verifies.
    assert result.verify_receipt(secret=secret) is True


def test_volume_writer_detects_sha_mismatch(handler_module, tmp_path):
    """VolumeArtifactWriter fails closed if written bytes don't match."""
    secret = "volume-secret"
    out_dir = tmp_path / "artifacts" / "mismatch"
    writer = handler_module.VolumeArtifactWriter(
        output_dir=out_dir,
        callback_payload_bytes=json.dumps({"payload": {}}).encode(),
        artifact_manifest_dict={},
        dossier_dict={},
        callback_secret=secret,
    )
    # Patch read_bytes to return corrupted data → sha mismatch detected.
    model_bytes = b"original-bytes"
    with patch("pathlib.Path.read_bytes", return_value=b"corrupted"):
        with pytest.raises(ValueError, match=r"sha256/size mismatch"):
            writer.write_artifact(
                model_bytes=model_bytes,
                artifact_id="artifact:xyz",
                artifact_format="pickle",
            )


def test_volume_writer_rejects_empty_bytes(handler_module, tmp_path):
    """VolumeArtifactWriter fails closed on empty bytes."""
    writer = handler_module.VolumeArtifactWriter(
        output_dir=tmp_path / "out",
        callback_payload_bytes=b"{}",
        artifact_manifest_dict={},
        dossier_dict={},
        callback_secret="s",
    )
    with pytest.raises(ValueError, match=r"empty"):
        writer.write_artifact(b"", "artifact:x", "pickle")


# --- PresignedUploadArtifactWriter ----------------------------------------- #


def test_presigned_writer_rejects_http_scheme(handler_module):
    """PresignedUploadArtifactWriter rejects http:// (insecure) at construction."""
    with pytest.raises(ValueError, match=r"disallowed"):
        handler_module.PresignedUploadArtifactWriter(
            presigned_url="http://insecure.example.com/upload",
            callback_secret="s",
        )


def test_presigned_writer_rejects_ftp_scheme(handler_module):
    """PresignedUploadArtifactWriter rejects ftp:// at construction."""
    with pytest.raises(ValueError, match=r"disallowed"):
        handler_module.PresignedUploadArtifactWriter(
            presigned_url="ftp://ftp.example.com/upload",
            callback_secret="s",
        )


def test_presigned_writer_uploads_via_put(handler_module):
    """PresignedUploadArtifactWriter uploads via HTTP PUT and verifies 200."""
    secret = "presigned-secret"
    url = "https://s3.example.com/bucket/artifact.pkl"
    writer = handler_module.PresignedUploadArtifactWriter(
        presigned_url=url,
        callback_secret=secret,
    )
    model_bytes = b"presigned-model-bytes"

    # Mock urlopen to return a 200 response.
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.getcode.return_value = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("handler.urlopen", return_value=mock_resp) as mock_open:
        result = writer.write_artifact(
            model_bytes=model_bytes,
            artifact_id="artifact:presigned1",
            artifact_format="pickle",
        )
    # Verify the PUT request was made with the right method + body.
    mock_open.assert_called_once()
    req = mock_open.call_args[0][0]
    assert req.method == "PUT"
    assert req.data == model_bytes
    # Result carries the presigned URL as the URI.
    assert result.artifact_uri == url
    expected_sha = hashlib.sha256(model_bytes).hexdigest()
    assert result.artifact_sha256 == expected_sha
    assert result.artifact_size_bytes == len(model_bytes)
    assert result.verify_receipt(secret=secret) is True


def test_presigned_writer_fails_on_non_200(handler_module):
    """PresignedUploadArtifactWriter fails closed on non-200 response."""
    writer = handler_module.PresignedUploadArtifactWriter(
        presigned_url="https://s3.example.com/bucket/fail.pkl",
        callback_secret="s",
    )
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.getcode.return_value = 403
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("handler.urlopen", return_value=mock_resp):
        with pytest.raises(ValueError, match=r"HTTP 403"):
            writer.write_artifact(b"bytes", "artifact:x", "pickle")


def test_presigned_writer_fails_on_network_error(handler_module):
    """PresignedUploadArtifactWriter fails closed on network error."""
    writer = handler_module.PresignedUploadArtifactWriter(
        presigned_url="https://s3.example.com/bucket/neterr.pkl",
        callback_secret="s",
    )
    with patch("handler.urlopen", side_effect=ConnectionError("timeout")):
        with pytest.raises(ValueError, match=r"presigned artifact upload failed"):
            writer.write_artifact(b"bytes", "artifact:x", "pickle")


# --- Signed failure envelope ----------------------------------------------- #


def test_build_artifact_write_failure_callback_is_signed(handler_module):
    """_build_artifact_write_failure_callback produces a signed envelope."""
    import os

    secret = "failure-secret"
    old = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET")
    os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = secret
    try:
        envelope = handler_module._build_artifact_write_failure_callback(
            job_id="qf:train:fail:write:1",
            error_summary="disk full",
        )
    finally:
        if old is not None:
            os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = old
        else:
            os.environ.pop("QUANT_FOUNDRY_CALLBACK_SECRET", None)

    assert envelope["error_code"] == "artifact_write_failed"
    assert envelope["error_summary"] == "disk full"
    assert envelope["job_id"] == "qf:train:fail:write:1"
    assert envelope["callback_signature"]
    assert envelope["callback_ts"]
    # The callback payload is valid JSON with the right result_type.
    payload = json.loads(envelope["callback_payload"])
    assert payload["result_type"] == "artifact_write_failed"
    assert payload["payload"]["error_code"] == "artifact_write_failed"
    # Signature verifies with the secret.
    from quant_foundry.signatures import verify_callback

    assert verify_callback(
        envelope["callback_payload"].encode("utf-8"),
        envelope["callback_signature"],
        secret=secret,
        ts=envelope["callback_ts"],
        job_id="qf:train:fail:write:1",
    )


# --- Handler integration: disallowed URI scheme → signed failure ----------- #


def _make_training_input(job_id: str, **extra) -> dict:
    """Build a minimal training input dict for the handler."""
    return {
        "input": {
            "job_id": job_id,
            "dataset_manifest_ref": "ds-manifest-test",
            "model_family": "gbm",
            "search_space": {"n_estimators": [100]},
            "random_seed": 42,
            "hardware_class": "mock-gpu",
            "extra_constraints": {},
            **extra,
        }
    }


def _make_load_spec(
    *,
    manifest_dict: dict | None = None,
    data_csv: str = "feature_1,feature_2,label\n1.0,2.0,0\n3.0,4.0,1\n",
) -> dict:
    """Build a dataset_load_spec with an inline manifest + inline data.

    The manifest is written to a temp file and referenced via
    ``manifest_uri``. The data is written to a temp CSV file and
    referenced via ``data_uri``. Both use local file paths so the
    ManifestDatasetLoader can fetch them locally.
    """
    import tempfile

    if manifest_dict is None:
        manifest_dict = {
            "schema_version": 1,
            "dataset_id": "test-dataset",
            "feature_schema_hash": "a" * 64,
            "label_schema_hash": "b" * 64,
            "as_of_ts": 1700000000_000_000_000,
            "universe_hash": "c" * 64,
            "row_count": 2,
            "checksum": "d" * 64,
            "folds": {},
            "pit_proof_verified": True,
            "source_vintage_refs": [],
            "quality_report_hash": None,
            "manifest_uri": "",
            "data_uri": "",
            "data_format": "csv",
            "data_sha256": "",
            "quality_report_uri": None,
            "quality_report_sha256": None,
            "feature_names": ["feature_1", "feature_2"],
        }
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="qf_artifact_test_"))
    manifest_path = tmp_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_dict), encoding="utf-8")
    data_path = tmp_dir / "data.csv"
    data_path.write_text(data_csv, encoding="utf-8")

    manifest_bytes = manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    data_bytes = data_path.read_bytes()
    data_sha = hashlib.sha256(data_bytes).hexdigest()

    return {
        "manifest_uri": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "data_uri": str(data_path),
        "data_sha256": data_sha,
        "data_format": "csv",
        "row_count": 2,
        "feature_schema_hash": manifest_dict.get("feature_schema_hash", ""),
        "label_schema_hash": manifest_dict.get("label_schema_hash", ""),
    }


def test_handler_rejects_disallowed_presigned_uri_scheme(handler_module, monkeypatch):
    """Handler rejects http:// presigned URL with a signed failure envelope."""
    secret = "handler-integration-secret"
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", secret)
    # Use a canary request (local trainer) so no GPU/ML deps needed.
    event = _make_training_input(
        "qf:train:reject:1",
        presigned_artifact_url="http://insecure.example.com/upload",
    )
    result = handler_module.handler(event)
    assert result["error_code"] == "artifact_write_failed"
    assert result["job_id"] == "qf:train:reject:1"
    # Signed failure envelope.
    assert result["callback_signature"]
    assert result["callback_payload"]
    payload = json.loads(result["callback_payload"])
    assert payload["result_type"] == "artifact_write_failed"


def test_handler_fake_writer_canary_no_persistence(handler_module, monkeypatch):
    """Canary run with no output_prefix/presigned URL uses FakeArtifactWriter."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "canary-secret")
    event = _make_training_input("qf:train:canary:fake:1")
    result = handler_module.handler(event)
    # Success (canary uses local trainer).
    assert result.get("job_id") == "qf:train:canary:fake:1"
    assert "error_code" not in result
    # FakeArtifactWriter produces an artifact://fake/ URI.
    artifact = result["artifact_result"]
    assert artifact["artifact_uri"].startswith("artifact://fake/")
    assert artifact["artifact_sha256"]
    assert artifact["artifact_size_bytes"] > 0
    # Write receipt is present and verifies.
    assert artifact["write_receipt"] is not None
    receipt = result["artifact_write_receipt"]
    assert receipt is not None


def test_handler_volume_writer_persists_artifact(handler_module, monkeypatch, tmp_path):
    """Handler with output_prefix uses VolumeArtifactWriter and persists."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "volume-int-secret")
    out_dir = str(tmp_path / "vol_out")
    event = _make_training_input(
        "qf:train:vol:1",
        output_prefix=out_dir,
    )
    result = handler_module.handler(event)
    assert result.get("job_id") == "qf:train:vol:1"
    assert "error_code" not in result
    artifact = result["artifact_result"]
    assert artifact["artifact_uri"].startswith("file://")
    assert artifact["artifact_uri"].endswith("model.pkl")
    # Model file exists on disk.
    model_path = tmp_path / "vol_out" / "model.pkl"
    assert model_path.exists()
    # Write receipt verifies.
    assert result["artifact_write_receipt"] is not None


# --- Durable artifact deny gate (Tier 0.2) --------------------------------- #
#
# Tests for the /tmp deny gate and output_prefix validation. The deny gate
# fires for non-canary jobs (training_mode != "canary") when the resolved
# output_prefix is under /tmp or no durable destination is supplied. Canary
# jobs may use /tmp (FakeArtifactWriter is canary-only by design).


def test_is_under_tmp_detects_tmp_paths(handler_module):
    """_is_under_tmp detects /tmp and /tmp/... paths."""
    assert handler_module._is_under_tmp("/tmp") is True
    assert handler_module._is_under_tmp("/tmp/foo") is True
    assert handler_module._is_under_tmp("/tmp/a/b/c") is True
    assert handler_module._is_under_tmp("file:///tmp/foo") is True
    # Non-/tmp paths are not under /tmp.
    assert handler_module._is_under_tmp("/runpod-volume/artifacts") is False
    assert handler_module._is_under_tmp("/workspace/artifacts") is False
    assert handler_module._is_under_tmp("/var/tmp/foo") is False
    assert handler_module._is_under_tmp("") is False
    assert handler_module._is_under_tmp(None) is False


def test_validate_output_prefix_denies_tmp_for_real_jobs(handler_module):
    """_validate_output_prefix_durable denies /tmp for non-canary jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/tmp/a7-train-artifacts",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is not None
    assert "/tmp" in err
    assert "durable" in err.lower() or "die with the worker" in err


def test_validate_output_prefix_denies_tmp_for_research_mode(handler_module):
    """Deny gate fires for research mode too (not just production)."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/tmp/research-out",
        presigned_artifact_url=None,
        training_mode="research",
    )
    assert err is not None
    assert "/tmp" in err


def test_validate_output_prefix_allows_tmp_for_canary(handler_module):
    """Canary jobs may use /tmp (canary-only by design)."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/tmp/canary-out",
        presigned_artifact_url=None,
        training_mode="canary",
    )
    assert err is None


def test_validate_output_prefix_rejects_invalid_prefix(handler_module):
    """Non-volume, non-URI prefixes are rejected for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/var/tmp/artifacts",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is not None
    assert "durable" in err.lower() or "not a durable" in err.lower()


def test_validate_output_prefix_accepts_runpod_volume(handler_module):
    """/runpod-volume/ paths are accepted for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/runpod-volume/artifacts/train1",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is None


def test_validate_output_prefix_accepts_workspace(handler_module):
    """/workspace/ paths are accepted for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="/workspace/artifacts/train1",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is None


def test_validate_output_prefix_accepts_presigned_url(handler_module):
    """Presigned https:// URL is accepted for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix=None,
        presigned_artifact_url="https://s3.example.com/bucket/model.pkl",
        training_mode="production",
    )
    assert err is None


def test_validate_output_prefix_accepts_s3_uri(handler_module):
    """s3:// URI is accepted for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="s3://my-bucket/artifacts/model.pkl",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is None


def test_validate_output_prefix_rejects_file_uri_to_tmp(handler_module):
    """file:// URI to /tmp is rejected for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="file:///tmp/artifacts/train1",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is not None
    assert "/tmp" in err


def test_validate_output_prefix_accepts_file_uri_to_volume(handler_module):
    """file:// URI to /runpod-volume/ is accepted for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="file:///runpod-volume/artifacts/train1",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is None


def test_validate_output_prefix_rejects_no_destination_for_real_job(handler_module):
    """No output_prefix and no presigned URL fails for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix=None,
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is not None
    assert "durable" in err.lower() or "output_prefix" in err.lower()


def test_validate_output_prefix_rejects_file_uri_to_non_volume(handler_module):
    """file:// URI to a non-volume path is rejected for real jobs."""
    err = handler_module._validate_output_prefix_durable(
        output_prefix="file:///opt/artifacts/train1",
        presigned_artifact_url=None,
        training_mode="production",
    )
    assert err is not None
    assert "file://" in err or "non-volume" in err


# --- Handler integration: /tmp deny gate ----------------------------------- #


def test_handler_denies_tmp_for_real_jobs(handler_module, monkeypatch):
    """Handler rejects /tmp output_prefix for non-canary jobs (signed failure)."""
    secret = "deny-gate-secret"
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", secret)
    # Tier 1.5: production mode requires dataset_load_spec. Provide one so
    # the test reaches the artifact deny gate (not the dataset guard).
    load_spec = _make_load_spec()
    # Use a canary request (local trainer) but set training_mode=production
    # so the deny gate fires. No GPU/ML deps needed (local trainer).
    event = _make_training_input(
        "qf:train:deny:tmp:1",
        output_prefix="/tmp/a7-train-artifacts",
        dataset_load_spec=load_spec,
        extra_constraints={"training_mode": "production"},
    )
    result = handler_module.handler(event)
    # Signed failure envelope.
    assert result["error_code"] == "artifact_destination_not_durable"
    assert result["job_id"] == "qf:train:deny:tmp:1"
    assert result["callback_signature"]
    assert result["callback_payload"]
    payload = json.loads(result["callback_payload"])
    assert payload["result_type"] == "artifact_destination_not_durable"
    # Error message names /tmp and the missing durable destination.
    error_msg = result["error_summary"]
    assert "/tmp" in error_msg
    assert "durable" in error_msg.lower() or "die with the worker" in error_msg.lower()


def test_handler_allows_tmp_for_canary_jobs(handler_module, monkeypatch, tmp_path):
    """Handler allows /tmp output_prefix for canary jobs (canary-only by design)."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "canary-tmp-secret")
    out_dir = str(tmp_path / "canary_tmp_out")
    event = _make_training_input(
        "qf:train:canary:tmp:1",
        output_prefix=out_dir,
        extra_constraints={"training_mode": "canary"},
    )
    result = handler_module.handler(event)
    assert result.get("job_id") == "qf:train:canary:tmp:1"
    assert "error_code" not in result
    artifact = result["artifact_result"]
    assert artifact["artifact_uri"].startswith("file://")


def test_handler_denies_invalid_prefix_for_real_jobs(handler_module, monkeypatch):
    """Handler rejects non-volume, non-URI output_prefix for real jobs."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "deny-invalid-secret")
    load_spec = _make_load_spec()
    event = _make_training_input(
        "qf:train:deny:invalid:1",
        output_prefix="/var/tmp/artifacts",
        dataset_load_spec=load_spec,
        extra_constraints={"training_mode": "production"},
    )
    result = handler_module.handler(event)
    assert result["error_code"] == "artifact_destination_not_durable"
    assert result["job_id"] == "qf:train:deny:invalid:1"
    assert result["callback_signature"]


def test_handler_denies_no_destination_for_real_jobs(handler_module, monkeypatch):
    """Handler fails closed when no durable destination is supplied for real jobs."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "deny-nodest-secret")
    load_spec = _make_load_spec()
    event = _make_training_input(
        "qf:train:deny:nodest:1",
        dataset_load_spec=load_spec,
        extra_constraints={"training_mode": "production"},
    )
    result = handler_module.handler(event)
    assert result["error_code"] == "artifact_destination_not_durable"
    assert result["job_id"] == "qf:train:deny:nodest:1"
    assert result["callback_signature"]
