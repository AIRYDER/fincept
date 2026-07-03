"""
TDD tests for quant_foundry.runtime_fingerprint (T-5.2 Runtime Fingerprint).

Covers:
- RuntimeFingerprintConfig construction + frozen/extra-forbid.
- RuntimeFingerprint construction + hash/signature validators.
- RuntimeFingerprintBuilder.collect_* methods (with mocking).
- RuntimeFingerprintBuilder.build (full + canary).
- compute_fingerprint_hash (determinism, order-independence).
- sign_fingerprint (valid, wrong secret).
- validate_fingerprint (valid, missing image_digest, placeholder, missing git_sha).
- verify_signature (valid, invalid, no signature).
- serialize/deserialize round-trip.
- Fail-closed production checks.
- Canary mode (missing image_digest allowed but promotion_eligible=False).
- Edge cases: no git repo, no Dockerfile, no torch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from quant_foundry.runtime_fingerprint import (
    FingerprintValidationError,
    RuntimeFingerprint,
    RuntimeFingerprintBuilder,
    RuntimeFingerprintConfig,
    deserialize_fingerprint,
    serialize_fingerprint,
    validate_fingerprint,
    verify_signature,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SECRET = "test-callback-secret-1234"


def _make_fingerprint(
    *,
    git_sha: str | None = "abc123def456",
    image_digest: str | None = "sha256:deadbeef",
    dockerfile_hash: str | None = "dockerhash",
    dependency_lock_hash: str | None = "dephash",
    python_version: str = "3.11.5 (main, ...)",
    os_info: str = "Linux-6.5.0-x86_64",
    cuda_version: str | None = "12.1",
    driver_version: str | None = "545.23.8",
    gpu_model: str | None = "NVIDIA GeForce RTX 4090",
    library_versions: dict[str, str] | None = None,
    random_seeds: dict[str, int] | None = None,
    dataset_manifest_hash: str | None = "dataset-hash",
    training_manifest_hash: str | None = "training-hash",
    fingerprint_hash: str = "0" * 64,
    signature: str | None = None,
    created_at: str | None = None,
    is_canary: bool = False,
    promotion_eligible: bool = True,
) -> RuntimeFingerprint:
    """Build a RuntimeFingerprint with sensible defaults for tests."""
    return RuntimeFingerprint(
        git_sha=git_sha,
        image_digest=image_digest,
        dockerfile_hash=dockerfile_hash,
        dependency_lock_hash=dependency_lock_hash,
        python_version=python_version,
        os_info=os_info,
        cuda_version=cuda_version,
        driver_version=driver_version,
        gpu_model=gpu_model,
        library_versions=library_versions if library_versions is not None else {"numpy": "1.24.0"},
        random_seeds=random_seeds if random_seeds is not None else {"python_seed": 42},
        dataset_manifest_hash=dataset_manifest_hash,
        training_manifest_hash=training_manifest_hash,
        fingerprint_hash=fingerprint_hash,
        signature=signature,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        is_canary=is_canary,
        promotion_eligible=promotion_eligible,
    )


@pytest.fixture
def builder() -> RuntimeFingerprintBuilder:
    """A builder with production_mode disabled for unit tests."""
    return RuntimeFingerprintBuilder(RuntimeFingerprintConfig(production_mode=False))


@pytest.fixture
def signed_fingerprint(builder: RuntimeFingerprintBuilder) -> RuntimeFingerprint:
    """A fingerprint with a valid HMAC signature under SECRET."""
    fp = _make_fingerprint()
    real_hash = builder.compute_fingerprint_hash(fp)
    sig = builder.sign_fingerprint(real_hash, SECRET)
    return fp.model_copy(update={"fingerprint_hash": real_hash, "signature": sig})


# ---------------------------------------------------------------------------
# RuntimeFingerprintConfig
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    """Config has all include_* flags True and production_mode True by default."""
    cfg = RuntimeFingerprintConfig()
    assert cfg.include_git_sha is True
    assert cfg.include_image_digest is True
    assert cfg.include_dockerfile_hash is True
    assert cfg.include_dependency_hash is True
    assert cfg.include_python_version is True
    assert cfg.include_os_info is True
    assert cfg.include_cuda_info is True
    assert cfg.include_gpu_info is True
    assert cfg.include_library_versions is True
    assert cfg.include_random_seeds is True
    assert cfg.include_dataset_hash is True
    assert cfg.include_training_manifest_hash is True
    assert cfg.production_mode is True
    assert cfg.callback_secret_env == "CALLBACK_SECRET"


def test_config_frozen() -> None:
    """Config is frozen and cannot be mutated."""
    cfg = RuntimeFingerprintConfig()
    with pytest.raises(ValidationError):
        cfg.include_git_sha = False  # type: ignore[misc]


def test_config_extra_forbid() -> None:
    """Config rejects unknown fields."""
    with pytest.raises(ValidationError):
        RuntimeFingerprintConfig(unknown_field=True)  # type: ignore[call-arg]


def test_config_custom_callback_secret_env() -> None:
    """Config accepts a custom callback secret env var name."""
    cfg = RuntimeFingerprintConfig(callback_secret_env="MY_SECRET")
    assert cfg.callback_secret_env == "MY_SECRET"


# ---------------------------------------------------------------------------
# RuntimeFingerprint model
# ---------------------------------------------------------------------------


def test_fingerprint_construction_defaults() -> None:
    """A fingerprint with required fields constructs successfully."""
    fp = _make_fingerprint()
    assert fp.git_sha == "abc123def456"
    assert fp.image_digest == "sha256:deadbeef"
    assert fp.is_canary is False
    assert fp.promotion_eligible is True


def test_fingerprint_frozen() -> None:
    """Fingerprint is frozen."""
    fp = _make_fingerprint()
    with pytest.raises(ValidationError):
        fp.git_sha = "changed"  # type: ignore[misc]


def test_fingerprint_extra_forbid() -> None:
    """Fingerprint rejects unknown fields."""
    with pytest.raises(ValidationError):
        RuntimeFingerprint(
            python_version="3.11",
            os_info="linux",
            fingerprint_hash="0" * 64,
            created_at="2024-01-01T00:00:00Z",
            unknown_field=True,  # type: ignore[call-arg]
        )


def test_fingerprint_hash_must_be_64_hex() -> None:
    """fingerprint_hash must be exactly 64 hex chars."""
    with pytest.raises(ValidationError):
        _make_fingerprint(fingerprint_hash="abc")
    with pytest.raises(ValidationError):
        _make_fingerprint(fingerprint_hash="z" * 64)  # non-hex


def test_fingerprint_hash_uppercase_normalized_to_lower() -> None:
    """Uppercase hex fingerprint_hash is normalized to lowercase."""
    fp = _make_fingerprint(fingerprint_hash="A" * 64)
    assert fp.fingerprint_hash == "a" * 64


def test_signature_must_be_64_hex_or_none() -> None:
    """signature must be 64 hex chars or None."""
    # None is valid
    fp = _make_fingerprint(signature=None)
    assert fp.signature is None
    # 64 hex valid
    fp2 = _make_fingerprint(signature="a" * 64)
    assert fp2.signature == "a" * 64
    # wrong length invalid
    with pytest.raises(ValidationError):
        _make_fingerprint(signature="abc")
    # non-hex invalid
    with pytest.raises(ValidationError):
        _make_fingerprint(signature="z" * 64)


# ---------------------------------------------------------------------------
# RuntimeFingerprintBuilder.collect_*
# ---------------------------------------------------------------------------


def test_collect_git_sha_success(builder: RuntimeFingerprintBuilder) -> None:
    """collect_git_sha returns the SHA when git is available."""
    fake_result = type(
        "R",
        (),
        {"returncode": 0, "stdout": "abcdef1234567890\n", "stderr": ""},
    )()
    with patch("quant_foundry.runtime_fingerprint.subprocess.run", return_value=fake_result):
        sha = builder.collect_git_sha()
    assert sha == "abcdef1234567890"


def test_collect_git_sha_no_repo(builder: RuntimeFingerprintBuilder) -> None:
    """collect_git_sha returns None when git fails (not a repo)."""
    fake_result = type(
        "R",
        (),
        {"returncode": 128, "stdout": "", "stderr": "fatal: not a git repository"},
    )()
    with patch("quant_foundry.runtime_fingerprint.subprocess.run", return_value=fake_result):
        assert builder.collect_git_sha() is None


def test_collect_git_sha_no_git_binary(builder: RuntimeFingerprintBuilder) -> None:
    """collect_git_sha returns None when git binary is missing."""
    with patch(
        "quant_foundry.runtime_fingerprint.subprocess.run",
        side_effect=FileNotFoundError(),
    ):
        assert builder.collect_git_sha() is None


def test_collect_image_digest_from_env(builder: RuntimeFingerprintBuilder) -> None:
    """collect_image_digest reads IMAGE_DIGEST env var."""
    with patch.dict(os.environ, {"IMAGE_DIGEST": "sha256:abc123"}):
        assert builder.collect_image_digest() == "sha256:abc123"


def test_collect_image_digest_missing(builder: RuntimeFingerprintBuilder) -> None:
    """collect_image_digest returns None when env var is absent."""
    env = {k: v for k, v in os.environ.items() if k != "IMAGE_DIGEST"}
    with patch.dict(os.environ, env, clear=True):
        assert builder.collect_image_digest() is None


def test_collect_dockerfile_hash_success(
    builder: RuntimeFingerprintBuilder, tmp_path: Any
) -> None:
    """collect_dockerfile_hash computes SHA-256 of the Dockerfile."""
    dockerfile = tmp_path / "Dockerfile"
    content = b"FROM python:3.11\nRUN pip install numpy\n"
    dockerfile.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert builder.collect_dockerfile_hash(str(dockerfile)) == expected


def test_collect_dockerfile_hash_missing(builder: RuntimeFingerprintBuilder) -> None:
    """collect_dockerfile_hash returns None when the file is absent."""
    assert builder.collect_dockerfile_hash("/nonexistent/Dockerfile") is None


def test_collect_dependency_hash_success(
    builder: RuntimeFingerprintBuilder, tmp_path: Any
) -> None:
    """collect_dependency_hash computes SHA-256 of the requirements file."""
    req = tmp_path / "requirements.txt"
    content = b"numpy==1.24.0\npandas==2.0.0\n"
    req.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert builder.collect_dependency_hash(str(req)) == expected


def test_collect_dependency_hash_missing(builder: RuntimeFingerprintBuilder) -> None:
    """collect_dependency_hash returns None when the file is absent."""
    assert builder.collect_dependency_hash("/nonexistent/requirements.txt") is None


def test_collect_python_version(builder: RuntimeFingerprintBuilder) -> None:
    """collect_python_version returns sys.version."""
    import sys

    assert builder.collect_python_version() == sys.version


def test_collect_os_info(builder: RuntimeFingerprintBuilder) -> None:
    """collect_os_info returns platform.platform()."""
    import platform

    assert builder.collect_os_info() == platform.platform()


def test_collect_cuda_info_no_torch(builder: RuntimeFingerprintBuilder) -> None:
    """collect_cuda_info returns (None, None) when torch is not importable."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "torch":
            raise ImportError("no torch")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        cuda, driver = builder.collect_cuda_info()
    assert cuda is None
    assert driver is None


