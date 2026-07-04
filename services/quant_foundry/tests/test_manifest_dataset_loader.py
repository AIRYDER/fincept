"""
Tests for the ManifestDatasetLoader (Phase 3 / T-2.2).

Acceptance criteria verified:
1. Fetch manifest, verify manifest sha.
2. Fetch/load data from manifest-declared URI.
3. Verify data sha and row count.
4. Verify schema hashes.
5. Return typed dataset frame plus column roles.
6. Bad data checksum fails.
7. Bad row count fails.
8. Unknown data format fails.
9. Missing required column role fails.

Tests requiring pandas/pyarrow use ``pytest.importorskip`` so they are
skipped in environments without those deps.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

import pytest

from fincept_core.datasets import (
    ColumnRoles,
    DatasetLoadError,
    DatasetLoadReceipt,
    LoadedDataset,
    ManifestDatasetLoader,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_load_error(code: str, callable_obj: Any) -> DatasetLoadError:
    """Assert that ``callable_obj`` raises a DatasetLoadError with ``code``."""
    with pytest.raises(DatasetLoadError) as exc_info:
        callable_obj()
    assert exc_info.value.code == code, (
        f"expected code={code!r}, got {exc_info.value.code!r}: {exc_info.value}"
    )
    return exc_info.value


def _write_manifest(
    tmp_path: pathlib.Path,
    *,
    data_uri: str,
    data_sha256: str | None = None,
    data_format: str | None = None,
    row_count: int | None = None,
    feature_schema_hash: str | None = None,
    label_schema_hash: str | None = None,
    column_roles: dict | None = None,
) -> tuple[pathlib.Path, str, str]:
    """Write a manifest JSON file and return (path, uri, sha256)."""
    manifest: dict = {
        "dataset_id": "test-dataset",
        "data_uri": data_uri,
    }
    if data_sha256 is not None:
        manifest["data_sha256"] = data_sha256
    if data_format is not None:
        manifest["data_format"] = data_format
    if row_count is not None:
        manifest["row_count"] = row_count
    if feature_schema_hash is not None:
        manifest["feature_schema_hash"] = feature_schema_hash
    if label_schema_hash is not None:
        manifest["label_schema_hash"] = label_schema_hash
    if column_roles is not None:
        manifest["column_roles"] = column_roles
    manifest_json = json.dumps(manifest, sort_keys=True, indent=2)
    manifest_path = tmp_path / "test.manifest.json"
    # Write bytes directly to avoid platform line-ending conversion
    # (write_text on Windows inserts \r\n, changing the sha).
    manifest_bytes = manifest_json.encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha = _sha256_bytes(manifest_bytes)
    return manifest_path, manifest_path.as_uri(), manifest_sha


def _write_csv(
    tmp_path: pathlib.Path,
    rows: int = 5,
    *,
    name: str = "data.csv",
) -> tuple[pathlib.Path, str, str]:
    """Write a CSV data file and return (path, uri, sha256)."""
    lines = ["timestamp,f1,f2,label"]
    for i in range(rows):
        lines.append(f"{i},{i * 1.0},{i * 2.0},{i % 2}")
    csv_text = "\n".join(lines) + "\n"
    csv_path = tmp_path / name
    csv_bytes = csv_text.encode("utf-8")
    csv_path.write_bytes(csv_bytes)
    csv_sha = _sha256_bytes(csv_bytes)
    return csv_path, csv_path.as_uri(), csv_sha


# ---------------------------------------------------------------------------
# ColumnRoles
# ---------------------------------------------------------------------------


class TestColumnRoles:
    """Tests for the ColumnRoles model."""

    def test_valid_column_roles(self) -> None:
        roles = ColumnRoles(
            feature_columns=("f1", "f2"),
            label_columns=("label",),
            timestamp_column="timestamp",
        )
        assert roles.feature_columns == ("f1", "f2")
        assert roles.label_columns == ("label",)
        assert roles.timestamp_column == "timestamp"

    def test_empty_feature_columns_fails(self) -> None:
        with pytest.raises(Exception, match="feature_columns"):
            ColumnRoles(feature_columns=(), label_columns=("label",))

    def test_empty_label_columns_fails(self) -> None:
        with pytest.raises(Exception, match="label_columns"):
            ColumnRoles(feature_columns=("f1",), label_columns=())

    def test_duplicate_feature_columns_fails(self) -> None:
        with pytest.raises(Exception, match="duplicate"):
            ColumnRoles(
                feature_columns=("f1", "f1"),
                label_columns=("label",),
            )

    def test_frozen(self) -> None:
        roles = ColumnRoles(
            feature_columns=("f1",),
            label_columns=("label",),
        )
        with pytest.raises(Exception):
            roles.feature_columns = ("f2",)  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(Exception):
            ColumnRoles(
                feature_columns=("f1",),
                label_columns=("label",),
                bogus="no",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# DatasetLoadReceipt
# ---------------------------------------------------------------------------


class TestDatasetLoadReceipt:
    """Tests for the DatasetLoadReceipt model."""

    def test_valid_receipt(self) -> None:
        receipt = DatasetLoadReceipt(
            manifest_uri="file:///tmp/m.json",
            manifest_sha256_verified=True,
            data_uri="file:///tmp/d.csv",
            data_sha256_verified=True,
            row_count_verified=True,
            schema_verified=True,
            loaded_at_ns=12345,
        )
        assert receipt.manifest_sha256_verified is True
        assert receipt.loaded_at_ns == 12345

    def test_empty_uri_fails(self) -> None:
        with pytest.raises(Exception):
            DatasetLoadReceipt(
                manifest_uri="",
                manifest_sha256_verified=True,
                data_uri="file:///tmp/d.csv",
                data_sha256_verified=True,
                row_count_verified=True,
                schema_verified=True,
                loaded_at_ns=1,
            )

    def test_frozen(self) -> None:
        receipt = DatasetLoadReceipt(
            manifest_uri="file:///tmp/m.json",
            manifest_sha256_verified=True,
            data_uri="file:///tmp/d.csv",
            data_sha256_verified=True,
            row_count_verified=True,
            schema_verified=True,
            loaded_at_ns=1,
        )
        with pytest.raises(Exception):
            receipt.manifest_uri = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ManifestDatasetLoader — happy path
# ---------------------------------------------------------------------------


class TestManifestDatasetLoaderHappyPath:
    """Acceptance: fetch manifest, verify sha, load data, verify hashes + row count."""

    def test_load_csv_with_full_verification(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=5)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=5,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=5,
        )
        loaded = loader.load()
        assert isinstance(loaded, LoadedDataset)
        assert loaded.row_count == 5
        assert loaded.load_receipt.manifest_sha256_verified is True
        assert loaded.load_receipt.data_sha256_verified is True
        assert loaded.load_receipt.row_count_verified is True
        # Column roles inferred.
        assert "label" in loaded.column_roles.label_columns
        assert len(loaded.column_roles.feature_columns) >= 2

    def test_load_returns_dataframe(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=3)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=3,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=3,
        )
        loaded = loader.load()
        # df is a pandas DataFrame.
        assert hasattr(loaded.df, "columns")
        assert len(loaded.df) == 3

    def test_load_with_explicit_column_roles(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        roles = ColumnRoles(
            feature_columns=("f1", "f2"),
            label_columns=("label",),
            timestamp_column="timestamp",
        )
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
            column_roles=roles,
        )
        loaded = loader.load()
        assert loaded.column_roles.feature_columns == ("f1", "f2")
        assert loaded.column_roles.timestamp_column == "timestamp"

    def test_load_with_manifest_column_roles(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        roles_dict = {
            "feature_columns": ["f1", "f2"],
            "label_columns": ["label"],
            "timestamp_column": "timestamp",
        }
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
            column_roles=roles_dict,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        loaded = loader.load()
        assert loaded.column_roles.feature_columns == ("f1", "f2")
        assert loaded.column_roles.label_columns == ("label",)

    def test_load_without_manifest_sha_skips_verification(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        _manifest_path, manifest_uri, _ = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        # No manifest_sha256 declared → loader computes hash but does
        # not fail (canary/research permissive).
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=None,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        loaded = loader.load()
        assert loaded.load_receipt.manifest_sha256_verified is False
        assert loaded.manifest_hash  # still computed


# ---------------------------------------------------------------------------
# ManifestDatasetLoader — failure paths (fail-closed)
# ---------------------------------------------------------------------------


class TestManifestDatasetLoaderFailures:
    """Acceptance: bad checksum, bad row count, unknown format, missing roles all fail."""

    def test_bad_manifest_sha_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        _manifest_path, manifest_uri, _ = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256="0" * 64,  # wrong hash
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
        )
        _assert_load_error("manifest_hash_mismatch", loader.load)

    def test_bad_data_sha_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, _csv_sha = _write_csv(tmp_path, rows=2)
        # Manifest declares a WRONG data sha — the actual data won't match.
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256="0" * 64,
            data_format="csv",
            row_count=2,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256="0" * 64,  # wrong data hash
            data_format="csv",
            row_count=2,
        )
        _assert_load_error("data_hash_mismatch", loader.load)

    def test_bad_row_count_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=5)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=999,  # wrong
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=999,
        )
        _assert_load_error("row_count_mismatch", loader.load)

    def test_unknown_data_format_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="xml",  # unsupported
            row_count=2,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="xml",
            row_count=2,
        )
        _assert_load_error("unknown_data_format", loader.load)

    def test_unknown_format_inferred_from_extension_fails(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        pytest.importorskip("pandas")
        # Write a file with an unsupported extension.
        bad_path = tmp_path / "data.xml"
        bad_path.write_text("<x/>", encoding="utf-8")
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=bad_path.as_uri(),
            data_format=None,  # force inference from extension
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=bad_path.as_uri(),
            data_format=None,
        )
        _assert_load_error("unknown_data_format", loader.load)

    def test_schema_hash_mismatch_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=2)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
            feature_schema_hash="a" * 64,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=2,
            feature_schema_hash="b" * 64,  # mismatch with manifest
        )
        _assert_load_error("schema_hash_mismatch", loader.load)

    def test_missing_manifest_file_fails(self, tmp_path: pathlib.Path) -> None:
        loader = ManifestDatasetLoader(
            manifest_uri=(tmp_path / "nonexistent.json").as_uri(),
            manifest_sha256="0" * 64,
            data_uri=(tmp_path / "data.csv").as_uri(),
            data_format="csv",
        )
        _assert_load_error("fetch_failed", loader.load)

    def test_missing_data_file_fails(self, tmp_path: pathlib.Path) -> None:
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=(tmp_path / "nonexistent.csv").as_uri(),
            data_format="csv",
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=(tmp_path / "nonexistent.csv").as_uri(),
            data_format="csv",
        )
        _assert_load_error("fetch_failed", loader.load)

    def test_invalid_manifest_json_fails(self, tmp_path: pathlib.Path) -> None:
        bad_manifest = tmp_path / "bad.manifest.json"
        bad_manifest.write_text("{not valid json", encoding="utf-8")
        loader = ManifestDatasetLoader(
            manifest_uri=bad_manifest.as_uri(),
            manifest_sha256=None,
            data_uri=(tmp_path / "data.csv").as_uri(),
            data_format="csv",
        )
        _assert_load_error("parse_failed", loader.load)

    def test_missing_column_role_fails(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        # Write a CSV with only one column (no features can be inferred
        # if the single column is the label).
        csv_path = tmp_path / "data.csv"
        csv_bytes = b"label\n1\n2\n3\n"
        csv_path.write_bytes(csv_bytes)
        csv_sha = _sha256_bytes(csv_bytes)
        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_path.as_uri(),
            data_sha256=csv_sha,
            data_format="csv",
            row_count=3,
        )
        loader = ManifestDatasetLoader(
            manifest_uri=manifest_uri,
            manifest_sha256=manifest_sha,
            data_uri=csv_path.as_uri(),
            data_sha256=csv_sha,
            data_format="csv",
            row_count=3,
        )
        _assert_load_error("missing_column_role", loader.load)


# ---------------------------------------------------------------------------
# ManifestDatasetLoader — duck-typed spec
# ---------------------------------------------------------------------------


class TestDuckTypedSpec:
    """The loader accepts any object with the ManifestLike attributes."""

    def test_load_from_duck_typed_spec(self, tmp_path: pathlib.Path) -> None:
        pytest.importorskip("pandas")
        _csv_path, csv_uri, csv_sha = _write_csv(tmp_path, rows=3)

        class FakeSpec:
            manifest_uri = csv_uri  # will be set after manifest write
            manifest_sha256 = None
            data_uri = csv_uri
            data_sha256 = csv_sha
            data_format = "csv"
            row_count = 3
            feature_schema_hash = None
            label_schema_hash = None

        _manifest_path, manifest_uri, manifest_sha = _write_manifest(
            tmp_path,
            data_uri=csv_uri,
            data_sha256=csv_sha,
            data_format="csv",
            row_count=3,
        )
        spec = FakeSpec()
        spec.manifest_uri = manifest_uri
        spec.manifest_sha256 = manifest_sha
        loader = ManifestDatasetLoader(spec=spec)
        loaded = loader.load()
        assert loaded.row_count == 3
        assert loaded.load_receipt.manifest_sha256_verified is True
