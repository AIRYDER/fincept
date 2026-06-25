"""
fincept_core.storage — provider-agnostic storage abstraction (local + S3-compatible).

A single storage module that picks the right backend based on env vars and works
on Railway (Object Storage is S3-compatible), AWS (S3), Cloudflare R2, MinIO, or
local filesystem. The goal: let ``quant_foundry`` (trainer / inference engine /
artifact import) talk to object storage without any AWS/boto3 coupling baked
into the call sites.

Design invariants (mirroring ``quant_foundry.artifacts``):
- **Injectable, not hardcoded.** Callers receive a ``StorageBackend`` instance
  (or use the ``get_storage_backend()`` factory). No AWS/boto3 imports leak into
  consumers.
- **Lazy boto3 import.** This module is importable without ``boto3`` installed.
  ``boto3`` is imported only inside ``S3StorageBackend`` methods, so local-only
  deployments never need it.
- **Path traversal protection on ALL backends.** ``..`` segments are rejected
  for both local paths and S3 keys (defense-in-depth).
- **Backward compatible.** ``file://`` URIs and bare paths still work with zero
  config changes. The default backend is ``local``.
- **No secrets hardcoded.** All credentials come from env vars or constructor
  params — never written to disk or logged.
- **Canonical URIs.** ``write_bytes`` returns the canonical URI
  (``file://<abs-path>`` or ``s3://bucket/key``) so callers can persist a
  stable reference.
"""

from __future__ import annotations

import os
import pathlib
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote, unquote, urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Errors
# ===========================================================================


class StorageError(Exception):
    """Base error for storage backend failures (URI validation, IO, config)."""


class UnsupportedUriError(StorageError):
    """Raised when a URI scheme is not supported by the active backend."""


class PathTraversalError(StorageError):
    """Raised when a URI contains a ``..`` traversal segment."""


class StorageConfigError(StorageError):
    """Raised when the storage configuration is incomplete or invalid."""


# ---------------------------------------------------------------------------
# URI helpers
# ===========================================================================


