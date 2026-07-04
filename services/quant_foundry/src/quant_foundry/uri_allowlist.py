"""URI allowlist for dataset and artifact URIs (T-2.3).

Restricts the schemes, hosts, and filesystem roots that the production
quant-foundry worker is allowed to fetch datasets and artifacts from.

The goal is to make the production worker **fail-closed**: any URI that
isn't explicitly approved by an :class:`URIAllowlistConfig` is rejected
with a structured :class:`URIValidationResult`. This prevents the worker
from being abused as a generic network fetcher (SSRF) or as a way to
read arbitrary files off the host (``file:///etc/passwd``).

Supported schemes (see :class:`URIScheme`):

- ``file``           - local filesystem, restricted to approved volume roots
- ``http``/``https`` - object stores / public mirrors, restricted to
                       approved hosts; localhost and private IPs are
                       rejected in production mode
- ``s3``/``gs``/``azblob`` - object-store schemes; the host/bucket must
                              be in ``allowed_object_hosts``
- ``runpod_volume``  - RunPod-mounted volume, restricted to
                       ``allowed_volume_roots``

All public functions are pure and side-effect free; they never touch the
network or the filesystem (path resolution is lexical via ``pathlib``).
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sensitive absolute paths that are always rejected for ``file://`` URIs
#: regardless of the configured volume roots. These are common system
#: files/directories that a dataset URI should never point at.
_SYSTEM_PATHS: Final[tuple[str, ...]] = (
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/var/log",
)

#: Regex that matches a URI scheme prefix. Unlike :func:`urllib.parse.urlsplit`
#: this allows underscores (needed for the ``runpod_volume`` scheme).
_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+._-]*):")


#: Regex that matches ``user:password@`` credentials embedded in a URI
#: netloc. Used by :func:`redact_uri`.
_CREDENTIALS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<scheme>[^:/?#]+)://(?P<user>[^:/@]+):(?P<password>[^@]*)@",
)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class URIScheme(StrEnum):
    """Approved URI schemes for dataset/artifact references.

    Members are lower-case strings so they compare directly against the
    ``scheme`` component returned by :func:`urllib.parse.urlsplit`.
    """

    FILE = "file"
    HTTP = "http"
    HTTPS = "https"
    S3 = "s3"
    GS = "gs"
    AZBLOB = "azblob"
    RUNPOD_VOLUME = "runpod_volume"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class URIAllowlistConfig(BaseModel):
    """Configuration for :func:`validate_uri`.

    The config is **frozen** and **forbids unknown fields** so a typo in
    a deployment manifest cannot silently relax a security control.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_schemes: list[URIScheme] = Field(
        default_factory=lambda: [
            URIScheme.FILE,
            URIScheme.HTTPS,
            URIScheme.S3,
            URIScheme.GS,
            URIScheme.AZBLOB,
            URIScheme.RUNPOD_VOLUME,
        ],
        description="URI schemes the worker is permitted to fetch.",
    )
    allowed_volume_roots: list[str] = Field(
        default_factory=lambda: ["/workspace/data", "/workspace/artifacts"],
        description="Approved RunPod/local volume mount paths for file:// and runpod_volume:// URIs.",
    )
    allowed_object_hosts: list[str] = Field(
        default_factory=lambda: [
            "s3.amazonaws.com",
            "storage.googleapis.com",
            "blob.core.windows.net",
        ],
        description="Approved object-store hosts for http(s)://, s3://, gs://, azblob:// URIs.",
    )
    allow_localhost: bool = Field(
        default=False,
        description="If True, localhost references are permitted (dev/test only).",
    )
    allow_arbitrary_http: bool = Field(
        default=False,
        description="If True, arbitrary public HTTP hosts are permitted (never in production).",
    )
    production_mode: bool = Field(
        default=True,
        description="If True, the worker enforces production-grade restrictions.",
    )

    @field_validator("allowed_schemes")
    @classmethod
    def _schemes_non_empty(cls, value: list[URIScheme]) -> list[URIScheme]:
        """Reject an empty scheme list - the worker must allow at least one scheme."""
        if not value:
            raise ValueError("allowed_schemes must not be empty")
        return list(value)

    @field_validator("allowed_volume_roots")
    @classmethod
    def _volume_roots_non_empty(cls, value: list[str]) -> list[str]:
        """Reject an empty volume-root list - file:// URIs need at least one root."""
        if not value:
            raise ValueError("allowed_volume_roots must not be empty")
        return list(value)


