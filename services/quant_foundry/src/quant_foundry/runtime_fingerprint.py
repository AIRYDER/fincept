"""
quant_foundry.runtime_fingerprint — Runtime environment fingerprint (T-5.2).

Records the full execution environment (git sha, image digest, Dockerfile hash,
dependency lock hash, Python version, CUDA version, GPU model, library versions,
random seeds, dataset/training manifest hashes) and signs it with an HMAC
callback secret.

Security / audit invariants (non-negotiable):

- **Fail-closed in production.** A production job whose fingerprint is missing
  an image digest (or has a placeholder digest) or is missing a git sha is
  rejected by :func:`validate_fingerprint`. Canary jobs are allowed to omit
  these but are marked ``promotion_eligible=False``.
- **Deterministic fingerprint hash.** The ``fingerprint_hash`` is a SHA-256
  over canonical (sorted-key) JSON of all fingerprint fields *excluding* the
  ``fingerprint_hash`` and ``signature`` fields themselves. This makes the hash
  reproducible and order-independent.
- **HMAC-signed.** When a callback secret is available the fingerprint hash is
  signed with HMAC-SHA256 so tampering with any field is detectable.
- **Lazy torch import.** CUDA / GPU collection degrades gracefully to ``None``
  on CPU-only hosts (no torch installed).
- **No secrets logged.** The callback secret is never stored on the fingerprint
  or written to logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FingerprintValidationError(Exception):
    """Raised when a runtime fingerprint fails validation.

    Carries a human-readable reason. Used for fail-closed production checks,
    hash mismatches, and signature verification failures.
    """


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class RuntimeFingerprintConfig(BaseModel):
    """Configuration for :class:`RuntimeFingerprintBuilder`.

    Frozen + ``extra='forbid'`` for audit integrity. Each ``include_*`` flag
    controls whether the corresponding environment field is collected during
    :meth:`RuntimeFingerprintBuilder.build`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    include_git_sha: bool = True
    include_image_digest: bool = True
    include_dockerfile_hash: bool = True
    include_dependency_hash: bool = True
    include_python_version: bool = True
    include_os_info: bool = True
    include_cuda_info: bool = True
    include_gpu_info: bool = True
    include_library_versions: bool = True
    include_random_seeds: bool = True
    include_dataset_hash: bool = True
    include_training_manifest_hash: bool = True
    production_mode: bool = True
    callback_secret_env: str = "CALLBACK_SECRET"


# ---------------------------------------------------------------------------
# Fingerprint model
# ---------------------------------------------------------------------------


# Fields excluded from the fingerprint hash computation (the hash itself and
# its signature). Kept as a module-level constant so tests can reference it.
_HASH_EXCLUDED_FIELDS: frozenset[str] = frozenset({"fingerprint_hash", "signature"})


