#!/usr/bin/env python3
"""Standalone artifact manifest verifier (Tier 0.2 / durable-artifact skill).

Loads an artifact manifest JSON, fetches the artifact by URI, re-hashes
with SHA-256, compares to the manifest's declared sha256, and verifies
the HMAC write receipt against ``QUANT_FOUNDRY_CALLBACK_SECRET``.

This is the trusted-side verifier: it runs independently of the worker
and the callback ingestion service. It proves that the artifact at the
declared URI is byte-for-byte identical to what the worker signed, and
that the write receipt is authentic (not forged).

Usage:
    # Verify a manifest file on disk (file:// artifact URI):
    QUANT_FOUNDRY_CALLBACK_SECRET=secret \\
        python scripts/verify_artifact_manifest.py path/to/artifact_manifest.json

    # Verify a manifest from stdin:
    cat artifact_manifest.json | \\
        QUANT_FOUNDRY_CALLBACK_SECRET=secret \\
        python scripts/verify_artifact_manifest.py -

Exit codes:
    0 — manifest verified (sha256 matches, write receipt authentic)
    1 — verification failed (sha256 mismatch or receipt invalid)
    2 — operational error (file not found, fetch error, bad JSON)

Manifest format (written by VolumeArtifactWriter as artifact_manifest.json):
    {
        "artifact_uri": "file:///runpod-volume/artifacts/train1/model.pkl",
        "artifact_sha256": "<64 hex chars>",
        "artifact_size_bytes": 12345,
        "artifact_format": "pickle",
        "write_receipt": "<64 hex chars HMAC-SHA256>",
        ...other fields ignored...
    }
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, url2pathname, urlopen

REQUIRED_FIELDS = (
    "artifact_uri",
    "artifact_sha256",
    "artifact_size_bytes",
    "artifact_format",
    "write_receipt",
)


def _get_secret() -> str:
    """Read the callback secret from the environment (fail-closed)."""
    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not secret:
        print(
            "ERROR: QUANT_FOUNDRY_CALLBACK_SECRET is not set — cannot "
            "verify write receipt (fail closed).",
            file=sys.stderr,
        )
        sys.exit(2)
    return secret


def _load_manifest(path: str) -> dict[str, Any]:
    """Load a manifest JSON from a file path or stdin ('-')."""
    if path == "-":
        raw = sys.stdin.read()
    else:
        p = Path(path)
        if not p.exists():
            print(f"ERROR: manifest file not found: {path}", file=sys.stderr)
            sys.exit(2)
        raw = p.read_text(encoding="utf-8")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in manifest: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(manifest, dict):
        print("ERROR: manifest must be a JSON object", file=sys.stderr)
        sys.exit(2)
    # Check required fields.
    missing = [f for f in REQUIRED_FIELDS if f not in manifest]
    if missing:
        print(
            f"ERROR: manifest missing required fields: {missing}",
            file=sys.stderr,
        )
        sys.exit(2)
    return manifest


def _fetch_artifact(uri: str) -> bytes:
    """Fetch artifact bytes by URI (file:// or https://)."""
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme == "file":
        # file:// URI → local path (url2pathname handles Windows drive
        # letters correctly, e.g. file:///C:/foo → C:\foo).
        local_path = url2pathname(parsed.path)
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"artifact file not found: {path}")
        return path.read_bytes()
    elif scheme == "https":
        # https:// URI → HTTP GET (presigned URL).
        req = Request(uri, method="GET")
        with urlopen(req) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise OSError(f"HTTP {status} fetching artifact from {uri}")
            return resp.read()
    else:
        raise ValueError(
            f"unsupported artifact URI scheme {scheme!r} (supported: file, https): {{uri}}"
        )


def _recompute_receipt(
    *,
    artifact_uri: str,
    artifact_sha256: str,
    artifact_size_bytes: int,
    artifact_format: str,
    secret: str,
) -> str:
    """Recompute the HMAC-SHA256 write receipt (canonical form)."""
    canonical = "|".join(
        [
            artifact_uri,
            artifact_sha256,
            str(artifact_size_bytes),
            artifact_format,
        ]
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_manifest(manifest: dict[str, Any], *, secret: str) -> bool:
    """Verify a manifest: fetch artifact, re-hash, check receipt.

    Returns True if both the sha256 and the write receipt verify.
    Prints diagnostic details to stdout/stderr.
    """
    uri = manifest["artifact_uri"]
    declared_sha = manifest["artifact_sha256"]
    declared_size = int(manifest["artifact_size_bytes"])
    fmt = manifest["artifact_format"]
    receipt = manifest["write_receipt"]

    print("Verifying artifact manifest:")
    print(f"  URI:      {uri}")
    print(f"  SHA-256:  {declared_sha}")
    print(f"  Size:     {declared_size} bytes")
    print(f"  Format:   {fmt}")

    # Step 1: fetch the artifact bytes.
    try:
        artifact_bytes = _fetch_artifact(uri)
    except Exception as exc:
        print(f"FAIL: cannot fetch artifact: {exc}", file=sys.stderr)
        return False

    # Step 2: re-hash with SHA-256 and compare.
    actual_sha = hashlib.sha256(artifact_bytes).hexdigest()
    actual_size = len(artifact_bytes)
    print(f"  Fetched:  {actual_size} bytes, sha256={actual_sha}")

    if actual_sha != declared_sha:
        print(
            f"FAIL: sha256 mismatch — manifest declares {declared_sha} "
            f"but artifact hashes to {actual_sha}",
            file=sys.stderr,
        )
        return False
    if actual_size != declared_size:
        print(
            f"FAIL: size mismatch — manifest declares {declared_size} "
            f"but artifact is {actual_size} bytes",
            file=sys.stderr,
        )
        return False
    print("  SHA-256:  OK (matches manifest)")

    # Step 3: verify the HMAC write receipt.
    expected_receipt = _recompute_receipt(
        artifact_uri=uri,
        artifact_sha256=declared_sha,
        artifact_size_bytes=declared_size,
        artifact_format=fmt,
        secret=secret,
    )
    if not hmac.compare_digest(expected_receipt, receipt):
        print(
            "FAIL: write receipt HMAC mismatch — manifest receipt does "
            "not match the recomputed HMAC (forged or wrong secret).",
            file=sys.stderr,
        )
        return False
    print("  Receipt:  OK (HMAC verified)")

    print("VERIFIED: artifact sha256 matches and write receipt is authentic.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an artifact manifest (sha256 + HMAC write receipt).",
    )
    parser.add_argument(
        "manifest_path",
        help="Path to the artifact manifest JSON (or '-' for stdin).",
    )
    args = parser.parse_args()

    secret = _get_secret()
    manifest = _load_manifest(args.manifest_path)

    try:
        ok = verify_manifest(manifest, secret=secret)
    except Exception as exc:
        print(f"ERROR: verification error: {exc}", file=sys.stderr)
        return 2

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