class URIValidationResult(BaseModel):
    """Outcome of validating a single URI.

    The result is **frozen** so callers cannot mutate a ``valid`` result
    into an ``invalid`` one (or vice versa) after the fact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(description="The URI that was validated.")
    scheme: URIScheme = Field(description="The parsed scheme.")
    is_valid: bool = Field(description="True if the URI passed all checks.")
    rejection_reason: str | None = Field(
        default=None,
        description="Stable machine-readable code explaining why the URI was rejected.",
    )
    resolved_path: str | None = Field(
        default=None,
        description="For file:// URIs, the resolved absolute path on disk.",
    )
    host: str | None = Field(
        default=None,
        description="For network/object URIs, the host or bucket component.",
    )


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


def is_localhost(host: str) -> bool:
    """Return True if *host* refers to the local machine.

    Recognises the literal names ``localhost`` and the loopback addresses
    ``127.0.0.1``, ``::1`` and ``0.0.0.0`` (case-insensitive, whitespace
    stripped). IPv6 brackets (``[::1]``) are tolerated.
    """
    if not host:
        return False
    cleaned = host.strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return cleaned in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_private_ip(host: str) -> bool:
    """Return True if *host* is a private or link-local IPv4 address.

    Covers the RFC 1918 ranges (``10.0.0.0/8``, ``172.16.0.0/12``,
    ``192.168.0.0/16``) and the link-local range ``169.254.0.0/16``.
    Non-IPv4 hosts (DNS names, IPv6) return ``False``.
    """
    if not host:
        return False
    cleaned = host.strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    parts = cleaned.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    a, b, _c, _d = octets
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    return False


def has_path_traversal(path: str) -> bool:
    """Return True if *path* contains a ``..`` path segment.

    A leading ``..`` or a ``..`` between separators counts as traversal;
    a literal ``..`` inside a filename (``foo..bar``) does not.
    """
    if not path:
        return False
    # Normalise backslashes so Windows-style paths are also caught.
    normalised = path.replace("\\", "/")
    segments = normalised.split("/")
    return ".." in segments


def _lexical_resolve_posix(path: str) -> str:
    """Lexically resolve ``.`` and ``..`` segments in a POSIX path.

    Unlike :meth:`pathlib.Path.resolve`, this performs no filesystem
    access and works on :class:`pathlib.PurePosixPath`. A leading ``..``
    that would escape above ``/`` is collapsed away (the result is
    always anchored at ``/``).
    """
    if not path:
        return "/"
    is_absolute = path.startswith("/")
    parts: list[str] = []
    for segment in path.replace("\\", "/").split("/"):
        if segment == "" or segment == ".":
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    resolved = "/" + "/".join(parts)
    return resolved if is_absolute else resolved.lstrip("/")


def is_under_root(path: str, roots: list[str]) -> bool:
    """Return True if *path* is lexically contained under one of *roots*.

    Comparison is performed with :class:`pathlib.PurePosixPath` so the
    check is platform-independent and immune to ``..`` tricks (the path
    is lexically resolved before comparison). A root equals to the path
    is accepted (the root itself is "under" itself).
    """
    if not path or not roots:
        return False
    try:
        target = PurePosixPath(_lexical_resolve_posix(path))
    except (ValueError, OSError):
        return False
    for root in roots:
        try:
            root_path = PurePosixPath(_lexical_resolve_posix(root))
        except (ValueError, OSError):
            continue
        if target == root_path:
            return True
        # ``relative_to`` raises ValueError when *target* is not under
        # *root_path*; that's exactly the signal we want.
        try:
            target.relative_to(root_path)
            return True
        except ValueError:
            continue
    return False


def redact_uri(uri: str) -> str:
    """Return *uri* with any embedded ``user:password@`` credentials masked.

    For example ``s3://AKIA:SECRET@bucket/key`` becomes
    ``s3://***:***@bucket/key``. URIs without credentials are returned
    unchanged.
    """
    if not uri:
        return uri
    match = _CREDENTIALS_RE.match(uri)
    if not match:
        return uri
    scheme = match.group("scheme")
    rest = uri[match.end() :]
    return f"{scheme}://***:***@{rest}"


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def _reject(
    uri: str,
    scheme: URIScheme,
    reason: str,
    *,
    resolved_path: str | None = None,
    host: str | None = None,
) -> URIValidationResult:
    """Build a rejected :class:`URIValidationResult`."""
    return URIValidationResult(
        uri=uri,
        scheme=scheme,
        is_valid=False,
        rejection_reason=reason,
        resolved_path=resolved_path,
        host=host,
    )


def _accept(
    uri: str,
    scheme: URIScheme,
    *,
    resolved_path: str | None = None,
    host: str | None = None,
) -> URIValidationResult:
    """Build an accepted :class:`URIValidationResult`."""
    return URIValidationResult(
        uri=uri,
        scheme=scheme,
        is_valid=True,
        rejection_reason=None,
        resolved_path=resolved_path,
        host=host,
    )


def _validate_file_uri(
    uri: str,
    config: URIAllowlistConfig,
) -> URIValidationResult:
    """Validate a ``file://`` URI against the configured volume roots."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return _reject(uri, URIScheme.FILE, "malformed_uri")
    raw_path = parsed.path
    if not raw_path:
        return _reject(uri, URIScheme.FILE, "empty_path")

    if has_path_traversal(raw_path):
        return _reject(uri, URIScheme.FILE, "path_traversal")

    # Lexical resolution keeps the check platform-independent and free
    # of filesystem access (the worker may not have the volume mounted
    # at validation time).
    resolved_posix = _lexical_resolve_posix(raw_path)

    # Reject well-known system paths regardless of the configured roots.
    for system_path in _SYSTEM_PATHS:
        if resolved_posix == system_path or resolved_posix.startswith(system_path + "/"):
            return _reject(
                uri,
                URIScheme.FILE,
                "system_path",
                resolved_path=resolved_posix,
            )

    if not is_under_root(resolved_posix, config.allowed_volume_roots):
        return _reject(
            uri,
            URIScheme.FILE,
            "outside_volume_roots",
            resolved_path=resolved_posix,
        )

    return _accept(uri, URIScheme.FILE, resolved_path=resolved_posix)


