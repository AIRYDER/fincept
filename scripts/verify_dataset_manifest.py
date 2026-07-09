#!/usr/bin/env python3
"""verify_dataset_manifest.py — verify a point-in-time dataset manifest.

Standalone CLI that loads a manifest JSON file and verifies its integrity
before a training worker trusts it. This is the manifest-first verification
gate: the worker fetches the manifest, verifies its hash, then reads the data
and verifies the data hash, row count, and schema hashes.

Verification steps (each must pass or the script exits non-zero):
  1. Load the manifest JSON from ``--manifest-path``.
  2. Verify ``manifest_hash`` — recompute SHA-256 over the canonical manifest
     payload (the fields that affect reproducibility) and compare to the
     ``manifest_hash`` field in the JSON.
  3. Verify ``data_sha256`` — compute SHA-256 of the data file at
     ``--data-path`` and compare to the ``data_sha256`` field.
  4. Verify ``row_count`` — read the data file and count rows, compare to the
     ``row_count`` field.
  5. Verify ``feature_schema_hash`` — if ``feature_names`` is present in the
     manifest, recompute SHA-256 of the sorted feature names joined by ``:``
     and compare. Otherwise verify the hash is a valid 64-char hex string.
  6. Verify ``pit_proof_verified`` is True (point-in-time proof is mandatory).

The script is deliberately standalone — it does NOT import from
``quant_foundry`` (to avoid circular deps). It does its own hash computation
and file reading. Heavy data deps (polars/pandas/pyarrow) are lazily imported
inside the functions that need them so the script loads fast and fails with a
clear message if a dep is missing.

Usage:
    python scripts/verify_dataset_manifest.py \\
        --manifest-path /data/dataset.manifest.json \\
        --data-path /data/dataset.parquet

Exit codes:
    0 — all verifications passed (VERIFIED)
    1 — one or more verifications failed (FAILED)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# 64-char lowercase hex (SHA-256).
_HEX256_RE = re.compile(r"^[0-9a-f]{64}$")

# The canonical payload fields that are hashed into manifest_hash.
# This mirrors FeatureLakeManifest._canonical_payload() — kept in sync here
# so the script is standalone (no import from quant_foundry).
_CANONICAL_FIELDS: tuple[str, ...] = (
    "schema_version",
    "dataset_id",
    "feature_schema_hash",
    "label_schema_hash",
    "as_of_ts",
    "universe_hash",
    "row_count",
    "checksum",
    "folds",
    "pit_proof_verified",
    "source_vintage_refs",
    "quality_report_hash",
    "manifest_uri",
    "data_uri",
    "data_format",
    "data_sha256",
    "quality_report_uri",
    "quality_report_sha256",
)


# ---------------------------------------------------------------------------
# Verification primitives (pure functions, no quant_foundry imports)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file (streaming, memory-efficient)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract the canonical payload fields from the manifest dict.

    Only the fields in ``_CANONICAL_FIELDS`` participate in the manifest hash.
    Extra fields (e.g. ``availability``, ``feature_names``) are excluded so the
    hash matches the one computed by ``FeatureLakeManifest.manifest_hash()``.
    """
    return {k: manifest.get(k) for k in _CANONICAL_FIELDS}


def verify_manifest_hash(manifest: dict[str, Any]) -> tuple[bool, str]:
    """Verify manifest_hash by recomputing SHA-256 over the canonical payload.

    Returns (ok, message).
    """
    declared = manifest.get("manifest_hash")
    if not declared or not isinstance(declared, str):
        return False, "manifest_hash field is missing or not a string"
    if not _HEX256_RE.match(declared):
        return False, f"manifest_hash is not a valid 64-char hex SHA-256: {declared!r}"

    payload = _canonical_payload(manifest)
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    if actual != declared:
        return False, (f"manifest_hash mismatch: declared={declared}, computed={actual}")
    return True, f"manifest_hash verified ({declared[:16]}…)"