def test_collect_cuda_info_with_torch(builder: RuntimeFingerprintBuilder) -> None:
    """collect_cuda_info returns torch.version.cuda when torch is available."""
    fake_torch = type(
        "T",
        (),
        {
            "version": type("V", (), {"cuda": "12.1"})(),
            "cuda": type(
                "C",
                (),
                {
                    "is_available": staticmethod(lambda: False),
                },
            )(),
        },
    )()
    with patch.dict("sys.modules", {"torch": fake_torch}):
        cuda, driver = builder.collect_cuda_info()
    assert cuda == "12.1"
    assert driver is None


def test_collect_gpu_info_no_gpu(builder: RuntimeFingerprintBuilder) -> None:
    """collect_gpu_info returns None when no GPU is available."""
    with patch(
        "quant_foundry.tabular_neural_runtime.check_gpu",
        return_value=type(
            "S", (), {"available": False, "device_name": None}
        )(),
    ):
        assert builder.collect_gpu_info() is None


def test_collect_gpu_info_with_gpu(builder: RuntimeFingerprintBuilder) -> None:
    """collect_gpu_info returns the device name when a GPU is available."""
    with patch(
        "quant_foundry.tabular_neural_runtime.check_gpu",
        return_value=type(
            "S", (), {"available": True, "device_name": "NVIDIA RTX 4090"}
        )(),
    ):
        assert builder.collect_gpu_info() == "NVIDIA RTX 4090"