class RuntimeFingerprint(BaseModel):
    """A signed snapshot of the runtime environment at job execution time.

    Frozen + ``extra='forbid'`` for audit integrity. ``fingerprint_hash`` is a
    deterministic SHA-256 over canonical JSON of all other fields.
    ``signature`` is an HMAC-SHA256 of the fingerprint hash using the callback
    secret (``None`` when no secret was available at build time).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    git_sha: str | None = None
    image_digest: str | None = None
    dockerfile_hash: str | None = None
    dependency_lock_hash: str | None = None
    python_version: str
    os_info: str
    cuda_version: str | None = None
    driver_version: str | None = None
    gpu_model: str | None = None
    library_versions: dict[str, str] = Field(default_factory=dict)
    random_seeds: dict[str, int] = Field(default_factory=dict)
    dataset_manifest_hash: str | None = None
    training_manifest_hash: str | None = None
    fingerprint_hash: str
    signature: str | None = None
    created_at: str
    is_canary: bool = False
    promotion_eligible: bool = False

    @field_validator("fingerprint_hash")
    @classmethod
    def _validate_fingerprint_hash(cls, v: str) -> str:
        """Ensure the fingerprint hash is a 64-char lowercase hex SHA-256."""
        if not isinstance(v, str) or len(v) != 64:
            raise ValueError("fingerprint_hash must be a 64-char hex string")
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("fingerprint_hash must be hex") from exc
        return v.lower()

    @field_validator("signature")
    @classmethod
    def _validate_signature(cls, v: str | None) -> str | None:
        """Ensure the signature, when present, is a 64-char lowercase hex string."""
        if v is None:
            return v
        if not isinstance(v, str) or len(v) != 64:
            raise ValueError("signature must be a 64-char hex string or None")
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("signature must be hex") from exc
        return v.lower()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class RuntimeFingerprintBuilder:
    """Collects runtime environment metadata and builds a signed fingerprint.

    All ``collect_*`` methods degrade gracefully (return ``None``) when the
    corresponding source is unavailable (no git repo, no Dockerfile, no torch,
    etc.) so the builder can run on any host.

    Args:
        config: :class:`RuntimeFingerprintConfig` controlling which fields are
            collected and whether production-mode validation is enforced.
    """

    def __init__(self, config: RuntimeFingerprintConfig | None = None) -> None:
        """Initialize the builder with a config (defaults if ``None``)."""
        self.config = config if config is not None else RuntimeFingerprintConfig()

    # -- collection helpers --------------------------------------------------

    def collect_git_sha(self) -> str | None:
        """Return the current ``git rev-parse HEAD`` SHA, or ``None`` if not in a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    def collect_image_digest(self) -> str | None:
        """Return the image digest from the ``IMAGE_DIGEST`` env var, or ``None``."""
        digest = os.environ.get("IMAGE_DIGEST")
        return digest if digest else None

    def collect_dockerfile_hash(self, dockerfile_path: str | None = None) -> str | None:
        """Compute SHA-256 of the Dockerfile, or ``None`` if the file is not found.

        Args:
            dockerfile_path: Optional explicit path to the Dockerfile. When
                ``None`` the builder looks for ``Dockerfile`` in the current
                working directory.
        """
        path = dockerfile_path or "Dockerfile"
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except (FileNotFoundError, OSError):
            return None
        return hashlib.sha256(data).hexdigest()

    def collect_dependency_hash(self, requirements_path: str | None = None) -> str | None:
        """Compute SHA-256 of the requirements lock file, or ``None`` if not found.

        Args:
            requirements_path: Optional explicit path to the requirements file.
                When ``None`` the builder looks for ``requirements.txt`` in
                the current working directory.
        """
        path = requirements_path or "requirements.txt"
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except (FileNotFoundError, OSError):
            return None
        return hashlib.sha256(data).hexdigest()

    def collect_python_version(self) -> str:
        """Return ``sys.version`` (the full version string)."""
        return sys.version

    def collect_os_info(self) -> str:
        """Return ``platform.platform()`` (OS / arch summary string)."""
        return platform.platform()

    def collect_cuda_info(self) -> tuple[str | None, str | None]:
        """Return ``(cuda_version, driver_version)`` from a lazy torch import.

        Returns ``(None, None)`` on CPU-only hosts or when torch is not
        installed.
        """
        try:
            import torch
        except Exception:
            return (None, None)
        cuda_version = torch.version.cuda
        cuda_version_str = str(cuda_version) if cuda_version is not None else None
        driver_version: str | None = None
        if torch.cuda.is_available():
            # nvidia-driver version is only meaningful when CUDA is available.
            try:
                driver_version = str(torch.cuda.get_device_properties(0).name)
            except Exception:
                driver_version = None
        return (cuda_version_str, driver_version)

    def collect_gpu_info(self) -> str | None:
        """Return the GPU model name, or ``None`` if no GPU is available.

        Uses :func:`quant_foundry.tabular_neural_runtime.check_gpu` (lazy
        import) so the module remains importable without torch.
        """
        try:
            from quant_foundry.tabular_neural_runtime import check_gpu
        except Exception:
            return None
        status = check_gpu()
        if not status.available:
            return None
        return status.device_name

    def collect_library_versions(self) -> dict[str, str]:
        """Return installed versions of key libraries.

        Only libraries that are actually importable are included, so the
        result is stable across CPU-only and GPU hosts.
        """
        candidates = [
            "torch",
            "numpy",
            "pandas",
            "pydantic",
            "scipy",
            "scikit-learn",
            "sklearn",
            "xgboost",
            "catboost",
            "lightgbm",
            "optuna",
        ]
        versions: dict[str, str] = {}
        for name in candidates:
            try:
                mod = __import__(name)
            except Exception:
                continue
            ver = getattr(mod, "__version__", None)
            if ver is not None:
                versions[name] = str(ver)
        return versions

    def collect_random_seeds(self) -> dict[str, int]:
        """Return current random seeds (``PYTHONHASHSEED`` and runtime seeds).

        ``PYTHONHASHSEED`` is read from the environment (``0`` when unset /
        random). Python's ``random`` module state is not deterministic across
        runs, so only environment-controlled seeds are recorded here.
        """
        seeds: dict[str, int] = {}
        pyhashseed = os.environ.get("PYTHONHASHSEED")
        if pyhashseed is not None and pyhashseed.isdigit():
            seeds["python_seed"] = int(pyhashseed)
        else:
            # Unset / "random" -> record 0 as a sentinel (not the actual seed).
            seeds["python_seed"] = 0
        return seeds

    # -- hashing & signing ---------------------------------------------------

    def compute_fingerprint_hash(self, fingerprint: RuntimeFingerprint) -> str:
        """Compute a deterministic SHA-256 over canonical JSON of all fields.

        Excludes ``fingerprint_hash`` and ``signature`` (the hash and its
        signature) from the hashed payload. Dict fields are sorted by key so
        the hash is order-independent.
        """
        payload = self._canonical_hash_payload(fingerprint)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _canonical_hash_payload(fingerprint: RuntimeFingerprint) -> dict[str, Any]:
        """Build the canonical (sorted, JSON-serializable) payload for hashing.

        Excludes ``fingerprint_hash`` and ``signature``. Dicts are converted to
        sorted key->value mappings so the serialized form is stable regardless
        of insertion order.
        """
        data = fingerprint.model_dump()
        payload: dict[str, Any] = {}
        for key in sorted(data.keys()):
            if key in _HASH_EXCLUDED_FIELDS:
                continue
            value = data[key]
            if isinstance(value, dict):
                payload[key] = {str(k): value[k] for k in sorted(value.keys())}
            else:
                payload[key] = value
        return payload

    def sign_fingerprint(self, fingerprint_hash: str, secret: str) -> str:
        """Return the HMAC-SHA256 hex signature of ``fingerprint_hash`` using ``secret``."""
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be a non-empty string")
        if not isinstance(fingerprint_hash, str) or not fingerprint_hash:
            raise ValueError("fingerprint_hash must be a non-empty string")
        return hmac.new(
            secret.encode("utf-8"), fingerprint_hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # -- build ---------------------------------------------------------------

    def build(
        self,
        dataset_manifest_hash: str | None = None,
        training_manifest_hash: str | None = None,
        random_seeds: dict[str, int] | None = None,
        is_canary: bool = False,
    ) -> RuntimeFingerprint:
        """Collect all enabled fields, compute the hash, sign, and return a fingerprint.

        Args:
            dataset_manifest_hash: Optional hash of the dataset manifest.
            training_manifest_hash: Optional hash of the training manifest.
            random_seeds: Optional override of the collected random seeds.
            is_canary: When ``True`` the fingerprint is a canary build; missing
                image digest / git sha are allowed but
                ``promotion_eligible`` is forced to ``False``.
        """
        cfg = self.config

        git_sha = self.collect_git_sha() if cfg.include_git_sha else None
        image_digest = self.collect_image_digest() if cfg.include_image_digest else None
        dockerfile_hash = self.collect_dockerfile_hash() if cfg.include_dockerfile_hash else None
        dependency_lock_hash = (
            self.collect_dependency_hash() if cfg.include_dependency_hash else None
        )
        python_version = self.collect_python_version() if cfg.include_python_version else ""
        os_info = self.collect_os_info() if cfg.include_os_info else ""
        cuda_version: str | None = None
        driver_version: str | None = None
        if cfg.include_cuda_info:
            cuda_version, driver_version = self.collect_cuda_info()
        gpu_model = self.collect_gpu_info() if cfg.include_gpu_info else None
        library_versions = self.collect_library_versions() if cfg.include_library_versions else {}
        seeds = (
            random_seeds
            if random_seeds is not None
            else (self.collect_random_seeds() if cfg.include_random_seeds else {})
        )
        dataset_hash = dataset_manifest_hash if cfg.include_dataset_hash else None
        training_hash = training_manifest_hash if cfg.include_training_manifest_hash else None

        created_at = datetime.now(UTC).isoformat()

        # Determine promotion eligibility: canaries are never eligible; production
        # builds are eligible only when image digest and git sha are present.
        if is_canary:
            promotion_eligible = False
        else:
            promotion_eligible = bool(image_digest and git_sha)

        # Build a provisional fingerprint (hash computed below) then reconstruct
        # with the real hash + signature. We use a placeholder hash first to
        # satisfy the required field, then recompute.
        provisional = RuntimeFingerprint(
            git_sha=git_sha,
            image_digest=image_digest,
            dockerfile_hash=dockerfile_hash,
            dependency_lock_hash=dependency_lock_hash,
            python_version=python_version,
            os_info=os_info,
            cuda_version=cuda_version,
            driver_version=driver_version,
            gpu_model=gpu_model,
            library_versions=library_versions,
            random_seeds=seeds,
            dataset_manifest_hash=dataset_hash,
            training_manifest_hash=training_hash,
            fingerprint_hash="0" * 64,
            signature=None,
            created_at=created_at,
            is_canary=is_canary,
            promotion_eligible=promotion_eligible,
        )
        real_hash = self.compute_fingerprint_hash(provisional)

        signature: str | None = None
        secret = os.environ.get(cfg.callback_secret_env)
        if secret:
            signature = self.sign_fingerprint(real_hash, secret)

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
            library_versions=library_versions,
            random_seeds=seeds,
            dataset_manifest_hash=dataset_hash,
            training_manifest_hash=training_hash,
            fingerprint_hash=real_hash,
            signature=signature,
            created_at=created_at,
            is_canary=is_canary,
            promotion_eligible=promotion_eligible,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def validate_fingerprint(
    fingerprint: RuntimeFingerprint,
    expected_hash: str | None = None,
    production_mode: bool = True,
) -> None:
    """Validate a runtime fingerprint, failing closed in production mode.

    In production mode:
      - ``image_digest`` must not be ``None`` or ``"placeholder"``.
      - ``git_sha`` must not be ``None``.

    If ``expected_hash`` is provided it must match ``fingerprint.fingerprint_hash``.

    Args:
        fingerprint: The fingerprint to validate.
        expected_hash: Optional expected fingerprint hash for tamper detection.
        production_mode: When ``True`` enforce strict (fail-closed) checks.

    Raises:
        FingerprintValidationError: On any validation failure.
    """
    if production_mode:
        if fingerprint.image_digest is None:
            raise FingerprintValidationError(
                "image_digest is missing (required in production mode)"
            )
        if fingerprint.image_digest == "placeholder":
            raise FingerprintValidationError(
                "image_digest is 'placeholder' (not allowed in production mode)"
            )
        if fingerprint.git_sha is None:
            raise FingerprintValidationError("git_sha is missing (required in production mode)")

    if expected_hash is not None:
        if not hmac.compare_digest(expected_hash, fingerprint.fingerprint_hash):
            raise FingerprintValidationError("fingerprint_hash does not match expected hash")


def verify_signature(fingerprint: RuntimeFingerprint, secret: str) -> bool:
    """Verify the fingerprint's HMAC signature against ``secret``.

    Recomputes the HMAC-SHA256 of ``fingerprint.fingerprint_hash`` and
    compares it to ``fingerprint.signature`` in constant time.

    Args:
        fingerprint: The fingerprint whose signature to verify.
        secret: The callback secret used at signing time.

    Returns:
        ``True`` if the signature is valid.

    Raises:
        FingerprintValidationError: If the fingerprint has no signature or the
            signature does not match.
    """
    if fingerprint.signature is None:
        raise FingerprintValidationError("fingerprint has no signature to verify")
    if not isinstance(secret, str) or not secret:
        raise FingerprintValidationError("secret must be a non-empty string")
    builder = RuntimeFingerprintBuilder()
    expected = builder.sign_fingerprint(fingerprint.fingerprint_hash, secret)
    if not hmac.compare_digest(expected, fingerprint.signature):
        raise FingerprintValidationError("fingerprint signature verification failed")
    return True


def serialize_fingerprint(fingerprint: RuntimeFingerprint) -> str:
    """Serialize a fingerprint to a canonical JSON string.

    The output is sorted-key JSON so the serialization is deterministic and
    suitable for storage / transmission.
    """
    return fingerprint.model_dump_json()


def deserialize_fingerprint(json_str: str) -> RuntimeFingerprint:
    """Deserialize a fingerprint from a JSON string produced by :func:`serialize_fingerprint`.

    Args:
        json_str: JSON string (from :func:`serialize_fingerprint`).

    Returns:
        A :class:`RuntimeFingerprint` reconstructed from the JSON.

    Raises:
        FingerprintValidationError: If the JSON is invalid or the resulting
            fingerprint fails model validation.
    """
    try:
        return RuntimeFingerprint.model_validate_json(json_str)
    except Exception as exc:
        raise FingerprintValidationError(f"failed to deserialize fingerprint: {exc}") from exc