def verify_data_sha256(data_path: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    """Verify data_sha256 by computing SHA-256 of the data file.

    Returns (ok, message). If data_sha256 is not set in the manifest, this
    check is skipped (returns ok=True with a skip message).
    """
    declared = manifest.get("data_sha256")
    if not declared:
        return True, "data_sha256 not declared in manifest (skipped)"
    if not isinstance(declared, str) or not _HEX256_RE.match(declared):
        return False, f"data_sha256 is not a valid 64-char hex SHA-256: {declared!r}"

    if not data_path.exists():
        return False, f"data file not found: {data_path}"

    actual = _sha256_file(data_path)
    if actual != declared:
        return False, f"data_sha256 mismatch: declared={declared}, computed={actual}"
    return True, f"data_sha256 verified ({declared[:16]}…)"


def _count_rows(data_path: Path) -> int:
    """Count rows in a parquet or csv file (lazy import of data deps)."""
    suffix = data_path.suffix.lower()

    if suffix in (".parquet", ".parquet.gz"):
        try:
            import polars as pl
        except ImportError:
            pass
        else:
            df = pl.read_parquet(str(data_path))
            return df.height
        try:
            import pyarrow.parquet as pq
        except ImportError:
            pass
        else:
            meta = pq.read_metadata(str(data_path))
            return meta.num_rows
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError("cannot read parquet: install polars, pyarrow, or pandas") from exc
        df = pd.read_parquet(str(data_path))
        return len(df)

    if suffix in (".csv", ".csv.gz"):
        try:
            import polars as pl
        except ImportError:
            pass
        else:
            df = pl.read_csv(str(data_path))
            return df.height
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError("cannot read csv: install polars or pandas") from exc
        df = pd.read_csv(str(data_path))
        return len(df)

    raise ValueError(
        f"unsupported data format: {suffix} (expected .parquet, .parquet.gz, .csv, or .csv.gz)"
    )


def verify_row_count(data_path: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    """Verify row_count by reading the data file and counting rows.

    Returns (ok, message).
    """
    declared = manifest.get("row_count")
    if declared is None:
        return False, "row_count field is missing from manifest"
    if not isinstance(declared, int) or declared < 0:
        return False, f"row_count is not a non-negative int: {declared!r}"

    if not data_path.exists():
        return False, f"data file not found: {data_path}"

    try:
        actual = _count_rows(data_path)
    except (ImportError, ValueError) as exc:
        return False, f"cannot count rows: {exc}"

    if actual != declared:
        return False, f"row_count mismatch: declared={declared}, actual={actual}"
    return True, f"row_count verified ({declared} rows)"


def verify_feature_schema_hash(manifest: dict[str, Any]) -> tuple[bool, str]:
    """Verify feature_schema_hash.

    If ``feature_names`` is present in the manifest, recompute SHA-256 of the
    sorted feature names joined by ``:`` and compare. Otherwise verify the
    hash is a valid 64-char hex string (structural check).
    """
    declared = manifest.get("feature_schema_hash")
    if not declared or not isinstance(declared, str):
        return False, "feature_schema_hash field is missing or not a string"
    if not _HEX256_RE.match(declared):
        return False, (f"feature_schema_hash is not a valid 64-char hex SHA-256: {declared!r}")

    feature_names = manifest.get("feature_names")
    if feature_names is not None:
        if not isinstance(feature_names, list):
            return False, "feature_names is present but not a list"
        payload = ":".join(sorted(str(n) for n in feature_names))
        actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if actual != declared:
            return False, (f"feature_schema_hash mismatch: declared={declared}, computed={actual}")
        return True, f"feature_schema_hash verified from feature_names ({declared[:16]}…)"

    return True, f"feature_schema_hash present ({declared[:16]}…) — no feature_names to recompute"


def verify_pit_proof(manifest: dict[str, Any]) -> tuple[bool, str]:
    """Verify pit_proof_verified is True (point-in-time proof is mandatory)."""
    pit = manifest.get("pit_proof_verified")
    if pit is None:
        return False, "pit_proof_verified field is missing from manifest"
    if pit is not True:
        return False, (
            f"pit_proof_verified is {pit!r} — must be True (point-in-time proof is mandatory)"
        )
    return True, "pit_proof_verified=True"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _print_receipt(
    manifest_path: Path,
    data_path: Path,
    manifest: dict[str, Any],
    results: list[tuple[str, bool, str]],
) -> None:
    """Print a clear verification receipt."""
    all_ok = all(ok for _, ok, _ in results)
    status = "VERIFIED" if all_ok else "FAILED"

    print("=" * 72)
    print(f"  DATASET MANIFEST VERIFICATION RECEIPT — {status}")
    print("=" * 72)
    print(f"  manifest_path : {manifest_path}")
    print(f"  data_path     : {data_path}")
    print(f"  dataset_id    : {manifest.get('dataset_id', '<missing>')}")
    print(f"  manifest_hash : {manifest.get('manifest_hash', '<missing>')}")
    print("-" * 72)
    for name, ok, msg in results:
        marker = "[PASS]" if ok else "[FAIL]"
        print(f"  {marker} {name}: {msg}")
    print("=" * 72)
    if all_ok:
        print("  All verifications passed — manifest is trustworthy.")
    else:
        failures = [name for name, ok, _ in results if not ok]
        print(f"  {len(failures)} verification(s) failed: {', '.join(failures)}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a point-in-time dataset manifest before training.",
    )
    parser.add_argument(
        "--manifest-path",
        required=True,
        type=Path,
        help="Path to the manifest JSON file.",
    )
    parser.add_argument(
        "--data-path",
        required=True,
        type=Path,
        help="Path to the data file (parquet or csv).",
    )
    args = parser.parse_args(argv)

    manifest_path: Path = args.manifest_path
    data_path: Path = args.data_path

    # --- load manifest ---
    if not manifest_path.exists():
        print(f"FAILED: manifest file not found: {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"FAILED: cannot load manifest JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(manifest, dict):
        print("FAILED: manifest JSON is not an object", file=sys.stderr)
        return 1

    # --- run verifications ---
    results: list[tuple[str, bool, str]] = []

    ok, msg = verify_manifest_hash(manifest)
    results.append(("manifest_hash", ok, msg))

    ok, msg = verify_data_sha256(data_path, manifest)
    results.append(("data_sha256", ok, msg))

    ok, msg = verify_row_count(data_path, manifest)
    results.append(("row_count", ok, msg))

    ok, msg = verify_feature_schema_hash(manifest)
    results.append(("feature_schema_hash", ok, msg))

    ok, msg = verify_pit_proof(manifest)
    results.append(("pit_proof_verified", ok, msg))

    # --- print receipt ---
    _print_receipt(manifest_path, data_path, manifest, results)

    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