def test_collect_library_versions(builder: RuntimeFingerprintBuilder) -> None:
    """collect_library_versions returns versions for importable libraries."""
    versions = builder.collect_library_versions()
    # pydantic is always importable in the test env
    assert "pydantic" in versions
    assert isinstance(versions["pydantic"], str)


def test_collect_random_seeds(builder: RuntimeFingerprintBuilder) -> None:
    """collect_random_seeds returns a dict with python_seed."""
    with patch.dict(os.environ, {"PYTHONHASHSEED": "42"}):
        seeds = builder.collect_random_seeds()
    assert seeds["python_seed"] == 42


def test_collect_random_seeds_unset(builder: RuntimeFingerprintBuilder) -> None:
    """collect_random_seeds records 0 when PYTHONHASHSEED is unset."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
    with patch.dict(os.environ, env, clear=True):
        seeds = builder.collect_random_seeds()
    assert seeds["python_seed"] == 0


# ---------------------------------------------------------------------------
# compute_fingerprint_hash
# ---------------------------------------------------------------------------


def test_fingerprint_hash_determinism(builder: RuntimeFingerprintBuilder) -> None:
    """Same fingerprint fields produce the same hash."""
    ts = "2024-01-01T00:00:00Z"
    fp1 = _make_fingerprint(created_at=ts)
    fp2 = _make_fingerprint(created_at=ts)
    assert builder.compute_fingerprint_hash(fp1) == builder.compute_fingerprint_hash(fp2)


def test_fingerprint_hash_changes_with_fields(builder: RuntimeFingerprintBuilder) -> None:
    """Different field values produce different hashes."""
    ts = "2024-01-01T00:00:00Z"
    fp1 = _make_fingerprint(git_sha="aaa", created_at=ts)
    fp2 = _make_fingerprint(git_sha="bbb", created_at=ts)
    assert builder.compute_fingerprint_hash(fp1) != builder.compute_fingerprint_hash(fp2)


def test_fingerprint_hash_order_independence(builder: RuntimeFingerprintBuilder) -> None:
    """Dict field key order does not affect the hash."""
    ts = "2024-01-01T00:00:00Z"
    fp1 = _make_fingerprint(
        library_versions={"numpy": "1.24.0", "torch": "2.1.0"}, created_at=ts
    )
    fp2 = _make_fingerprint(
        library_versions={"torch": "2.1.0", "numpy": "1.24.0"}, created_at=ts
    )
    assert builder.compute_fingerprint_hash(fp1) == builder.compute_fingerprint_hash(fp2)


def test_fingerprint_hash_excludes_signature_and_hash(
    builder: RuntimeFingerprintBuilder,
) -> None:
    """The hash itself and signature do not affect the computed hash."""
    ts = "2024-01-01T00:00:00Z"
    fp1 = _make_fingerprint(fingerprint_hash="0" * 64, signature=None, created_at=ts)
    fp2 = _make_fingerprint(fingerprint_hash="1" * 64, signature="a" * 64, created_at=ts)
    assert builder.compute_fingerprint_hash(fp1) == builder.compute_fingerprint_hash(fp2)


def test_fingerprint_hash_is_64_hex(builder: RuntimeFingerprintBuilder) -> None:
    """The computed hash is a 64-char lowercase hex string."""
    h = builder.compute_fingerprint_hash(_make_fingerprint())
    assert len(h) == 64
    int(h, 16)  # raises if not hex


# ---------------------------------------------------------------------------
# sign_fingerprint
# ---------------------------------------------------------------------------


def test_sign_fingerprint_valid(builder: RuntimeFingerprintBuilder) -> None:
    """sign_fingerprint produces a valid 64-char HMAC-SHA256 hex."""
    sig = builder.sign_fingerprint("a" * 64, SECRET)
    assert len(sig) == 64
    int(sig, 16)
    expected = hmac.new(SECRET.encode(), (b"a" * 64), hashlib.sha256).hexdigest()
    assert sig == expected


def test_sign_fingerprint_wrong_secret(builder: RuntimeFingerprintBuilder) -> None:
    """Different secrets produce different signatures."""
    sig1 = builder.sign_fingerprint("a" * 64, SECRET)
    sig2 = builder.sign_fingerprint("a" * 64, "other-secret")
    assert sig1 != sig2


def test_sign_fingerprint_empty_secret_raises(builder: RuntimeFingerprintBuilder) -> None:
    """An empty secret raises ValueError."""
    with pytest.raises(ValueError):
        builder.sign_fingerprint("a" * 64, "")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_full_fingerprint() -> None:
    """build() produces a complete, signed fingerprint when a secret is set."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    with patch.dict(os.environ, {"CALLBACK_SECRET": SECRET, "IMAGE_DIGEST": "sha256:abc"}):
        with patch.object(b, "collect_git_sha", return_value="abc123"):
            fp = b.build()
    assert fp.git_sha == "abc123"
    assert fp.image_digest == "sha256:abc"
    assert fp.signature is not None
    assert len(fp.fingerprint_hash) == 64
    assert fp.is_canary is False
    assert fp.promotion_eligible is True