def _reject_traversal(segments: tuple[str, ...], uri: str) -> None:
    """Reject any ``..`` segment in ``segments`` (path traversal defense)."""
    if ".." in segments:
        raise PathTraversalError(
            f"uri contains '..' traversal segment — path escape rejected: {uri!r}"
        )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse an ``s3://bucket/key`` URI into ``(bucket, key)``.

    Raises ``UnsupportedUriError`` if the scheme is not ``s3``, the bucket is
    empty, or the key is empty. Raises ``PathTraversalError`` if the key
    contains a ``..`` segment.
    """
    parsed = urlparse(uri)
    if (parsed.scheme or "").lower() != "s3":
        raise UnsupportedUriError(
            f"expected s3:// uri, got scheme {parsed.scheme!r}: {uri!r}"
        )
    bucket = parsed.netloc or ""
    if not bucket:
        raise UnsupportedUriError(f"s3 uri has empty bucket: {uri!r}")
    raw_key = unquote(parsed.path or "").lstrip("/")
    if not raw_key:
        raise UnsupportedUriError(f"s3 uri has empty key: {uri!r}")
    _reject_traversal(pathlib.PurePosixPath(raw_key).parts, uri)
    return bucket, raw_key


def _strip_windows_drive_leading_slash(path: str) -> str:
    """On Windows, ``file:///C:/...`` yields ``/C:/...`` — strip the leading
    slash before a drive letter so ``pathlib.Path`` resolves it correctly."""
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        return path[1:]
    return path


def parse_file_uri(uri: str) -> pathlib.Path:
    """Parse a ``file://`` URI or bare path into a ``pathlib.Path``.

    Raises ``PathTraversalError`` if the path contains a ``..`` segment.
    Windows drive-letter bare paths (e.g. ``C:\\Users\\...``) are handled
    despite ``urlparse`` treating the drive letter as a scheme.
    """
    # Windows drive-letter bare paths: urlparse treats "C:\..." as scheme="c".
    # Detect a single-letter scheme followed by a path separator and treat
    # the whole string as a bare path.
    if (
        len(uri) >= 3
        and uri[1] == ":"
        and uri[0].isalpha()
        and (uri[2] in ("\\", "/"))
    ):
        raw_path = uri
        raw_path = _strip_windows_drive_leading_slash(raw_path)
        _reject_traversal(pathlib.PurePath(raw_path).parts, uri)
        return pathlib.Path(raw_path)
    parsed = urlparse(uri)
    if (parsed.scheme or "").lower() == "file":
        raw_path = unquote(parsed.path or "")
    elif parsed.scheme == "":
        raw_path = uri
    else:
        raise UnsupportedUriError(
            f"expected file:// uri or bare path, got scheme {parsed.scheme!r}: {uri!r}"
        )
    if not raw_path:
        raise UnsupportedUriError(f"file uri has empty path: {uri!r}")
    raw_path = _strip_windows_drive_leading_slash(raw_path)
    _reject_traversal(pathlib.PurePath(raw_path).parts, uri)
    return pathlib.Path(raw_path)


# ---------------------------------------------------------------------------
# Backend protocol / ABC
# ===========================================================================


class StorageBackend(ABC):
    """Abstract storage backend.

    All backends implement the same small surface so callers (trainer, inference
    engine, artifact import) are fully provider-agnostic. Convenience text
    helpers are provided here so concrete backends only implement byte IO.
    """

    @abstractmethod
    def read_bytes(self, uri: str) -> bytes:
        """Read bytes from ``uri``. Raises ``StorageError`` on failure."""

    @abstractmethod
    def write_bytes(self, uri: str, data: bytes) -> str:
        """Write ``data`` to ``uri`` and return the canonical URI."""

    @abstractmethod
    def exists(self, uri: str) -> bool:
        """Return True if ``uri`` exists in this backend."""

    def read_text(self, uri: str, encoding: str = "utf-8") -> str:
        """Read text from ``uri`` (convenience, uses ``read_bytes``)."""
        return self.read_bytes(uri).decode(encoding)

    def write_text(self, uri: str, text: str, encoding: str = "utf-8") -> str:
        """Write text to ``uri`` (convenience, uses ``write_bytes``)."""
        return self.write_bytes(uri, text.encode(encoding))

    def download_to_temp(self, uri: str, suffix: str | None = None) -> str:
        """Download ``uri`` to a local temp file and return the path.

        Used by the trainer / inference engine which need a local filesystem
        path to feed into LightGBM / onnxruntime. The temp file is NOT cleaned
        up automatically (callers manage the lifecycle via ``tempfile``).
        """
        import tempfile

        data = self.read_bytes(uri)
        if suffix is None:
            parsed = urlparse(uri)
            _, ext = os.path.splitext(unquote(parsed.path or ""))
            suffix = ext if ext else ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return tmp_path


# ---------------------------------------------------------------------------
# Local filesystem backend
# ===========================================================================


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend.

    Handles ``file://`` URIs and bare paths. Uses ``pathlib.Path`` for all
    operations. Path traversal (``..`` segments) is rejected. Parent
    directories are created on write.
    """

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        if base_dir is not None:
            self.base_dir: pathlib.Path | None = pathlib.Path(base_dir)
        else:
            self.base_dir = None

    def _resolve(self, uri: str) -> pathlib.Path:
        """Resolve a ``file://`` URI or bare path to a concrete ``Path``.

        Relative bare paths are resolved against ``base_dir`` when set.
        """
        path = parse_file_uri(uri)
        if not path.is_absolute() and self.base_dir is not None:
            path = (self.base_dir / path).resolve()
        return path

    @staticmethod
    def _canonical(path: pathlib.Path) -> str:
        """Return the canonical ``file://`` URI for ``path``."""
        abs_path = path.resolve() if not path.is_absolute() else path
        # Use as_posix for cross-platform stable URIs.
        return "file://" + abs_path.as_posix()

    def read_bytes(self, uri: str) -> bytes:
        path = self._resolve(uri)
        if not path.is_file():
            raise StorageError(f"local file not found: {uri!r} -> {path}")
        return path.read_bytes()

    def write_bytes(self, uri: str, data: bytes) -> str:
        path = self._resolve(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._canonical(path)

    def exists(self, uri: str) -> bool:
        path = self._resolve(uri)
        return path.exists()

    def read_text(self, uri: str, encoding: str = "utf-8") -> str:
        path = self._resolve(uri)
        if not path.is_file():
            raise StorageError(f"local file not found: {uri!r} -> {path}")
        return path.read_text(encoding=encoding)

    def write_text(self, uri: str, text: str, encoding: str = "utf-8") -> str:
        path = self._resolve(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return self._canonical(path)


# ---------------------------------------------------------------------------
# S3-compatible backend (AWS S3, Railway Object Storage, R2, MinIO)
# ===========================================================================


class S3StorageBackend(StorageBackend):
    """S3-compatible object storage backend.

    Handles ``s3://`` URIs. Uses ``boto3`` (LAZY import — module is importable
    without boto3). Configurable endpoint URL for Railway Object Storage,
    Cloudflare R2, MinIO, etc. Path traversal (``..`` segments) is rejected
    for S3 keys. Returns canonical ``s3://bucket/key`` URIs.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        # Allow injecting a pre-built client (used by tests + artifact module).
        self._client = client

    def _get_client(self) -> Any:
        """Lazily build (or return cached) the boto3 S3 client."""
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via tests
            raise StorageError(
                "boto3 is required for S3 storage backend but is not installed"
            ) from exc
        kwargs: dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.access_key is not None and self.secret_key is not None:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        self._client = boto3.client("s3", **kwargs)
        return self._client

    def _resolve_bucket(self, uri: str) -> tuple[str, str]:
        """Parse ``uri`` into ``(bucket, key)``, falling back to the default
        bucket when the URI has no bucket component (bare key)."""
        parsed = urlparse(uri)
        if (parsed.scheme or "").lower() == "s3":
            return parse_s3_uri(uri)
        # Bare key — use default bucket.
        if self.bucket is None:
            raise StorageConfigError(
                f"no bucket in uri and no default bucket configured: {uri!r}"
            )
        key = uri.lstrip("/")
        _reject_traversal(pathlib.PurePosixPath(key).parts, uri)
        return self.bucket, key

    @staticmethod
    def _canonical(bucket: str, key: str) -> str:
        return f"s3://{bucket}/{key}"

    def read_bytes(self, uri: str) -> bytes:
        bucket, key = self._resolve_bucket(uri)
        client = self._get_client()
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"s3 read failed for {uri!r}: {exc}") from exc
        body = resp["Body"].read() if hasattr(resp, "__getitem__") else resp.read()
        return body

    def write_bytes(self, uri: str, data: bytes) -> str:
        bucket, key = self._resolve_bucket(uri)
        client = self._get_client()
        try:
            client.put_object(Bucket=bucket, Key=key, Body=data)
        except Exception as exc:
            raise StorageError(f"s3 write failed for {uri!r}: {exc}") from exc
        return self._canonical(bucket, key)

    def exists(self, uri: str) -> bool:
        bucket, key = self._resolve_bucket(uri)
        client = self._get_client()
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Config (pydantic-settings)
# ===========================================================================


class StorageConfig(BaseSettings):
    """Storage configuration read from env vars.

    Env prefix is ``FINCEPT_STORAGE_``. Defaults to the ``local`` backend so
    local development needs no S3 config. No secrets are hardcoded — all
    credentials come from env vars.
    """

    model_config = SettingsConfigDict(
        env_prefix="FINCEPT_STORAGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BACKEND: str = Field(default="local")
    LOCAL_BASE_DIR: str = Field(default="data")
    S3_ENDPOINT: str | None = Field(default=None)
    S3_REGION: str = Field(default="us-east-1")
    S3_ACCESS_KEY: str | None = Field(default=None)
    S3_SECRET_KEY: str | None = Field(default=None)
    S3_BUCKET: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Factory (cached singleton)
# ===========================================================================


_STORAGE_BACKEND: StorageBackend | None = None


def get_storage_backend(config: StorageConfig | None = None) -> StorageBackend:
    """Return the appropriate ``StorageBackend`` for the current config.

    Reads ``StorageConfig`` from env vars (or accepts an explicit config) and
    returns a ``LocalStorageBackend`` or ``S3StorageBackend``. The result is
    cached (singleton) so repeated calls don't rebuild the boto3 client.
    """
    global _STORAGE_BACKEND
    if _STORAGE_BACKEND is not None and config is None:
        return _STORAGE_BACKEND
    cfg = config if config is not None else StorageConfig()
    backend = cfg.BACKEND.strip().lower()
    if backend == "local":
        instance: StorageBackend = LocalStorageBackend(base_dir=cfg.LOCAL_BASE_DIR)
    elif backend == "s3":
        instance = S3StorageBackend(
            endpoint_url=cfg.S3_ENDPOINT,
            region=cfg.S3_REGION,
            access_key=cfg.S3_ACCESS_KEY,
            secret_key=cfg.S3_SECRET_KEY,
            bucket=cfg.S3_BUCKET,
        )
    else:
        raise StorageConfigError(
            f"unknown FINCEPT_STORAGE_BACKEND {backend!r} (expected 'local' or 's3')"
        )
    if config is None:
        _STORAGE_BACKEND = instance
    return instance


def clear_storage_backend_cache() -> None:
    """Clear the cached singleton (used by tests)."""
    global _STORAGE_BACKEND
    _STORAGE_BACKEND = None


# ---------------------------------------------------------------------------
# URI resolution helper
# ===========================================================================


def resolve_uri(
    uri: str,
    backend: StorageBackend | None = None,
) -> str:
    """Resolve a relative/bare path to an absolute URI.

    - For local backends: bare paths are converted to ``file://`` URIs
      (resolved against the backend's ``base_dir`` when relative).
    - For S3 backends: the URI is validated (``s3://`` scheme required).
    - If ``backend`` is None, the factory singleton is used.
    """
    if backend is None:
        backend = get_storage_backend()
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if isinstance(backend, LocalStorageBackend):
        if scheme == "file":
            return uri
        if scheme == "":
            path = pathlib.Path(uri)
            if not path.is_absolute() and backend.base_dir is not None:
                path = (backend.base_dir / path).resolve()
            return "file://" + path.as_posix()
        if scheme == "s3":
            raise UnsupportedUriError(
                "s3:// uri passed to LocalStorageBackend.resolve_uri"
            )
        raise UnsupportedUriError(f"unsupported uri scheme {scheme!r}: {uri!r}")
    if isinstance(backend, S3StorageBackend):
        if scheme == "s3":
            bucket, key = parse_s3_uri(uri)
            return backend._canonical(bucket, key)
        raise UnsupportedUriError(
            f"S3StorageBackend requires s3:// uri, got {scheme!r}: {uri!r}"
        )
    if scheme == "file" or scheme == "":
        return uri
    if scheme == "s3":
        return uri
    raise UnsupportedUriError(f"unsupported uri scheme {scheme!r}: {uri!r}")


__all__ = [
    "LocalStorageBackend",
    "PathTraversalError",
    "S3StorageBackend",
    "StorageBackend",
    "StorageConfig",
    "StorageConfigError",
    "StorageError",
    "UnsupportedUriError",
    "clear_storage_backend_cache",
    "get_storage_backend",
    "parse_file_uri",
    "parse_s3_uri",
    "resolve_uri",
]