def _validate_http_uri(
    uri: str,
    scheme: URIScheme,
    config: URIAllowlistConfig,
) -> URIValidationResult:
    """Validate an ``http://`` or ``https://`` URI."""
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return _reject(uri, scheme, "malformed_uri")
    host = (parsed.hostname or "").lower()
    if not host:
        return _reject(uri, scheme, "missing_host", host=None)

    if config.production_mode and not config.allow_localhost:
        if is_localhost(host):
            return _reject(uri, scheme, "localhost_forbidden", host=host)
        if is_private_ip(host):
            return _reject(uri, scheme, "private_ip_forbidden", host=host)

    if not config.allow_arbitrary_http:
        if host not in {h.lower() for h in config.allowed_object_hosts}:
            return _reject(uri, scheme, "host_not_allowed", host=host)

    return _accept(uri, scheme, host=host)


def _validate_object_uri(
    uri: str,
    scheme: URIScheme,
    config: URIAllowlistConfig,
) -> URIValidationResult:
    """Validate an ``s3://``, ``gs://`` or ``azblob://`` URI.

    For these schemes the *netloc* is the bucket/account name; we treat
    it as the host and require it (or a host component of the URI) to
    appear in ``allowed_object_hosts``.
    """
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return _reject(uri, scheme, "malformed_uri")
    # ``s3://bucket/key`` -> netloc == "bucket"
    # ``s3://bucket.s3.amazonaws.com/key`` -> netloc == "bucket.s3.amazonaws.com"
    netloc = (parsed.netloc or "").lower()
    host = (parsed.hostname or netloc or "").lower()
    # Strip any ``user:pass@`` prefix from the netloc for the host check.
    if "@" in netloc:
        host = netloc.split("@", 1)[1].split(":", 1)[0]

    if not host:
        return _reject(uri, scheme, "missing_bucket", host=None)

    allowed = {h.lower() for h in config.allowed_object_hosts}
    # Accept either an exact match or a suffix match where the host ends
    # with ``.<allowed>`` (e.g. ``my-bucket.s3.amazonaws.com``).
    matched = host in allowed or any(host.endswith("." + a) for a in allowed)
    if not matched:
        return _reject(uri, scheme, "bucket_not_allowed", host=host)

    return _accept(uri, scheme, host=host)