def test_build_canary_marks_ineligible() -> None:
    """A canary build is marked promotion_eligible=False even with digest+sha."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    with patch.dict(os.environ, {"CALLBACK_SECRET": SECRET, "IMAGE_DIGEST": "sha256:abc"}):
        with patch.object(b, "collect_git_sha", return_value="abc123"):
            fp = b.build(is_canary=True)
    assert fp.is_canary is True
    assert fp.promotion_eligible is False


def test_build_no_secret_no_signature() -> None:
    """When no callback secret is set the fingerprint has signature=None."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    env = {k: v for k, v in os.environ.items() if k != "CALLBACK_SECRET"}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(b, "collect_git_sha", return_value="abc123"):
            fp = b.build()
    assert fp.signature is None


def test_build_respects_include_flags() -> None:
    """When include_git_sha is False, git_sha is None on the built fingerprint."""
    cfg = RuntimeFingerprintConfig(include_git_sha=False, production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    with patch.object(b, "collect_git_sha", return_value="abc123") as mock_collect:
        fp = b.build()
    mock_collect.assert_not_called()
    assert fp.git_sha is None


def test_build_with_random_seeds_override() -> None:
    """Explicit random_seeds override the collected seeds."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    override = {"python_seed": 99, "numpy_seed": 7}
    with patch.object(b, "collect_git_sha", return_value="abc"):
        fp = b.build(random_seeds=override)
    assert fp.random_seeds == override


# ---------------------------------------------------------------------------
# validate_fingerprint
# ---------------------------------------------------------------------------


def test_validate_fingerprint_valid_non_production() -> None:
    """A valid fingerprint passes validation in non-production mode."""
    fp = _make_fingerprint(image_digest=None, git_sha=None)
    validate_fingerprint(fp, production_mode=False)


def test_validate_fingerprint_valid_production() -> None:
    """A fingerprint with digest+sha passes production validation."""
    fp = _make_fingerprint(image_digest="sha256:abc", git_sha="abc123")
    validate_fingerprint(fp, production_mode=True)


def test_validate_fingerprint_missing_digest_production() -> None:
    """Missing image_digest fails closed in production."""
    fp = _make_fingerprint(image_digest=None, git_sha="abc")
    with pytest.raises(FingerprintValidationError, match="image_digest"):
        validate_fingerprint(fp, production_mode=True)


def test_validate_fingerprint_placeholder_digest_production() -> None:
    """Placeholder image_digest fails closed in production."""
    fp = _make_fingerprint(image_digest="placeholder", git_sha="abc")
    with pytest.raises(FingerprintValidationError, match="placeholder"):
        validate_fingerprint(fp, production_mode=True)


def test_validate_fingerprint_missing_git_sha_production() -> None:
    """Missing git_sha fails closed in production."""
    fp = _make_fingerprint(image_digest="sha256:abc", git_sha=None)
    with pytest.raises(FingerprintValidationError, match="git_sha"):
        validate_fingerprint(fp, production_mode=True)


def test_validate_fingerprint_hash_mismatch() -> None:
    """A mismatched expected_hash fails closed."""
    fp = _make_fingerprint()
    with pytest.raises(FingerprintValidationError, match="expected hash"):
        validate_fingerprint(fp, expected_hash="1" * 64, production_mode=False)


def test_validate_fingerprint_hash_match() -> None:
    """A matching expected_hash passes."""
    fp = _make_fingerprint()
    validate_fingerprint(fp, expected_hash=fp.fingerprint_hash, production_mode=False)


def test_validate_fingerprint_canary_missing_digest_allowed() -> None:
    """Canary with missing digest is allowed in non-production mode."""
    fp = _make_fingerprint(image_digest=None, git_sha=None, is_canary=True, promotion_eligible=False)
    validate_fingerprint(fp, production_mode=False)


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


def test_verify_signature_valid(signed_fingerprint: RuntimeFingerprint) -> None:
    """A correctly signed fingerprint verifies True."""
    assert verify_signature(signed_fingerprint, SECRET) is True


def test_verify_signature_wrong_secret(signed_fingerprint: RuntimeFingerprint) -> None:
    """A wrong secret raises FingerprintValidationError."""
    with pytest.raises(FingerprintValidationError, match="signature"):
        verify_signature(signed_fingerprint, "wrong-secret")


def test_verify_signature_no_signature() -> None:
    """A fingerprint with no signature raises FingerprintValidationError."""
    fp = _make_fingerprint(signature=None)
    with pytest.raises(FingerprintValidationError, match="no signature"):
        verify_signature(fp, SECRET)


def test_verify_signature_empty_secret(signed_fingerprint: RuntimeFingerprint) -> None:
    """An empty secret raises FingerprintValidationError."""
    with pytest.raises(FingerprintValidationError, match="secret"):
        verify_signature(signed_fingerprint, "")


# ---------------------------------------------------------------------------
# serialize / deserialize
# ---------------------------------------------------------------------------


def test_serialize_deserialize_roundtrip(signed_fingerprint: RuntimeFingerprint) -> None:
    """serialize then deserialize reproduces an equivalent fingerprint."""
    s = serialize_fingerprint(signed_fingerprint)
    fp2 = deserialize_fingerprint(s)
    assert fp2 == signed_fingerprint


def test_serialize_returns_json_string(signed_fingerprint: RuntimeFingerprint) -> None:
    """serialize_fingerprint returns a valid JSON string."""
    s = serialize_fingerprint(signed_fingerprint)
    assert isinstance(s, str)
    parsed = json.loads(s)
    assert parsed["git_sha"] == signed_fingerprint.git_sha


def test_deserialize_invalid_json_raises() -> None:
    """Invalid JSON raises FingerprintValidationError."""
    with pytest.raises(FingerprintValidationError):
        deserialize_fingerprint("not-json{")


def test_deserialize_invalid_fingerprint_raises() -> None:
    """JSON that fails model validation raises FingerprintValidationError."""
    bad = json.dumps({"python_version": "3.11", "os_info": "linux"})  # missing required
    with pytest.raises(FingerprintValidationError):
        deserialize_fingerprint(bad)


# ---------------------------------------------------------------------------
# Integration: build -> validate -> verify
# ---------------------------------------------------------------------------


def test_build_validate_verify_integration() -> None:
    """A built fingerprint validates and its signature verifies."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    with patch.dict(os.environ, {"CALLBACK_SECRET": SECRET, "IMAGE_DIGEST": "sha256:abc"}):
        with patch.object(b, "collect_git_sha", return_value="abc123"):
            fp = b.build()
    validate_fingerprint(fp, production_mode=False)
    assert verify_signature(fp, SECRET) is True


def test_build_canary_not_promotion_eligible_integration() -> None:
    """A canary build with missing digest is not promotion eligible."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    env = {k: v for k, v in os.environ.items() if k != "IMAGE_DIGEST"}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(b, "collect_git_sha", return_value="abc"):
            fp = b.build(is_canary=True)
    assert fp.is_canary is True
    assert fp.promotion_eligible is False
    # Canary with missing digest is allowed in non-production validation
    validate_fingerprint(fp, production_mode=False)


def test_build_production_missing_digest_fails_validation() -> None:
    """A production build with missing digest fails production validation."""
    cfg = RuntimeFingerprintConfig(production_mode=False)
    b = RuntimeFingerprintBuilder(cfg)
    env = {k: v for k, v in os.environ.items() if k != "IMAGE_DIGEST"}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(b, "collect_git_sha", return_value="abc"):
            fp = b.build()
    with pytest.raises(FingerprintValidationError, match="image_digest"):
        validate_fingerprint(fp, production_mode=True)