def _validate_runpod_volume_uri(
    uri: str,
    config: URIAllowlistConfig,
) -> URIValidationResult:
    """Validate a ``runpod_volume://`` URI against the configured roots.

    The ``runpod_volume`` scheme contains an underscore, which
    :func:`urllib.parse.urlsplit` does not recognise as a legal scheme
    character, so the path is extracted manually by stripping the
    ``runpod_volume://`` prefix.
    """
    prefix = "runpod_volume://"
    if not uri.lower().startswith(prefix):
        return _reject(uri, URIScheme.RUNPOD_VOLUME, "malformed_uri")
    raw_path = uri[len(prefix) :]
    # ``runpod_volume:///workspace/data/x`` -> after stripping the
    # scheme prefix the remainder is ``/workspace/data/x`` (the extra
    # leading slash from ``///`` is part of an empty authority).
    if raw_path.startswith("/"):
        # Drop the authority separator slash if present.
        raw_path = raw_path.lstrip("/")
        raw_path = "/" + raw_path
    if not raw_path or raw_path == "/":
        return _reject(uri, URIScheme.RUNPOD_VOLUME, "empty_path")

    if has_path_traversal(raw_path):
        return _reject(uri, URIScheme.RUNPOD_VOLUME, "path_traversal")

    resolved_posix = _lexical_resolve_posix(raw_path)
    if not is_under_root(resolved_posix, config.allowed_volume_roots):
        return _reject(
            uri,
            URIScheme.RUNPOD_VOLUME,
            "outside_volume_roots",
            resolved_path=resolved_posix,
        )

    return _accept(
        uri,
        URIScheme.RUNPOD_VOLUME,
        resolved_path=resolved_posix,
    )


def validate_uri(uri: str, config: URIAllowlistConfig) -> URIValidationResult:
    """Validate a single *uri* against *config*.

    Returns a :class:`URIValidationResult`. Never raises for a malformed
    or rejected URI - the outcome is communicated via
    ``is_valid``/``rejection_reason``. An empty URI is rejected with
    reason ``empty_uri``.
    """
    if not uri or not uri.strip():
        return URIValidationResult(
            uri=uri,
            scheme=URIScheme.FILE,
            is_valid=False,
            rejection_reason="empty_uri",
        )

    try:
        parsed = urlsplit(uri)
    except ValueError:
        # ``urlsplit`` raises on malformed IPv6 hosts etc.
        return URIValidationResult(
            uri=uri,
            scheme=URIScheme.FILE,
            is_valid=False,
            rejection_reason="malformed_uri",
        )
    # ``urlsplit`` does not recognise schemes containing underscores
    # (e.g. ``runpod_volume``), so extract the scheme manually and fall
    # back to the parsed value only when the manual match misses.
    scheme_match = _SCHEME_RE.match(uri)
    raw_scheme = (
        scheme_match.group("scheme").lower() if scheme_match else (parsed.scheme or "").lower()
    )
    if not raw_scheme:
        return URIValidationResult(
            uri=uri,
            scheme=URIScheme.FILE,
            is_valid=False,
            rejection_reason="missing_scheme",
        )

    try:
        scheme = URIScheme(raw_scheme)
    except ValueError:
        return URIValidationResult(
            uri=uri,
            scheme=URIScheme.FILE,
            is_valid=False,
            rejection_reason="unknown_scheme",
        )

    if scheme not in config.allowed_schemes:
        return _reject(uri, scheme, "scheme_not_allowed")

    if scheme == URIScheme.FILE:
        return _validate_file_uri(uri, config)
    if scheme in (URIScheme.HTTP, URIScheme.HTTPS):
        return _validate_http_uri(uri, scheme, config)
    if scheme in (URIScheme.S3, URIScheme.GS, URIScheme.AZBLOB):
        return _validate_object_uri(uri, scheme, config)
    if scheme == URIScheme.RUNPOD_VOLUME:
        return _validate_runpod_volume_uri(uri, config)

    # Defensive: every scheme branch is handled above.
    return _reject(uri, scheme, "unhandled_scheme")


def validate_uris(
    uris: list[str],
    config: URIAllowlistConfig,
) -> list[URIValidationResult]:
    """Validate a batch of *uris* against *config*.

    Each URI is validated independently; a rejection of one does not
    short-circuit the others. The returned list is in the same order as
    the input.
    """
    return [validate_uri(uri, config) for uri in uris]


__all__ = [
    "URIAllowlistConfig",
    "URIScheme",
    "URIValidationResult",
    "has_path_traversal",
    "is_localhost",
    "is_private_ip",
    "is_under_root",
    "redact_uri",
    "validate_uri",
    "validate_uris",
]
